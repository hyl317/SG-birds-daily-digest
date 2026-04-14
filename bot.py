"""
SG Birds search bot.

Long-running Telethon process. DM the bot in private chat and send:
  - A species or SG location as text → searches the local SQLite archive.
  - A non-SG location as text → falls back to eBird (10 km, last 30 days).
  - A shared Telegram location pin → eBird results at those coordinates.

Setup:
  1. Create a bot via @BotFather, get a token
  2. Add BOT_TOKEN=... to .env
  3. (Optional) Add EBIRD_API_KEY=... to .env for the non-SG fallback
  4. Run: python bot.py
"""

import asyncio
import json
import os
import re
import urllib.parse

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import (
    MessageEntityBlockquote,
    MessageEntityBold,
    MessageEntityTextUrl,
    MessageMediaGeo,
    MessageMediaGeoLive,
)

import db
import ebird

load_dotenv()

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_SESSION_PATH = os.path.join(PROJECT_DIR, "session", "sg_birds_bot")

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
EBIRD_API_KEY = os.environ.get("EBIRD_API_KEY")

EBIRD_DIST_KM = 10
EBIRD_BACK_DAYS = 30

# Optional: source group ID, used to build deep links back to original messages.
# Read from group_config.json if available so links work in the inline results.
GROUP_ID = None
group_config_path = os.path.join(PROJECT_DIR, "group_config.json")
if os.path.exists(group_config_path):
    with open(group_config_path) as f:
        GROUP_ID = json.load(f).get("group_id")


def load_acronym_map():
    """
    Parse acronyms.md into {ACRONYM: expansion} dict (uppercase keys).
    Used to expand acronyms at query time so e.g. "SBG" matches "Singapore Botanic Gardens".
    """
    path = os.path.join(PROJECT_DIR, "acronyms.md")
    if not os.path.exists(path):
        return {}
    result = {}
    line_re = re.compile(r"^\s*-\s*([A-Za-z][\w-]*)\s*=\s*(.+?)\s*$")
    with open(path) as f:
        for line in f:
            m = line_re.match(line)
            if not m:
                continue
            key = m.group(1).upper()
            value = m.group(2)
            # Strip parenthetical suffixes like "(unconfirmed)"
            value = re.sub(r"\s*\([^)]*\)\s*$", "", value).strip()
            if value:
                result[key] = value
    return result


ACRONYM_MAP = load_acronym_map()


def maps_link(location):
    """Google Maps search URL for a free-text location. 'Singapore' is prepended
    so ambiguous names resolve to the local match."""
    if not location:
        return None
    query = urllib.parse.quote_plus(f"{location}, Singapore")
    return f"https://www.google.com/maps/search/?api=1&query={query}"


def deep_link(source_msg_id):
    """Build a t.me deep link to a message in the source group, if possible."""
    if not GROUP_ID or not source_msg_id:
        return None
    # Telegram's private group/channel link format strips the -100 prefix
    gid_str = str(GROUP_ID)
    if gid_str.startswith("-100"):
        gid_str = gid_str[4:]
    elif gid_str.startswith("-"):
        gid_str = gid_str[1:]
    return f"https://t.me/c/{gid_str}/{source_msg_id}"


bot = TelegramClient(BOT_SESSION_PATH, API_ID, API_HASH).start(bot_token=BOT_TOKEN)


WELCOME = (
    "👋 Hi! I'm the SG Birds search bot.\n\n"
    "Send me:\n"
    "  • A species or SG location — I'll search the SG Birds group archive (last 90 days).\n"
    "  • A non-SG place name (e.g. <code>foster city</code>, <code>taipei</code>) — "
    "I'll fetch recent eBird sightings within 10 km, last 30 days.\n"
    "  • A 📍 location pin (attach → location) — same eBird lookup at your coordinates.\n\n"
    "Commands: /help"
)

HELP = (
    "<b>How to use</b>\n\n"
    "<b>Text queries</b>\n"
    "• Species (<code>fairy pitta</code>) or SG location (<code>sungei buloh</code>) "
    "→ searches the SG Birds group archive.\n"
    "• Non-SG place (<code>foster city</code>, <code>taipei</code>) "
    "→ eBird, 10 km radius, last 30 days.\n"
    "Search is fuzzy — partial words work.\n\n"
    "<b>Location pin</b>\n"
    "Tap the 📎 attach button → Location → Send My Current Location. "
    "I'll fetch eBird sightings near those coordinates.\n\n"
    "The SG archive covers a rolling 90-day window. Each SG result links back to "
    "the original group message (only works if you're a member of the group)."
)


