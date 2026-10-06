"""One-time backfill of group_participants.unread_messages_count from DynamoDB
(port of scripts/backfill-unread-counts.js). Idempotent; only touches
``unread_messages_count``. Run from ONE instance.

  python -m scripts.backfill_unread_counts            # apply
  DRY_RUN=true python -m scripts.backfill_unread_counts   # report only
"""

from __future__ import annotations

import asyncio
import os
import sys

from app.db import raw
from app.db.session import dispose_engine
from app.repositories import aws_message as aws_message_repo

DRY_RUN = os.environ.get("DRY_RUN") == "true"
CONCURRENCY = int(os.environ.get("BACKFILL_CONCURRENCY") or 5) or 5


async def process_user(user_id: int) -> dict:
    by_group = await aws_message_repo.get_unread_counts_by_receiver(user_id)
    if DRY_RUN:
        return {"user_id": user_id, "groupsWithUnread": len(by_group), "total": sum(by_group.values())}
    await raw.execute("UPDATE group_participants SET unread_messages_count = 0 WHERE user_id = :u", {"u": user_id})
    for group_id, count in by_group.items():
        if count > 0:
            await raw.execute(
                "UPDATE group_participants SET unread_messages_count = :c WHERE user_id = :u AND group_id = :g AND is_deleted = 0",
                {"c": count, "u": user_id, "g": group_id},
            )
    return {"user_id": user_id, "groupsWithUnread": len(by_group)}


async def main() -> int:
    await raw.select("SELECT 1")
    print(f"Backfill starting{' (DRY RUN — no writes)' if DRY_RUN else ''}…")
    rows = await raw.select("SELECT DISTINCT user_id FROM group_participants WHERE is_deleted = 0")
    user_ids = [int(r["user_id"]) for r in rows]
    print(f"Found {len(user_ids)} distinct participant users.")
    done = 0
    for start in range(0, len(user_ids), CONCURRENCY):
        batch = user_ids[start : start + CONCURRENCY]
        results = await asyncio.gather(*(process_user(u) for u in batch), return_exceptions=True)
        for user_id, result in zip(batch, results, strict=True):
            if isinstance(result, Exception):
                print(f"  user {user_id}: ERROR {result}")
        done += len(batch)
        print(f"Progress: {done}/{len(user_ids)} users")
    print("Backfill complete.")
    await dispose_engine()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
