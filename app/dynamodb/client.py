"""DynamoDB DocumentClient equivalent (port of src/config/aws.config.js).

Wraps the low-level, thread-safe boto3 client and does what aws-sdk v2's
``DynamoDB.DocumentClient`` did implicitly:

* marshal native values (numbers, strings, None, dicts, lists) to
  AttributeValues and back;
* return numbers as JS-style numbers (``int`` when integral, else ``float``)
  instead of boto3's ``Decimal``;
* accept the same request shapes (KeyConditionExpression strings,
  ExpressionAttributeValues/Names, ExclusiveStartKey, Select=COUNT...).

boto3 is synchronous; calls run on a dedicated bounded thread pool so they
never block the event loop. Credentials/region resolve exactly like the Node
service (AWS_DYNOMODB_ACCESS_KEY/SECRET, region AWS_S3_REGION);
AWS_DYNAMODB_ENDPOINT_URL optionally points at a local emulator/VPC endpoint.

Also provides ``insert_message`` (Counters-table auto-increment id + put +
message-list version bump), ported from the same file.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.config import Config
from botocore.exceptions import ClientError

from app.core.config import get_settings
from app.core.js import UNDEFINED, num_out
from app.redis import cache_keys, cache_service

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()
_executor = ThreadPoolExecutor(max_workers=64, thread_name_prefix="dynamodb")
_client = None


def _to_dynamo(value: Any) -> Any:
    """Native -> boto3-serialisable (floats become Decimal, UNDEFINED dropped)."""
    if isinstance(value, bool) or value is None or isinstance(value, str | bytes):
        return value
    if isinstance(value, float):
        # NaN/Infinity raise in the serializer, like aws-sdk v2 rejecting the request.
        return Decimal(repr(value))
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items() if v is not UNDEFINED}
    if isinstance(value, list | tuple):
        return [_to_dynamo(v) for v in value]
    if hasattr(value, "isoformat"):
        # DocumentClient would serialise a Date as an empty map; the service
        # always stores ISO strings, so this branch only guards mistakes.
        return value.isoformat()
    return value


def _from_dynamo(value: Any) -> Any:
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return num_out(float(value))
    if isinstance(value, dict):
        return {k: _from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_dynamo(v) for v in value]
    if isinstance(value, set):
        return [_from_dynamo(v) for v in value]
    return value


def serialize_item(item: dict[str, Any]) -> dict[str, Any]:
    return {k: _serializer.serialize(_to_dynamo(v)) for k, v in item.items() if v is not UNDEFINED}


def deserialize_item(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {k: _from_dynamo(_deserializer.deserialize(v)) for k, v in item.items()}


def get_client():
    global _client
    if _client is None:
        settings = get_settings()
        kwargs: dict[str, Any] = {
            "region_name": settings.aws_s3_region,
            "config": Config(max_pool_connections=64, retries={"max_attempts": 3, "mode": "standard"}),
        }
        if settings.aws_dynamodb_access_key and settings.aws_dynamodb_secret_key:
            kwargs["aws_access_key_id"] = settings.aws_dynamodb_access_key
            kwargs["aws_secret_access_key"] = settings.aws_dynamodb_secret_key
        if settings.raw.AWS_DYNAMODB_ENDPOINT_URL:
            kwargs["endpoint_url"] = settings.raw.AWS_DYNAMODB_ENDPOINT_URL
        _client = boto3.client("dynamodb", **kwargs)
    return _client


def set_client(client) -> None:
    """Test seam."""
    global _client
    _client = client


async def _call(method: str, **params: Any) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    fn = getattr(get_client(), method)
    return await loop.run_in_executor(_executor, lambda: fn(**params))


def _prepare(params: dict[str, Any]) -> dict[str, Any]:
    out = dict(params)
    if "Key" in out:
        out["Key"] = serialize_item(out["Key"])
    if "Item" in out:
        out["Item"] = serialize_item(out["Item"])
    if "ExclusiveStartKey" in out and out["ExclusiveStartKey"] is not None:
        out["ExclusiveStartKey"] = serialize_item(out["ExclusiveStartKey"])
    elif "ExclusiveStartKey" in out:
        del out["ExclusiveStartKey"]
    if "ExpressionAttributeValues" in out:
        out["ExpressionAttributeValues"] = serialize_item(out["ExpressionAttributeValues"])
    return out


def _result(response: dict[str, Any]) -> dict[str, Any]:
    out = dict(response)
    out.pop("ResponseMetadata", None)
    if "Items" in out:
        out["Items"] = [deserialize_item(i) for i in out["Items"]]
    if "Item" in out:
        out["Item"] = deserialize_item(out["Item"])
    if "Attributes" in out:
        out["Attributes"] = deserialize_item(out["Attributes"])
    if "LastEvaluatedKey" in out:
        out["LastEvaluatedKey"] = deserialize_item(out["LastEvaluatedKey"])
    return out


class DocumentClient:
    """``AWS.DynamoDB.DocumentClient`` methods used by the service."""

    async def get(self, **params: Any) -> dict[str, Any]:
        return _result(await _call("get_item", **_prepare(params)))

    async def put(self, **params: Any) -> dict[str, Any]:
        return _result(await _call("put_item", **_prepare(params)))

    async def update(self, **params: Any) -> dict[str, Any]:
        return _result(await _call("update_item", **_prepare(params)))

    async def delete(self, **params: Any) -> dict[str, Any]:
        return _result(await _call("delete_item", **_prepare(params)))

    async def query(self, **params: Any) -> dict[str, Any]:
        return _result(await _call("query", **_prepare(params)))


document_client = DocumentClient()


def error_code(error: BaseException) -> str | None:
    if isinstance(error, ClientError):
        return error.response.get("Error", {}).get("Code")
    return None


async def increment_counter(table_name: str) -> int:
    """Atomic ``SET #count = if_not_exists(#count, :start) + :inc`` on the Counters table."""
    settings = get_settings()
    result = await document_client.update(
        TableName=settings.dynamodb_counter_table,
        Key={"tableName": table_name},
        UpdateExpression="SET #count = if_not_exists(#count, :start) + :inc",
        ExpressionAttributeNames={"#count": "count"},
        ExpressionAttributeValues={":inc": 1, ":start": 0},
        ReturnValues="UPDATED_NEW",
    )
    return result["Attributes"]["count"]


async def insert_message(message: dict[str, Any]) -> int:
    """Insert a chat message with an application-side auto-increment id.

    H-02: a put failure propagates (callers turn it into ``{error}``). The
    message-list version bump is best-effort and never turns a successful
    write into a failure.
    """
    table = get_settings().dynamodb_message_table
    new_id = await increment_counter(table)
    item = {"id": new_id, **message}
    await document_client.put(TableName=table, Item=item)
    try:
        await cache_service.bump_version(cache_keys.version_message_key(message.get("group_id")))
    except Exception as cache_error:  # noqa: BLE001
        print(f"bumpVersion failed after message insert (message saved): {cache_error}", flush=True)
    return new_id