def _utf16_len(s):
    """Telegram entity offsets are in UTF-16 code units, not Python characters."""
    return len(s.encode("utf-16-le")) // 2


class MessageBuilder:
    """Accumulates plain text and Telegram MessageEntity objects with correct UTF-16 offsets."""

    def __init__(self):
        self._chunks = []
        self.entities = []
        self.offset = 0  # UTF-16 code units

    def add(self, text):
        if not text:
            return
        self._chunks.append(text)
        self.offset += _utf16_len(text)

    def add_bold(self, text):
        start = self.offset
        self.add(text)
        self.entities.append(MessageEntityBold(start, _utf16_len(text)))

    def add_link(self, text, url):
        start = self.offset
        self.add(text)
        self.entities.append(MessageEntityTextUrl(start, _utf16_len(text), url))

    def add_blockquote_from(self, start_offset, collapsed=True):
        """Wrap everything from start_offset to current offset in a (collapsed) blockquote."""
        length = self.offset - start_offset
        if length > 0:
            self.entities.append(
                MessageEntityBlockquote(start_offset, length, collapsed=collapsed)
            )

    def snapshot(self):
        return (list(self._chunks), self.offset, len(self.entities))

    def restore(self, snap):
        chunks, offset, n_entities = snap
        self._chunks = chunks
        self.offset = offset
        self.entities = self.entities[:n_entities]

    @property
    def text(self):
        return "".join(self._chunks)


def _append_one(builder, row):
    builder.add("• ")
    builder.add_bold(row["species"])
    builder.add(f" — {row['date']}")
    count = row.get("_count")
    if count and count > 1:
        builder.add(f" ({count} sightings within last 3 months)")
    if row["location"]:
        builder.add("\n   📍 ")
        builder.add_link(row["location"], maps_link(row["location"]))
    if row["observer"]:
        builder.add(f"\n   👤 {row['observer']}")
    if row["notes"]:
        builder.add(f"\n   {row['notes']}")
    link = deep_link(row["source_msg_id"])
    if link:
        builder.add("\n   ")
        builder.add_link("View original", link)


def _coord_maps_link(lat, lng):
    return f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"


def _append_ebird_row(builder, row):
    builder.add("• ")
    builder.add_bold(row["species"])
    builder.add(f" — {row['date']}")
    count = row.get("_count")
    if count and count > 1:
        builder.add(f" ({count} sightings in last {EBIRD_BACK_DAYS} days)")
    howmany = row.get("count")
    if howmany:
        builder.add(f" [x{howmany}]")
    if row.get("location") and row.get("lat") is not None and row.get("lng") is not None:
        builder.add("\n   📍 ")
        builder.add_link(row["location"], _coord_maps_link(row["lat"], row["lng"]))
    if row.get("notable"):
        builder.add("\n   ⭐ notable")


def build_ebird_messages(header_place, rows, visible=5):
    """Build reply messages for eBird results. Mirrors build_chat_messages."""
    if not rows:
        b = MessageBuilder()
        b.add("No eBird sightings within ")
        b.add(f"{EBIRD_DIST_KM} km of ")
        b.add_bold(header_place)
        b.add(f" in the last {EBIRD_BACK_DAYS} days.")
        return [_finalize(b)]

    messages = []
    n = len(rows)
    b = MessageBuilder()
    b.add_bold(f"{n} species near {header_place}")
    b.add(f"\n(eBird · {EBIRD_DIST_KM} km · last {EBIRD_BACK_DAYS} days)\n\n")
    for row in rows[:visible]:
        _append_ebird_row(b, row)
        b.add("\n\n")

    if n > visible:
        b.add(f"Tap to expand {n - visible} more:\n")
        next_idx = _pack_ebird_into_blockquote(b, rows, visible)
    else:
        next_idx = n
    messages.append(_finalize(b))

    while next_idx < n:
        b = MessageBuilder()
        b.add_bold("More results (cont'd):")
        b.add("\n")
        prev_idx = next_idx
        next_idx = _pack_ebird_into_blockquote(b, rows, next_idx)
        if next_idx == prev_idx:
            next_idx += 1
        messages.append(_finalize(b))

    return messages


