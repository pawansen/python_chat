"""(Re)build the local regression fixtures. Safe by construction:

* MySQL: only the dedicated ``py_chat_regress`` database is dropped and
  recreated; table STRUCTURES are cloned from the local staging-dump schema
  (``CREATE TABLE ... LIKE``) and filled with SYNTHETIC rows only.
* DynamoDB: only the ``*_regress`` tables on the local moto emulator.

Usage: python -m tests.harness.seed
"""

from __future__ import annotations

import datetime as dt

import boto3
import pymysql

from tests.harness import env

TABLES = [
    "users", "roles", "employers", "workers", "staffs", "groups", "group_participants", "user_devices", "messages",
    "messages_log", "action_logs", "template_categories", "template_contents", "worker_interpreter_assignment_log",
    "interview_bookings", "jobs", "user_images", "agencies", "contracts", "contract_invoices_statuses", "job_views",
    "job_stages", "job_stages_logs",
]

VIEW_SQL = """
CREATE VIEW `user_data_view` AS select `u`.`id` AS `user_id`,`u`.`mobile_number` AS `mobile_number`,`u`.`country_code` AS `country_code`,
`u`.`email` AS `email`,`u`.`chat_translate_view` AS `chat_translate_view`,`u`.`chat_translate_language` AS `chat_translate_language`,
`u`.`role_id` AS `role_id`,`r`.`role_name` AS `role_name`,`r`.`guard_name` AS `guard_name`,`u`.`crop_original_image` AS `profile_image`,
`u`.`device_id` AS `device_id`,`s`.`staff_type` AS `staff_type`,`u`.`line_user_id` AS `line_user_id`,
(case when (`r`.`guard_name` = 'employer') then `e`.`whatsapp_country_code` when (`r`.`guard_name` = 'worker') then `w`.`whatsapp_country_code` else `s`.`whatsapp_country_code` end) AS `whatsapp_country_code`,
(case when (`r`.`guard_name` = 'employer') then `e`.`whatsapp_number` when (`r`.`guard_name` = 'worker') then `w`.`whatsapp_number` else `s`.`whatsapp` end) AS `whatsapp_number`,
(case when (`r`.`guard_name` = 'employer') then concat(replace(replace(`e`.`whatsapp_country_code`,'+',''),'-',''),trim(`e`.`whatsapp_number`)) when (`r`.`guard_name` = 'worker') then concat(replace(replace(`w`.`whatsapp_country_code`,'+',''),'-',''),trim(`w`.`whatsapp_number`)) else concat(replace(replace(`s`.`whatsapp_country_code`,'+',''),'-',''),trim(`s`.`whatsapp`)) end) AS `full_whatsapp_number`,
(case when (`r`.`guard_name` = 'employer') then concat(`e`.`name`,' ',`e`.`surname`) when (`r`.`guard_name` = 'worker') then concat(`w`.`name`,' ',`w`.`surname`) else concat(`s`.`first_name`,' ',`s`.`last_name`) end) AS `related_name`,
(case when (`r`.`guard_name` = 'employer') then `e`.`id` when (`r`.`guard_name` = 'worker') then `w`.`id` else `s`.`id` end) AS `employer_worker_staff_common_id`
from ((((`users` `u` join `roles` `r` on((`u`.`role_id` = `r`.`id`))) left join `employers` `e` on(((`u`.`id` = `e`.`user_id`) and (`r`.`guard_name` = 'employer'))))
left join `workers` `w` on(((`u`.`id` = `w`.`user_id`) and (`r`.`guard_name` = 'worker')))) left join `staffs` `s` on(((`u`.`id` = `s`.`user_id`) and (`r`.`guard_name` not in ('employer','worker')))))
"""

NOW = dt.datetime(2026, 10, 1, 8, 0, 0)


def _insert(cur, table: str, rows: list[dict]) -> None:
    for row in rows:
        cols = ", ".join(f"`{c}`" for c in row)
        marks = ", ".join(["%s"] * len(row))
        cur.execute(f"INSERT INTO `{table}` ({cols}) VALUES ({marks})", list(row.values()))


