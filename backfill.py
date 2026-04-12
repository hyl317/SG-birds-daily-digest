"""
One-time backfill: fetch the last 90 days of messages from the SG Birds group,
extract structured sightings via Claude, and populate sightings.db.

Idempotent — safe to re-run. Existing rows are deduped by (date, species, location, source_msg_id).

Usage:
    python backfill.py [--days 90] [--chunk-days 1]
"""

import argparse
import asyncio
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telethon import TelegramClient

import db
from sg_birds_summary import (
    PROJECT_DIR,
    SG_TZ,
    fetch_messages,
    load_config,
    summarize_with_claude,
)

load_dotenv()


async def backfill(days: int, chunk_days: int):
    config = load_config()
    if config is None:
        raise SystemExit("No config.json found. Run sg_birds_summary.py first to set up.")

    session_path = os.path.join(PROJECT_DIR, "session", "sg_birds")
    async with TelegramClient(
        session_path,
        int(os.environ["TELEGRAM_API_ID"]),
        os.environ["TELEGRAM_API_HASH"],
    ) as tg_client:
        now = datetime.now(SG_TZ).replace(second=0, microsecond=0)
        oldest = now - timedelta(days=days)

        print(f"Backfilling from {oldest.date()} to {now.date()} "
              f"({days} days, {chunk_days}-day chunks)")

        total_inserted = 0
        chunk_end = now
        while chunk_end > oldest:
            chunk_start = max(chunk_end - timedelta(days=chunk_days), oldest)
            start_utc = chunk_start.astimezone(ZoneInfo("UTC"))
            end_utc = chunk_end.astimezone(ZoneInfo("UTC"))

            print(f"\n--- Chunk {chunk_start.date()} → {chunk_end.date()} ---")
            messages = await fetch_messages(tg_client, config["group_id"], start_utc, end_utc)
            print(f"  Fetched {len(messages)} messages")

            if messages:
                try:
                    _, sightings = summarize_with_claude(messages)
                except Exception as e:
                    print(f"  Claude call failed: {e}")
                    sightings = None

                if sightings:
                    inserted = db.insert_sightings(sightings)
                    total_inserted += inserted
                    print(f"  Inserted {inserted} new sightings (chunk total: {len(sightings)})")
                else:
                    print("  No sightings parsed for this chunk")

            chunk_end = chunk_start

        print(f"\nDone. Inserted {total_inserted} new sightings. DB now contains {db.count()} rows.")


def main():
    parser = argparse.ArgumentParser(description="Backfill SG Birds sightings DB")
    parser.add_argument("--days", type=int, default=90, help="How many days back to fetch (default: 90)")
    parser.add_argument("--chunk-days", type=int, default=1,
                        help="Days per Claude call (default: 1, keeps each chunk small)")
    args = parser.parse_args()

    asyncio.run(backfill(args.days, args.chunk_days))


if __name__ == "__main__":
    main()
