"""
SG Birds Daily Summary
Reads the "SG Birds (sightings & live update)" Telegram group,
summarizes bird species and locations via Claude, and sends the digest to your Telegram Saved Messages.

First run: execute manually — Telethon will prompt for phone number + code.
After that, schedule via cron (see bottom of file for example).
"""

import json
import os
import plistlib
import re
import subprocess
import sys
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import Chat, Channel
import anthropic

import db

load_dotenv()

SG_TZ = ZoneInfo("Asia/Singapore")
MAX_MESSAGE_CHARS = 600_000  # Haiku 4.5 has 200K tokens (~800K chars); 600K leaves room for the prompt
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
ACRONYMS_PATH = os.path.join(PROJECT_DIR, "acronyms.md")
CONFIG_PATH = os.path.join(PROJECT_DIR, "config.json")
PLIST_NAME = "com.hyl.sgbirds-summary.plist"
PLIST_PATH = os.path.join(PROJECT_DIR, PLIST_NAME)
LAUNCHD_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{PLIST_NAME}")

FREQUENCY_OPTIONS = [
    ("Every 6 hours (4x/day)", 6),
    ("Every 8 hours (3x/day)", 8),
    ("Every 12 hours (2x/day)", 12),
    ("Every 24 hours (daily)", 24),
]


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return None