def seed_mysql() -> None:
    conn = pymysql.connect(host=env.MYSQL_HOST, port=env.MYSQL_PORT, user=env.MYSQL_USER, password=env.MYSQL_PASS, autocommit=True)
    cur = conn.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS `{env.TEST_DB}`")
    cur.execute(f"CREATE DATABASE `{env.TEST_DB}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    cur.execute(f"USE `{env.TEST_DB}`")
    cur.execute("SET SESSION sql_mode = 'NO_ENGINE_SUBSTITUTION'")
    cur.execute("SET time_zone = '+00:00'")  # same session zone as both services (TIMESTAMP columns convert)
    for table in TABLES:
        cur.execute(f"CREATE TABLE `{table}` LIKE `{env.SCHEMA_SOURCE_DB}`.`{table}`")
    cur.execute(VIEW_SQL)
    recent = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(minutes=1)

    _insert(cur, "roles", [
        {"id": 1, "role_name": "Admin", "guard_name": "admin", "status": 1},
        {"id": 2, "role_name": "CS", "guard_name": "cs", "status": 1},
        {"id": 3, "role_name": "Employer", "guard_name": "employer", "status": 1},
        {"id": 4, "role_name": "Worker", "guard_name": "worker", "status": 1},
        {"id": 5, "role_name": "Interpreter", "guard_name": "interpreter", "status": 1},
    ])
    users = [
        (1, 1, "admin@regress.test", "+852", "90000001"),
        (10, 2, "cs@regress.test", "+852", "90000010"),
        (11, 5, "interp@regress.test", "+852", "90000011"),
        (100, 3, "employer@regress.test", "+852", "91000100"),
        (101, 3, "employer2@regress.test", "+852", "91000100"),
        (200, 4, "worker@regress.test", "+62", "8120000200"),
        (201, 4, "worker2@regress.test", "+62", "8120000201"),
        (300, 4, "inactive@regress.test", "+62", "8120000300"),
    ]
    _insert(cur, "users", [
        {"id": uid, "role_id": role, "email": email, "country_code": cc, "mobile_number": mobile, "status": 0 if uid == 300 else 1,
         "mobile_verified": 1, "whatsapp_verified": 1, "last_activity_at": recent, "created_at": NOW, "updated_at": NOW,
         "crop_original_image": f"profile/u{uid}.png" if uid in (100, 200) else None,
         "last_whatsapp_message_ceated_at": NOW if uid == 200 else None, "notification_status": 1}
        for uid, role, email, cc, mobile in users
    ])
    _insert(cur, "staffs", [
        {"id": 1, "user_id": 1, "first_name": "Ada", "last_name": "Admin", "staff_type": "staff", "whatsapp_country_code": "+852", "whatsapp": "90000001", "status": 1},
        {"id": 2, "user_id": 10, "first_name": "Cass", "last_name": "Service", "staff_type": "staff", "whatsapp_country_code": "+852", "whatsapp": "90000010", "status": 1},
        {"id": 3, "user_id": 11, "first_name": "Ivy", "last_name": "Interp", "staff_type": "staff", "status": 1},
    ])
    _insert(cur, "employers", [
        {"id": 50, "user_id": 100, "name": "Ka Yan", "surname": "Chan", "chinese_name": "陳嘉欣", "hk_id": "Z765432(1)",
         "whatsapp_country_code": "+852", "whatsapp_number": "91000100", "birth_date": "1980-01-02", "reference_id": "E000050",
         "cs_owner_id": 2, "alternate_mobile_verified": 0, "status": 1, "created_at": NOW, "updated_at": NOW},
        {"id": 51, "user_id": 101, "name": "Ka Yan", "surname": "Chan", "chinese_name": "陳嘉欣", "hk_id": "Z765432(1)",
         "whatsapp_country_code": "+852", "whatsapp_number": "91000100", "birth_date": "1980-01-02", "reference_id": "E000051",
         "cs_owner_id": 2, "alternate_mobile_verified": 0, "status": 1, "created_at": NOW, "updated_at": NOW},
    ])
    _insert(cur, "workers", [
        {"id": 70, "user_id": 200, "name": "Siti", "surname": "Rahayu", "whatsapp_country_code": "+62", "whatsapp_number": "8120000200",
         "passport_number": "C7654321", "hk_id": "A123456(7)", "date_of_birth": "1995-05-05", "reference_id": "LPF000070", "status": 3},
        {"id": 71, "user_id": 201, "name": "Siti", "surname": "Rahayu", "whatsapp_country_code": "+62", "whatsapp_number": "8120000201",
         "passport_number": "C7654321", "hk_id": "B000000(0)", "date_of_birth": "1995-05-05", "reference_id": "LPF000071", "status": 2},
    ])
    groups = [
        {"id": 1001, "conversation_state": "Active", "friendly_name": "Siti Rahayu", "unique_name": "100-200", "type": "ONE_TO_ONE",
         "platform": "app", "platform_two": "app", "friend_type": "worker", "last_message": "hello", "last_msgtype": "text",
         "last_message_ceated_at": NOW, "message_send_by": 200, "is_deleted": 0, "created_at": NOW, "created_by": 100, "updated_at": NOW},
        {"id": 1002, "conversation_state": "Active", "friendly_name": "J0001-Chan-Rahayu", "unique_name": "555", "type": "GROUP",
         "platform": "app", "friend_type": "group_team", "job_id": 555, "job_description": "J0001", "last_message": "group hi",
         "last_msgtype": "text", "last_message_ceated_at": NOW - dt.timedelta(hours=1), "message_send_by": 10, "is_deleted": 0,
         "created_at": NOW, "created_by": 10, "updated_at": NOW},
        {"id": 1003, "conversation_state": "Active", "friendly_name": "Cass Service/Siti Rahayu", "unique_name": "200", "type": "ONE_TO_MANY",
         "platform": "whatsapp", "platform_two": "whatsapp", "friend_type": "worker", "last_message": "wa msg", "last_msgtype": "text",
         "last_message_ceated_at": NOW - dt.timedelta(hours=2), "message_send_by": 200, "is_deleted": 0, "created_at": NOW,
         "created_by": 10, "updated_at": NOW},
    ]
    _insert(cur, "groups", groups)
    participants = [
        (1001, 100, 1, 3, None, 0, 2), (1001, 200, 0, 4, None, 0, 0),
        (1002, 100, 1, 0, "employer", 0, 1), (1002, 200, 1, 0, "worker", 0, 0), (1002, 10, 1, 0, "cs", 0, 0),
        (1003, 10, 1, 2, None, 0, 0), (1003, 200, 0, 4, None, 1, 0),
    ]
    _insert(cur, "group_participants", [
        {"group_id": g, "user_id": u, "is_host": host, "role_id": role, "role_sid": rsid, "account_sid": str(acc),
         "unread_messages_count": unread, "identity": "", "joined_on": "2026-10-01", "is_deleted": 0, "created_at": NOW,
         "created_by": 1, "updated_at": NOW}
        for g, u, host, role, rsid, acc, unread in participants
    ])
    _insert(cur, "template_categories", [
        {"id": 900, "module_key": "worker", "slug": "passport_lost_expired_action_required", "name_en": "Passport lost", "status": 1},
    ])
    _insert(cur, "template_contents", [
        {"id": 901, "module_key": "worker", "category_id": 900, "template_name": "pp", "template_content": "Hi {{ worker_name }}, please upload",
         "status": 1},
    ])
    _insert(cur, "action_logs", [
        {"id": 1, "user_id": 200, "worker_id": 70, "entity_type": "worker", "action_type": "doc", "content": "Upload passport",
         "status": 1, "created_at": NOW, "created_by": 1},
    ])
    _insert(cur, "jobs", [
        {"id": 7001, "reference_id": "J0001", "status": 1, "employer_id": 50, "created_at": NOW, "expiry_date": "2026-09-01"},
    ])
    _insert(cur, "user_images", [
        {"id": 1, "user_id": 200, "uploaded": "u.png", "final_image": "f.png", "type": "profile", "is_default": 1, "created_at": NOW},
    ])
    conn.close()


def _dynamo():
    return boto3.client("dynamodb", endpoint_url=env.DYNAMO_ENDPOINT, region_name="ap-southeast-1",
                        aws_access_key_id="testing", aws_secret_access_key="testing")


def _gsi(name: str, hash_key: str, range_key: str) -> dict:
    return {"IndexName": name, "KeySchema": [{"AttributeName": hash_key, "KeyType": "HASH"}, {"AttributeName": range_key, "KeyType": "RANGE"}],
            "Projection": {"ProjectionType": "ALL"}}


def seed_dynamo() -> None:
    client = _dynamo()
    existing = client.list_tables()["TableNames"]
    for table in (env.MESSAGE_TABLE, env.COUNTER_TABLE):
        if table in existing:
            client.delete_table(TableName=table)
    client.create_table(TableName=env.COUNTER_TABLE, BillingMode="PAY_PER_REQUEST",
                        KeySchema=[{"AttributeName": "tableName", "KeyType": "HASH"}],
                        AttributeDefinitions=[{"AttributeName": "tableName", "AttributeType": "S"}])
    client.create_table(
        TableName=env.MESSAGE_TABLE, BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}, {"AttributeName": "created_at", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "id", "AttributeType": "N"}, {"AttributeName": "created_at", "AttributeType": "S"},
            {"AttributeName": "group_id", "AttributeType": "N"}, {"AttributeName": "user_id", "AttributeType": "N"},
            {"AttributeName": "receiver_id", "AttributeType": "N"},
        ],
        GlobalSecondaryIndexes=[
            _gsi("ChatIdIndex", "group_id", "id"), _gsi("ChatCreatedAtIndex", "group_id", "created_at"),
            _gsi("CreatedAtIndexSort", "group_id", "created_at"), _gsi("UserConversationIndex", "user_id", "group_id"),
            _gsi("GetUnreadCountIndex", "receiver_id", "group_id"),
        ],
    )
    messages = [
        (1, 1001, 200, 100, "hello", "2026-10-01T07:00:00.000Z", "unread", "app"),
        (2, 1001, 100, 200, "hi siti", "2026-10-01T07:05:00.000Z", "read", "app"),
        (3, 1001, 200, 100, "are you there", "2026-10-01T07:10:00.000Z", "unread", "app"),
        (4, 1002, 10, None, "group hi", "2026-10-01T06:00:00.000Z", "unread", "app"),
        (5, 1003, 200, None, "wa msg", "2026-10-01T05:00:00.000Z", "unread", "whatsapp"),
    ]
    for mid, gid, uid, rid, content, created, status, platform in messages:
        item = {
            "id": {"N": str(mid)}, "group_id": {"N": str(gid)}, "user_id": {"N": str(uid)}, "sender_id": {"N": str(uid)},
            "content": {"S": content}, "created_at": {"S": created}, "read_status": {"S": status}, "platform": {"S": platform},
            "is_deleted": {"N": "0"}, "message_type": {"S": "text"}, "media_type": {"S": "text"}, "type": {"S": "SINGLE"},
            "content_en": {"NULL": True}, "role_type": {"S": "member"},
        }
        if rid is not None:
            item["receiver_id"] = {"N": str(rid)}
        client.put_item(TableName=env.MESSAGE_TABLE, Item=item)
    client.put_item(TableName=env.COUNTER_TABLE, Item={"tableName": {"S": env.MESSAGE_TABLE}, "count": {"N": "5"}})


def seed_all() -> None:
    seed_mysql()
    seed_dynamo()


def flush_redis() -> None:
    import redis

    for db in (env.NODE_REDIS_DB, env.PY_REDIS_DB):
        redis.Redis(host=env.REDIS_HOST, port=env.REDIS_PORT, db=db, protocol=2).flushdb()


if __name__ == "__main__":
    seed_all()
    flush_redis()
    print("seeded")