def _pack_ebird_into_blockquote(builder, rows, start_idx):
    if start_idx >= len(rows):
        return start_idx
    bq_start = builder.offset
    idx = start_idx
    while idx < len(rows):
        snap = builder.snapshot()
        _append_ebird_row(builder, rows[idx])
        if idx < len(rows) - 1:
            builder.add("\n\n")
        if len(builder.text) > MAX_MSG_CHARS:
            builder.restore(snap)
            break
        idx += 1
    if builder.offset > bq_start:
        builder.add_blockquote_from(bq_start, collapsed=True)
    return idx


def maybe_dedupe_by_species(rows, threshold=5):
    """
    If results contain many distinct species, treat as a location-style search and
    collapse to one row per species (the most recent sighting), with a sighting count.
    Otherwise return rows unchanged.
    """
    distinct = {r["species"] for r in rows}
    if len(distinct) <= threshold:
        return rows

    by_species = {}  # preserves insertion order = date-desc order
    counts = {}
    for r in rows:
        sp = r["species"]
        counts[sp] = counts.get(sp, 0) + 1
        if sp not in by_species:
            by_species[sp] = dict(r)

    deduped = []
    for sp, r in by_species.items():
        r["_count"] = counts[sp]
        deduped.append(r)
    return deduped


MAX_MSG_CHARS = 3800  # Telegram limit is 4096; leave headroom for safety


def _finalize(builder):
    """Trim trailing whitespace and clamp entities to the trimmed text."""
    text = builder.text.rstrip()
    trimmed_utf16 = _utf16_len(text)
    entities = []
    for e in builder.entities:
        if e.offset >= trimmed_utf16 or e.length <= 0:
            continue
        if e.offset + e.length > trimmed_utf16:
            e.length = trimmed_utf16 - e.offset
        entities.append(e)
    return text, entities


def _pack_into_blockquote(builder, rows, start_idx):
    """
    Append rows[start_idx:] into the builder wrapped in a collapsed blockquote,
    fitting as many as possible under MAX_MSG_CHARS. Returns the next index
    that didn't fit (== len(rows) if all fit).
    """
    if start_idx >= len(rows):
        return start_idx

    bq_start = builder.offset
    idx = start_idx
    while idx < len(rows):
        snap = builder.snapshot()
        _append_one(builder, rows[idx])
        if idx < len(rows) - 1:
            builder.add("\n\n")
        if len(builder.text) > MAX_MSG_CHARS:
            builder.restore(snap)
            break
        idx += 1
    if builder.offset > bq_start:
        builder.add_blockquote_from(bq_start, collapsed=True)
    return idx


def build_chat_messages(query, rows, visible=5):
    """
    Build one or more chat reply messages. Returns a list of (text, entities) tuples.

    The first message contains a header + the first `visible` results in plain view.
    Any remaining results are placed inside collapsed blockquotes — Telegram shows
    a short preview and an expand affordance, keeping the message visually short
    until the user taps to expand.

    If too many results to fit in one Telegram message (4096 chars), the overflow
    spills into additional continuation messages, each with its own collapsed
    blockquote.
    """
    if not rows:
        b = MessageBuilder()
        b.add("No sightings found for ")
        b.add_bold(query)
        b.add(" in the last 90 days.")
        return [_finalize(b)]

    messages = []
    n = len(rows)

    # First message: header + visible results
    b = MessageBuilder()
    b.add_bold(f"{n} result{'s' if n != 1 else ''} for '{query}'")
    b.add("\n\n")
    for row in rows[:visible]:
        _append_one(b, row)
        b.add("\n\n")

    if n > visible:
        b.add(f"Tap to expand {n - visible} more:\n")
        next_idx = _pack_into_blockquote(b, rows, visible)
    else:
        next_idx = n

    messages.append(_finalize(b))

    # Continuation messages for overflow
    while next_idx < n:
        b = MessageBuilder()
        b.add_bold(f"More results (cont'd):")
        b.add("\n")
        prev_idx = next_idx
        next_idx = _pack_into_blockquote(b, rows, next_idx)
        if next_idx == prev_idx:
            # Single row too large to fit — skip it to avoid infinite loop
            next_idx += 1
        messages.append(_finalize(b))

    return messages


