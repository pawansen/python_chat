"""Shared constants for the local regression harness (never production values)."""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

import jwt

ROOT = Path(__file__).resolve().parents[2]
NODE_PROJECT = Path(os.environ.get("NODE_CHAT_DIR", r"E:\xampp\htdocs\maidmatch-chat"))
HARNESS_DIR = Path(__file__).resolve().parent
RUN_DIR = HARNESS_DIR / ".run"

MYSQL_HOST = os.environ.get("REGRESS_MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.environ.get("REGRESS_MYSQL_PORT", "3307"))
MYSQL_USER = os.environ.get("REGRESS_MYSQL_USER", "root")
MYSQL_PASS = os.environ.get("REGRESS_MYSQL_PASS", "")
SCHEMA_SOURCE_DB = os.environ.get("REGRESS_SCHEMA_SOURCE_DB", "maidmatch_ins_s1_verify")
TEST_DB = "py_chat_regress"  # created/dropped by the harness only

DYNAMO_PORT = int(os.environ.get("REGRESS_DYNAMO_PORT", "5005"))
DYNAMO_ENDPOINT = f"http://127.0.0.1:{DYNAMO_PORT}"
MESSAGE_TABLE = "chat_messages_regress"
COUNTER_TABLE = "Counters_regress"

REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379
NODE_REDIS_DB = 13
PY_REDIS_DB = 14

MOCK_PORT = 9099
NODE_PORT = 7191
PY_PORT = 7291
API_KEY = "regress-api-key"
JWT_SECRET = "regress-jwt-secret-0123456789abcdef"


def token(user_id: int, *, expired: bool = False, secret: str = JWT_SECRET, jti: str | None = None, alg: str = "HS256") -> str:
    now = int(time.time())
    claims = {"iss": "regress", "iat": now - 10, "nbf": now - 10, "exp": now - 3600 if expired else now + 3600,
              "jti": jti or f"jti-{user_id}", "sub": user_id, "prv": "regress"}
    return jwt.encode(claims, secret, algorithm=alg)


def _dummy_service_account() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    account = {
        "type": "service_account", "project_id": "regress-project", "private_key_id": "regress",
        "private_key": pem, "client_email": "regress@regress-project.iam.gserviceaccount.com", "client_id": "1",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    return base64.b64encode(json.dumps(account).encode()).decode()


def server_env(kind: str) -> dict[str, str]:
    """Environment shared by both servers (only the port / cache DB differ)."""
    env = {
        "NODE_ENV": "production",
        "LOG_LEVEL": "error",
        "PORT": str(NODE_PORT if kind == "node" else PY_PORT),
        "DB_HOST": MYSQL_HOST, "DB_PORT": str(MYSQL_PORT), "DB_NAME": TEST_DB, "DB_USER": MYSQL_USER, "DB_PASS": MYSQL_PASS,
        "DB_DIALECT": "mysql",
        "API_KEY": API_KEY,
        "ACCESS_TOKEN_SECRET": JWT_SECRET,
        "REDIS_HOST": REDIS_HOST, "REDIS_PORT": str(REDIS_PORT),
        "REDIS_DB": str(NODE_REDIS_DB if kind == "node" else PY_REDIS_DB),
        "IS_REDIS_ENABLED": "true",
        "REDIS_SOCKET_ADAPTER_ENABLED": "false",
        "INVALIDATION_BUS_ENABLED": "false",
        "AWS_S3_REGION": "ap-southeast-1", "AWS_REGION": "ap-southeast-1",
        "AWS_DYNOMODB_ACCESS_KEY": "testing", "AWS_DYNOMODB_SECRET_KEY": "testing",
        "AWS_S3_ACCESS_KEY_ID": "testing", "AWS_S3_SECRET_ACCESS_KEY": "testing", "AWS_S3_BUCKET": "regress-bucket",
        "AWS_S3_BASE": "https://regress-bucket.s3.ap-southeast-1.amazonaws.com/",
        "AWS_DYNAMODB_ENDPOINT_URL": DYNAMO_ENDPOINT, "AWS_S3_ENDPOINT_URL": DYNAMO_ENDPOINT,
        "REGRESS_AWS_ENDPOINT": DYNAMO_ENDPOINT,
        "DynoMODB_Message_Table": MESSAGE_TABLE, "DynoMODB_Counter_Table": COUNTER_TABLE,
        "DEFAULT_PROFILE_IMAGE": "https://cdn.example.test/default.png",
        "WHATSAPP_API_URL": f"http://127.0.0.1:{MOCK_PORT}", "WHATSAPP_API_KEY": "regress-d360",
        "OUTBOUND_HTTP_TIMEOUT_MS": "3000", "TRANSLATE_API_URL": f"http://127.0.0.1:{MOCK_PORT}/translate",
        "TRANSLATE_API_KEY": "regress-translate",
        "TRANSLATE_API_TIMEOUT_MS": "3000", "TRANSLATE_RETRIES": "0", "TRANSLATE_JOB_RETRIES": "0",
        "TWILIO_ACCOUNT_SID": "AC" + "0" * 32, "TWILIO_AUTH_TOKEN": "regress",
        "FIREBASE_SERVICE_ACCOUNT_BASE64": _dummy_service_account(),
        "LCC_OTA_ENABLED": "false",
        "SOCKET_HANDSHAKE_AUTH_ENFORCE": "true",
        "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
        "TZ": "UTC",
    }
    return env
