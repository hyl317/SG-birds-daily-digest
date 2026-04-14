"""
Smoke-test the bot by DMing /ping via the userbot account.

Usage:
    python smoketest.py [expected_short_sha]

Connects with the userbot session at session/sg_birds.session, sends `/ping`
to BOT_USERNAME, waits up to 15s for a reply containing "pong". If
`expected_short_sha` is given, also asserts the reply mentions that SHA so we
know the new code (not a zombie) is actually serving.

Exit codes:
    0  pong received (and SHA matches if checked)
    1  no pong within timeout
    2  config / startup error
    3  pong received but SHA mismatch (running stale code)
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
from telethon import TelegramClient, events

load_dotenv()

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
USERBOT_SESSION = os.path.join(PROJECT_DIR, "session", "sg_birds")

try:
    API_ID = int(os.environ["TELEGRAM_API_ID"])
    API_HASH = os.environ["TELEGRAM_API_HASH"]
except (KeyError, ValueError) as e:
    print(f"missing TELEGRAM_API_ID / TELEGRAM_API_HASH: {e}", file=sys.stderr)
    sys.exit(2)

BOT_USERNAME = os.environ.get("BOT_USERNAME")
if not BOT_USERNAME:
    print("BOT_USERNAME not set in .env (e.g. BOT_USERNAME=SGBirdsSearchBot)", file=sys.stderr)
    sys.exit(2)

TIMEOUT_SEC = 15


async def main():
    expected_sha = sys.argv[1].strip() if len(sys.argv) > 1 else None

    client = TelegramClient(USERBOT_SESSION, API_ID, API_HASH)
    await client.start()
    try:
        bot_entity = await client.get_entity(BOT_USERNAME)

        loop = asyncio.get_event_loop()
        future = loop.create_future()

        @client.on(events.NewMessage(from_users=bot_entity, incoming=True))
        async def _handler(event):
            text = event.raw_text or ""
            if "pong" in text.lower() and not future.done():
                future.set_result(text)

        await client.send_message(bot_entity, "/ping")

        try:
            reply = await asyncio.wait_for(future, timeout=TIMEOUT_SEC)
        except asyncio.TimeoutError:
            print(f"FAIL: no pong from {BOT_USERNAME} within {TIMEOUT_SEC}s", file=sys.stderr)
            return 1

        if expected_sha and expected_sha not in reply:
            print(
                f"FAIL: pong received but SHA mismatch — expected {expected_sha}, got: {reply!r}",
                file=sys.stderr,
            )
            return 3

        print(f"OK: {reply}")
        return 0
    finally:
        await client.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