@bot.on(events.NewMessage(pattern=r"^/start"))
async def on_start(event):
    if not event.is_private:
        return
    await event.reply(WELCOME, parse_mode="html")


@bot.on(events.NewMessage(pattern=r"^/help"))
async def on_help(event):
    if not event.is_private:
        return
    await event.reply(HELP, parse_mode="html")


async def _reply_messages(event, messages):
    for msg_text, entities in messages:
        await event.reply(msg_text, formatting_entities=entities, link_preview=False)


async def _handle_ebird_at(event, lat, lng, place_label):
    """Fetch eBird results at (lat, lng) and reply. place_label is shown in the header."""
    if not EBIRD_API_KEY:
        await event.reply(
            "eBird lookups aren't configured on this bot. Set EBIRD_API_KEY in .env to enable them.",
            link_preview=False,
        )
        return
    rows = await asyncio.to_thread(
        ebird.recent_near, lat, lng, EBIRD_API_KEY, EBIRD_DIST_KM, EBIRD_BACK_DAYS
    )
    if rows is None:
        await event.reply("eBird lookups aren't configured. Set EBIRD_API_KEY in .env.")
        return
    grouped = ebird.group_by_species(rows)
    messages = build_ebird_messages(place_label, grouped, visible=5)
    await _reply_messages(event, messages)


@bot.on(events.NewMessage)
async def on_message(event):
    # Only respond in private (DM) chats
    if not event.is_private:
        return

    # Shared location pin → eBird at those coordinates
    media = event.message.media if event.message else None
    if isinstance(media, (MessageMediaGeo, MessageMediaGeoLive)):
        geo = media.geo
        lat = float(geo.lat)
        lng = float(geo.long)
        place = await asyncio.to_thread(ebird.reverse_geocode, lat, lng) or f"{lat:.4f},{lng:.4f}"
        await _handle_ebird_at(event, lat, lng, place)
        return

    text = (event.raw_text or "").strip()
    # Skip empty messages and commands (handled separately)
    if not text or text.startswith("/"):
        return

    # Text query: geocode first. If it resolves outside SG, route to eBird.
    geo_result = await asyncio.to_thread(ebird.geocode, text)
    if geo_result is not None:
        lat, lng, display_name = geo_result
        if not ebird.is_in_sg(lat, lng):
            await _handle_ebird_at(event, lat, lng, display_name)
            return

    # Default path: local SG archive search
    rows = db.search(text, limit=100, acronym_map=ACRONYM_MAP)
    rows = maybe_dedupe_by_species(rows)
    messages = build_chat_messages(text, rows, visible=5)
    await _reply_messages(event, messages)


HEALTH_INTERVAL = 120  # seconds between get_me() pings
HEALTH_TIMEOUT = 30    # seconds to wait for a response


async def _health_loop():
    """
    Periodically ping Telegram via get_me(). If the call hangs or raises, the
    Telethon connection is stuck (seen in practice after "Connection reset by
    peer" storms) — exit hard so launchd's KeepAlive respawns us cleanly.
    """
    while True:
        await asyncio.sleep(HEALTH_INTERVAL)
        try:
            await asyncio.wait_for(bot.get_me(), timeout=HEALTH_TIMEOUT)
        except BaseException as e:
            print(f"Health check failed ({e!r}) — exiting for respawn", flush=True)
            os._exit(1)


async def _run():
    print("SG Birds bot starting...", flush=True)
    print(f"DB: {db.DEFAULT_DB_PATH} ({db.count()} sightings)", flush=True)
    print(f"Modes: DM text + GPS pins · eBird={'on' if EBIRD_API_KEY else 'off'}", flush=True)
    asyncio.create_task(_health_loop())
    await bot.run_until_disconnected()
    print("Bot disconnected — exiting for respawn", flush=True)
    os._exit(1)


def main():
    bot.loop.run_until_complete(_run())


if __name__ == "__main__":
    main()