def save_config(config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def generate_plist(hour, minute, frequency_hours):
    """Generate the launchd plist based on schedule config."""
    python_path = os.path.join(sys.prefix, "bin", "python")
    script_path = os.path.join(PROJECT_DIR, "sg_birds_summary.py")

    plist = {
        "Label": "com.hyl.sgbirds-summary",
        "ProgramArguments": [python_path, script_path],
        "WorkingDirectory": PROJECT_DIR,
        "StandardOutPath": "/tmp/sg_birds_summary.log",
        "StandardErrorPath": "/tmp/sg_birds_summary.log",
    }

    if frequency_hours == 24:
        # Run once a day at the specified time
        plist["StartCalendarInterval"] = {"Hour": hour, "Minute": minute}
    else:
        # Run every N hours
        plist["StartInterval"] = frequency_hours * 3600

    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(plist, f)


def install_schedule():
    """Symlink and load the plist into launchd."""
    # Unload if already loaded
    subprocess.run(["launchctl", "unload", LAUNCHD_PATH], capture_output=True)
    # Symlink
    if os.path.islink(LAUNCHD_PATH) or os.path.exists(LAUNCHD_PATH):
        os.remove(LAUNCHD_PATH)
    os.symlink(PLIST_PATH, LAUNCHD_PATH)
    # Load
    subprocess.run(["launchctl", "load", LAUNCHD_PATH], capture_output=True)
    print("Schedule installed in launchd.")


async def run_setup(client):
    """Interactive first-time setup: pick group, time, and frequency."""
    # --- Group selection ---
    print("\nFetching your groups...")
    groups = []
    async for dialog in client.iter_dialogs():
        if isinstance(dialog.entity, (Chat, Channel)) and dialog.is_group:
            groups.append((dialog.entity.id, dialog.name))

    if not groups:
        raise ValueError("You are not a member of any groups.")

    print("\nYour Telegram groups:")
    for i, (gid, name) in enumerate(groups, 1):
        print(f"  {i}. {name}")

    while True:
        try:
            choice = int(input(f"\nSelect a group (1-{len(groups)}): "))
            if 1 <= choice <= len(groups):
                break
        except ValueError:
            pass
        print("Invalid choice, try again.")

    group_id, group_name = groups[choice - 1]
    print(f"Selected: {group_name}")

    # --- Time selection ---
    while True:
        time_str = input("\nWhat time should the summary be sent? (HH:MM, 24h format, e.g. 21:00): ").strip()
        try:
            parts = time_str.split(":")
            hour, minute = int(parts[0]), int(parts[1])
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                break
        except (ValueError, IndexError):
            pass
        print("Invalid time, try again.")

    # --- Frequency selection ---
    print("\nHow often should the summary be sent?")
    for i, (label, _) in enumerate(FREQUENCY_OPTIONS, 1):
        print(f"  {i}. {label}")

    while True:
        try:
            freq_choice = int(input(f"\nSelect frequency (1-{len(FREQUENCY_OPTIONS)}): "))
            if 1 <= freq_choice <= len(FREQUENCY_OPTIONS):
                break
        except ValueError:
            pass
        print("Invalid choice, try again.")

    freq_label, freq_hours = FREQUENCY_OPTIONS[freq_choice - 1]

    # --- Send destination ---
    print("\nWhere should the summary be sent?")
    print("  1. Saved Messages (just you)")
    print("  2. A group or channel")

    while True:
        try:
            dest_choice = int(input("\nSelect destination (1-2): "))
            if dest_choice in (1, 2):
                break
        except ValueError:
            pass
        print("Invalid choice, try again.")

    send_to = None
    send_to_name = "Saved Messages"
    if dest_choice == 2:
        # Let user pick from their groups/channels
        print("\nYour groups and channels:")
        destinations = []
        async for dialog in client.iter_dialogs():
            if isinstance(dialog.entity, (Chat, Channel)):
                destinations.append((dialog.entity.id, dialog.name))

        for i, (did, name) in enumerate(destinations, 1):
            print(f"  {i}. {name}")

        while True:
            try:
                dest_pick = int(input(f"\nSelect destination (1-{len(destinations)}): "))
                if 1 <= dest_pick <= len(destinations):
                    break
            except ValueError:
                pass
            print("Invalid choice, try again.")

        send_to, send_to_name = destinations[dest_pick - 1]

    config = {
        "group_id": group_id,
        "group_name": group_name,
        "summary_hour": hour,
        "summary_minute": minute,
        "frequency_hours": freq_hours,
        "send_to": send_to,
        "send_to_name": send_to_name,
    }
    save_config(config)
    generate_plist(hour, minute, freq_hours)
    install_schedule()

    print(f"\nSetup complete!")
    print(f"  Group:     {group_name}")
    print(f"  Send to:   {send_to_name}")
    print(f"  Time:      {hour:02d}:{minute:02d}")
    print(f"  Frequency: {freq_label}")
    print(f"\nTo reconfigure, delete config.json and run again.")

    return config


def get_sender_name(entity):
    """Extract a display name from a message sender."""
    if not entity:
        return "Unknown"
    first = getattr(entity, "first_name", "") or ""
    last = getattr(entity, "last_name", "") or ""
    name = f"{first} {last}".strip()
    return name or "Unknown"


async def fetch_messages(client, group_id, start_utc, end_utc):
    """Fetch text messages from the group within the given UTC time window."""
    entity = await client.get_entity(group_id)

    # First pass: collect all messages and track which ones are replies
    raw_messages = []
    reply_ids = set()
    async for msg in client.iter_messages(entity, offset_date=end_utc, reverse=False):
        if msg.date.replace(tzinfo=ZoneInfo("UTC")) < start_utc:
            break
        text = msg.text or msg.raw_text
        if not text:
            continue
        raw_messages.append(msg)
        if msg.reply_to and msg.reply_to.reply_to_msg_id:
            reply_ids.add(msg.reply_to.reply_to_msg_id)

    # Fetch quoted messages that we need context for
    quoted = {}
    for reply_id in reply_ids:
        try:
            original = await client.get_messages(entity, ids=reply_id)
            if original and (original.text or original.raw_text):
                quote_text = original.text or original.raw_text
                # Truncate long quotes
                if len(quote_text) > 100:
                    quote_text = quote_text[:100] + "..."
                quoted[reply_id] = {
                    "sender": get_sender_name(original.sender),
                    "text": quote_text,
                }
        except Exception:
            pass

    messages = []
    for msg in raw_messages:
        text = msg.text or msg.raw_text
        sender = get_sender_name(msg.sender)
        reply_context = ""
        if msg.reply_to and msg.reply_to.reply_to_msg_id:
            q = quoted.get(msg.reply_to.reply_to_msg_id)
            if q:
                reply_context = f" (replying to {q['sender']}: \"{q['text']}\")"

        sg_dt = msg.date.astimezone(SG_TZ)
        messages.append({
            "msg_id": msg.id,
            "date": sg_dt.strftime("%Y-%m-%d"),
            "time": sg_dt.strftime("%H:%M"),
            "sender": sender,
            "reply_context": reply_context,
            "text": text,
        })

    messages.reverse()  # chronological order
    return messages


def load_acronyms():
    """Load known acronyms from acronyms.md."""
    if not os.path.exists(ACRONYMS_PATH):
        return ""
    with open(ACRONYMS_PATH) as f:
        return f.read()


def format_messages_for_claude(messages):
    """Format the message list for Claude with msg IDs and full dates inline."""
    return "\n".join(
        f"[msg_id={m['msg_id']}] [{m['date']} {m['time']}] {m['sender']}{m['reply_context']}: {m['text']}"
        for m in messages
    )


def summarize_with_claude(messages):
    """
    Use Claude to extract bird species and locations from the messages.

    `messages` is a list of dicts (from fetch_messages).
    Returns a tuple (html_summary, sightings_list).

    sightings_list is a list of dicts with keys:
        date, time, species, location, observer, notes, source_msg_id
    or None if structured parsing failed (the html_summary is still returned).
    """
    client = anthropic.Anthropic()

    messages_text = format_messages_for_claude(messages)
    if len(messages_text) > MAX_MESSAGE_CHARS:
        messages_text = messages_text[:MAX_MESSAGE_CHARS]
        print(f"Warning: truncated to {MAX_MESSAGE_CHARS} chars")

    acronyms_text = load_acronyms()
    acronyms_section = ""
    if acronyms_text.strip():
        acronyms_section = f"""
Known acronyms (use these to expand abbreviations in the messages):
{acronyms_text}
"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": f"""Analyze these bird sighting messages from the "SG Birds" Telegram group (Singapore).
{acronyms_section}
IMPORTANT: Only report bird species that are explicitly named or abbreviated in the text messages. Do NOT infer or guess species from photos, descriptions, or behavioral clues. If a message contains only a photo with no text identifying the bird, skip it. If an acronym is ambiguous and not in the known acronyms list, note it as "unidentified (acronym: XX)" rather than guessing.
Only include species with confirmed sighting details (location, time, or observer). Skip species that are merely mentioned in questions, queries, or general discussion without an actual sighting being reported.

CRITICAL — species accuracy: The "species" field for each sighting MUST be the exact bird that was actually observed in that specific message. Do NOT carry over a species name from earlier messages, the surrounding conversation context, or the chat's general topic. If a message describes seeing "Hooded Pitta and Blue-winged Pitta", create one sighting record per species — do NOT label it as "Fairy Pitta" just because Fairy Pitta is a popular topic in the group. If you cannot determine the species from the message itself, skip the sighting entirely rather than guessing.

Each message is prefixed with [msg_id=N] [YYYY-MM-DD HH:MM] sender: text. Use the msg_id to reference which message each sighting came from.

Return your response in TWO parts:

PART 1 — A JSON code block (fenced with ```json ... ```) containing an array of structured sighting objects. One object per (species, location) sighting. Schema:
[
  {{
    "date": "YYYY-MM-DD",            // sighting date in Singapore time
    "species": "Common Name",         // expand acronyms
    "location": "Full Location Name" or null,
    "observer": "Sender Name" or null,
    "notes": "any details (count, behavior, etc.)" or null,
    "source_msg_id": 12345            // the msg_id from the source message
  }}
]
Use the same skip rules: only include confirmed sightings with at least a location or observer.

PART 2 — After the JSON block, the human-readable summary, formatted for Telegram HTML:
- For each bird species mentioned, list location(s), time, and notable details
- Group by species, sorted alphabetically
- At the top, a quick stats line: total species count, total sightings, highlights
- If a location appears frequently, note how many sightings occurred there
- Wrap each bird's common name in <b>bold</b> tags (e.g. <b>Oriental Pied Hornbill</b>)
- Do NOT bold any other text (scientific names, locations, times, details, headings, stats)
- Use plain text for everything else

Messages:
{messages_text}"""
        }],
    )

    raw = next(b.text for b in response.content if b.type == "text")
    return parse_claude_response(raw)


def parse_claude_response(raw):
    """Extract the JSON sighting list and the HTML summary from Claude's response."""
    match = re.search(r"```json\s*(.*?)\s*```", raw, re.DOTALL)
    sightings = None
    html = raw
    if match:
        json_text = match.group(1)
        try:
            sightings = json.loads(json_text)
            if not isinstance(sightings, list):
                sightings = None
            else:
                # Strip the JSON block from the HTML so it doesn't get sent to Telegram
                html = (raw[:match.start()] + raw[match.end():]).strip()
        except json.JSONDecodeError as e:
            print(f"Warning: failed to parse sightings JSON: {e}")
            sightings = None
    else:
        print("Warning: no JSON sightings block found in Claude response")

    return html, sightings


def extract_acronyms(messages_text):
    """Ask Claude to identify unknown acronyms and append new ones to acronyms.md."""
    client = anthropic.Anthropic()
    known = load_acronyms()

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"""Look at these bird sighting messages from a Singapore birding Telegram group.
Identify any acronyms or abbreviations used (for locations, bird species, or birding terms).

Already known acronyms (do NOT repeat these):
{known}

For each NEW acronym found, output one line in this exact format:
- ACRONYM = Full Meaning

Only include acronyms you are reasonably confident about. If you're unsure of the meaning, append "(unconfirmed)" after it.
If there are no new acronyms, output exactly: NONE

Messages:
{messages_text}"""
        }],
    )

    result = next(b.text for b in response.content if b.type == "text").strip()
    if result == "NONE":
        return

    # Append new acronyms to the file
    with open(ACRONYMS_PATH, "a") as f:
        f.write(f"\n<!-- Auto-detected {datetime.now(SG_TZ).strftime('%d %b %Y')} -->\n")
        f.write(result + "\n")

    # Retroactively fix "unidentified (acronym: X)" rows whose X is now known.
    fixed = db.backfill_orphan_acronyms(db.parse_acronym_map(ACRONYMS_PATH))
    if fixed:
        print(f"Backfilled {fixed} orphan acronym row(s)")


async def send_telegram(client, subject, window, body, send_to=None):
    """Send the summary to Telegram. Defaults to Saved Messages if send_to is None."""
    if send_to is None:
        recipient = await client.get_me()
    else:
        recipient = await client.get_entity(send_to)
    message = f"<b>{subject}</b>\n{window}\n\n{body}"
    # Telegram has a 4096-char limit per message; split if needed
    if len(message) <= 4096:
        await client.send_message(recipient, message, parse_mode="html")
    else:
        chunks = [message[i:i + 4096] for i in range(0, len(message), 4096)]
        for chunk in chunks:
            await client.send_message(recipient, chunk, parse_mode="html")


async def main():
    session_path = os.path.join(PROJECT_DIR, "session", "sg_birds")
    async with TelegramClient(
        session_path,
        int(os.environ["TELEGRAM_API_ID"]),
        os.environ["TELEGRAM_API_HASH"],
    ) as tg_client:
        config = load_config()
        if config is None:
            config = await run_setup(tg_client)

        # Prune sightings older than the retention window before adding today's
        pruned = db.prune_older_than(90)
        if pruned:
            print(f"Pruned {pruned} sightings older than 90 days")

        # Compute time window based on configured schedule
        now = datetime.now(SG_TZ)
        freq_hours = config["frequency_hours"]

        # Look back freq_hours from now
        end_time = now.replace(second=0, microsecond=0)
        start_time = end_time - timedelta(hours=freq_hours)

        start_utc = start_time.astimezone(ZoneInfo("UTC"))
        end_utc = end_time.astimezone(ZoneInfo("UTC"))

        print(f"Fetching messages from {start_time} to {end_time} (SGT)")

        messages = await fetch_messages(tg_client, config["group_id"], start_utc, end_utc)

        print(f"Found {len(messages)} messages")

        if not messages:
            print("No messages in this window — skipping.")
            return

        summary, sightings = summarize_with_claude(messages)

        if sightings:
            inserted = db.insert_sightings(sightings)
            print(f"Persisted {inserted} new sightings to DB (total: {db.count()})")
        else:
            print("No structured sightings parsed — skipping DB write")

        # Acronym extraction still uses the simple text format
        acronyms_text = format_messages_for_claude(messages)
        if len(acronyms_text) > MAX_MESSAGE_CHARS:
            acronyms_text = acronyms_text[:MAX_MESSAGE_CHARS]
        extract_acronyms(acronyms_text)

        window_str = (
            f"{start_time.strftime('%-d %B %H:%M')} – {end_time.strftime('%-d %B %H:%M')}"
        )
        subject = "SG Birds Daily Summary"

        send_to = config.get("send_to")
        await send_telegram(tg_client, subject, window_str, summary, send_to=send_to)
        dest_name = config.get("send_to_name", "Saved Messages")
        print(f"Summary sent to {dest_name}: {subject}")


if __name__ == "__main__":
    asyncio.run(main())

# Scheduled via macOS launchd — see com.hyl.sgbirds-summary.plist
# Runs daily at 9:05 PM. If the Mac is asleep, launchd runs it on wake.
