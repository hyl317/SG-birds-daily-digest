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

import secrets
from collections import OrderedDict

from dotenv import load_dotenv
from telethon import Button, TelegramClient, events
from telethon.tl.types import (
    MessageEntityBlockquote,
    MessageEntityBold,
    MessageEntityTextUrl,
    MessageMediaGeo,
    MessageMediaGeoLive,
)

import classify
import db
import ebird
import taxonomy

load_dotenv()

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_SESSION_PATH = os.path.join(PROJECT_DIR, "session", "sg_birds_bot")

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
EBIRD_API_KEY = os.environ.get("EBIRD_API_KEY")

EBIRD_DIST_KM = 10
EBIRD_BACK_DAYS = 30


def _git_sha():
    """
    Short git SHA of the running checkout, used by /ping for deploy verification.
    Reads .git/HEAD directly (and the ref it points at) instead of spawning git,
    so it works under systemd where PATH may not include the git binary.
    """
    try:
        head_path = os.path.join(PROJECT_DIR, ".git", "HEAD")
        with open(head_path) as f:
            head = f.read().strip()
        if head.startswith("ref: "):
            ref_path = os.path.join(PROJECT_DIR, ".git", head[5:])
            with open(ref_path) as f:
                return f.read().strip()[:7]
        return head[:7]
    except Exception:
        return "unknown"


GIT_SHA = _git_sha()

# Pending geocode disambiguation choices. Keyed by a short random token that
# we stuff into inline button callback_data (which is capped at 64 bytes).
# Values are the full candidate list; we resolve lat/lng on button tap.
PENDING_GEO_CHOICES = OrderedDict()
PENDING_GEO_MAX = 256  # FIFO-evict when exceeded

# Resolved coordinates + label for refine-keyboard taps. Same FIFO LRU pattern.
PENDING_EBIRD_QUERIES = OrderedDict()
PENDING_EBIRD_MAX = 256

REFINE_RADII = [5, 10, 20, 50]  # km
REFINE_DAYS = [7, 14, 30, 60]

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

# Load the eBird taxonomy into classify.py's lookup sets. Uses the
# on-disk cache if fresh, otherwise fetches with EBIRD_API_KEY. Failure
# is non-fatal: classify.py falls back to treating every query as
# "location", which is the pre-classifier behaviour.
_tax_count = taxonomy.load(api_key=EBIRD_API_KEY)
print(f"Taxonomy: {_tax_count} entries loaded", flush=True)


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
    "  • <b>Species near place</b> (e.g. <code>great horned owl near foster city</code>) — "
    "eBird records of that species at that location.\n"
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
    "• <b>Species near place</b> (<code>great horned owl near foster city</code>, "
    "<code>fairy pitta in taipei</code>) → eBird records of that species, scoped to that location. "
    "Works with <i>near</i>, <i>in</i>, <i>at</i>, or <i>around</i>.\n"
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


def _append_ebird_row(builder, row, back_days):
    builder.add("• ")
    builder.add_bold(row["species"])
    builder.add(f" — {row['date']}")
    count = row.get("_count")
    if count and count > 1:
        builder.add(f" ({count} sightings in last {back_days} days)")
    howmany = row.get("count")
    if howmany:
        builder.add(f" [x{howmany}]")
    if row.get("location") and row.get("lat") is not None and row.get("lng") is not None:
        builder.add("\n   📍 ")
        builder.add_link(row["location"], _coord_maps_link(row["lat"], row["lng"]))
    if row.get("notable"):
        builder.add("\n   ⭐ notable")


def build_ebird_messages(header_place, rows, dist_km, back_days, visible=5, species_name=None):
    """Build reply messages for eBird results. Mirrors build_chat_messages.
    If species_name is set, renders a species-scoped header; otherwise the
    default 'N species near X' header for multi-species location queries."""
    if not rows:
        b = MessageBuilder()
        b.add("No eBird sightings ")
        if species_name:
            b.add("of ")
            b.add_bold(species_name)
            b.add(" ")
        b.add(f"within {dist_km} km of ")
        b.add_bold(header_place)
        b.add(f" in the last {back_days} days.")
        return [_finalize(b)]

    messages = []
    n = len(rows)
    b = MessageBuilder()
    if species_name:
        b.add_bold(f"{n} recent sighting{'s' if n != 1 else ''} of {species_name} near {header_place}")
    else:
        b.add_bold(f"{n} species near {header_place}")
    b.add(f"\n(eBird · {dist_km} km · last {back_days} days)\n\n")
    for row in rows[:visible]:
        _append_ebird_row(b, row, back_days)
        b.add("\n\n")

    if n > visible:
        b.add(f"Tap to expand {n - visible} more:\n")
        next_idx = _pack_ebird_into_blockquote(b, rows, visible, back_days)
    else:
        next_idx = n
    messages.append(_finalize(b))

    while next_idx < n:
        b = MessageBuilder()
        b.add_bold("More results (cont'd):")
        b.add("\n")
        prev_idx = next_idx
        next_idx = _pack_ebird_into_blockquote(b, rows, next_idx, back_days)
        if next_idx == prev_idx:
            next_idx += 1
        messages.append(_finalize(b))

    return messages


def _pack_ebird_into_blockquote(builder, rows, start_idx, back_days):
    if start_idx >= len(rows):
        return start_idx
    bq_start = builder.offset
    idx = start_idx
    while idx < len(rows):
        snap = builder.snapshot()
        _append_ebird_row(builder, rows[idx], back_days)
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


@bot.on(events.NewMessage(pattern=r"^/ping"))
async def on_ping(event):
    """Smoke-test endpoint used by the ship-bot deploy script. Replies with the
    running commit SHA so the deploy can verify the new code is actually live."""
    if not event.is_private:
        return
    await event.reply(f"pong (commit: {GIT_SHA})")


async def _reply_messages(event, messages, buttons=None):
    """Reply with each message in sequence. Buttons (if any) attach to the LAST message."""
    last_idx = len(messages) - 1
    for i, (msg_text, entities) in enumerate(messages):
        kwargs = {"formatting_entities": entities, "link_preview": False}
        if buttons is not None and i == last_idx:
            kwargs["buttons"] = buttons
        await event.reply(msg_text, **kwargs)


def _stash_geo_choices(candidates, species_code=None, species_name=None):
    """Store a candidate list and return a short token for callback data.
    species_code/species_name are carried through so a species+location query
    still hits the species-scoped eBird endpoint after disambiguation."""
    token = secrets.token_hex(4)  # 8 hex chars
    PENDING_GEO_CHOICES[token] = {
        "candidates": list(candidates),
        "species_code": species_code,
        "species_name": species_name,
    }
    PENDING_GEO_CHOICES.move_to_end(token)
    while len(PENDING_GEO_CHOICES) > PENDING_GEO_MAX:
        PENDING_GEO_CHOICES.popitem(last=False)
    return token


def _stash_ebird_query(lat, lng, place_label, dist_km, back_days, species_code=None, species_name=None):
    """Store coordinates + pending refine state. pending_* start equal to the active
    values and are mutated as the user taps radius/days buttons; 'Run' commits them.
    species_code/species_name are optional: when set, Run calls the species-scoped
    eBird endpoint and headers render 'sightings of X near Y'."""
    token = secrets.token_hex(4)
    PENDING_EBIRD_QUERIES[token] = {
        "lat": lat,
        "lng": lng,
        "place": place_label,
        "pending_dist": dist_km,
        "pending_days": back_days,
        "species_code": species_code,
        "species_name": species_name,
    }
    PENDING_EBIRD_QUERIES.move_to_end(token)
    while len(PENDING_EBIRD_QUERIES) > PENDING_EBIRD_MAX:
        PENDING_EBIRD_QUERIES.popitem(last=False)
    return token


def _build_refine_keyboard(token, pending_dist, pending_days):
    """
    Inline keyboard with preset radii (km) and timeframes (days), plus a Run button.
    ✓ marks show the staged selection (what Run will fetch), not necessarily what's
    currently displayed above. Radius/days taps only update marks; Run commits.
    """
    def fmt(val, cur, suffix):
        return f"✓ {val}{suffix}" if val == cur else f"{val}{suffix}"

    radius_row = [
        Button.inline(fmt(r, pending_dist, "km"), data=f"refine:{token}:r:{r}".encode())
        for r in REFINE_RADII
    ]
    days_row = [
        Button.inline(fmt(d, pending_days, "d"), data=f"refine:{token}:d:{d}".encode())
        for d in REFINE_DAYS
    ]
    run_row = [Button.inline("🔍 Run", data=f"refine:{token}:run:0".encode())]
    return [radius_row, days_row, run_row]


def _short_label(display_name, max_len=40):
    """Compact Nominatim display_name for an inline button label."""
    parts = [p.strip() for p in display_name.split(",")]
    if len(parts) >= 3:
        compact = f"{parts[0]}, {parts[-1]}"  # e.g. "Cambridge, United Kingdom"
    else:
        compact = display_name
    if len(compact) > max_len:
        compact = compact[: max_len - 1] + "…"
    return compact


async def _send_geo_picker(event, candidates, species_code=None, species_name=None):
    """Reply with an inline keyboard asking the user to pick among candidates."""
    token = _stash_geo_choices(candidates, species_code=species_code, species_name=species_name)
    buttons = [
        [Button.inline(_short_label(name), data=f"geo:{token}:{i}".encode())]
        for i, (_, _, name) in enumerate(candidates)
    ]
    await event.reply(
        f"Found {len(candidates)} places matching your query — which one?",
        buttons=buttons,
        link_preview=False,
    )


async def _send_ebird_picker(event, lat, lng, place_label, species_code=None, species_name=None):
    """
    Post a picker message BEFORE running any eBird query. The user stages their
    preferred radius/days with the buttons and hits 🔍 Run to actually search.
    Defaults to EBIRD_DIST_KM / EBIRD_BACK_DAYS. If species_code is set, Run
    will hit the species-scoped eBird endpoint instead of the multi-species one.
    """
    if not EBIRD_API_KEY:
        await event.reply(
            "eBird lookups aren't configured on this bot. Set EBIRD_API_KEY in .env to enable them.",
            link_preview=False,
        )
        return
    token = _stash_ebird_query(
        lat, lng, place_label, EBIRD_DIST_KM, EBIRD_BACK_DAYS,
        species_code=species_code, species_name=species_name,
    )
    buttons = _build_refine_keyboard(token, EBIRD_DIST_KM, EBIRD_BACK_DAYS)
    header = f"🦉 **{species_name}** near **{place_label}**" if species_name else f"📍 **{place_label}**"
    await event.reply(
        f"{header}\n\n"
        f"Pick a radius and a timeframe, then tap 🔍 Run.\n"
        f"(defaults: {EBIRD_DIST_KM} km · last {EBIRD_BACK_DAYS} days)",
        buttons=buttons,
        link_preview=False,
    )


async def _handle_ebird_at(event, lat, lng, place_label, dist_km=None, back_days=None,
                           species_code=None, species_name=None):
    """Fetch eBird results at (lat, lng) and reply. place_label is shown in the header.
    dist_km/back_days default to the global preset; the refine keyboard passes overrides.
    If species_code is set, hits /obs/geo/recent/{speciesCode} and groups by location
    instead of by species."""
    if not EBIRD_API_KEY:
        await event.reply(
            "eBird lookups aren't configured on this bot. Set EBIRD_API_KEY in .env to enable them.",
            link_preview=False,
        )
        return
    if dist_km is None:
        dist_km = EBIRD_DIST_KM
    if back_days is None:
        back_days = EBIRD_BACK_DAYS
    if species_code:
        rows = await asyncio.to_thread(
            ebird.recent_species_near, species_code, lat, lng, EBIRD_API_KEY, dist_km, back_days
        )
    else:
        rows = await asyncio.to_thread(
            ebird.recent_near, lat, lng, EBIRD_API_KEY, dist_km, back_days
        )
    if rows is None:
        await event.reply("eBird lookups aren't configured. Set EBIRD_API_KEY in .env.")
        return
    grouped = ebird.group_by_location(rows) if species_code else ebird.group_by_species(rows)
    messages = build_ebird_messages(
        place_label, grouped, dist_km, back_days, visible=5, species_name=species_name,
    )
    token = _stash_ebird_query(
        lat, lng, place_label, dist_km, back_days,
        species_code=species_code, species_name=species_name,
    )
    buttons = _build_refine_keyboard(token, dist_km, back_days)
    await _reply_messages(event, messages, buttons=buttons)


@bot.on(events.NewMessage)
async def on_message(event):
    # Only respond in private (DM) chats
    if not event.is_private:
        return

    # Shared location pin → show picker before searching
    media = event.message.media if event.message else None
    if isinstance(media, (MessageMediaGeo, MessageMediaGeoLive)):
        geo = media.geo
        lat = float(geo.lat)
        lng = float(geo.long)
        place = await asyncio.to_thread(ebird.reverse_geocode, lat, lng) or f"{lat:.4f},{lng:.4f}"
        await _send_ebird_picker(event, lat, lng, place)
        return

    text = (event.raw_text or "").strip()
    # Skip empty messages and commands (handled separately)
    if not text or text.startswith("/"):
        return

    # "SPECIES near LOCATION" queries (e.g. "great horned owl near foster city")
    # take the fast path straight to eBird's species-scoped endpoint. The
    # classifier would otherwise route this as "species" (all tokens bird-y)
    # and never see the location.
    parsed = classify.parse_species_location(text)
    if parsed:
        species_code, species_name, loc_text = parsed
        candidates = await asyncio.to_thread(ebird.geocode_candidates, loc_text)
        if candidates:
            non_sg = [c for c in candidates if not ebird.is_in_sg(c[0], c[1])] or list(candidates)
            if len(non_sg) == 1:
                lat, lng, display_name = non_sg[0]
                await _send_ebird_picker(
                    event, lat, lng, display_name,
                    species_code=species_code, species_name=species_name,
                )
            else:
                await _send_geo_picker(event, non_sg[:5], species_code=species_code, species_name=species_name)
            return
        # Geocoder came up empty — fall through to the normal classifier path.

    kind = classify.classify(text)

    # Species query → local SG archive only. Exotic species (e.g. Sabah
    # Partridge, Bornean Ground Cuckoo) correctly return "no results"
    # instead of routing to a nonsense eBird location.
    if kind == "species":
        await _search_local(event, text)
        return

    # Ambiguous query (place-with-bird-word like "hawk mountain",
    # "eagle lake", "chinese garden") → try local archive first; if
    # there's nothing, fall through to the geocoder.
    if kind == "ambiguous":
        rows = db.search(text, limit=100, acronym_map=ACRONYM_MAP)
        if rows:
            await _reply_local_rows(event, text, rows)
            return
        # fall through to geocoder

    # Location query (or ambiguous with no local hits).
    candidates = await asyncio.to_thread(ebird.geocode_candidates, text)
    if not candidates:
        # Geocoder came up empty — nothing to route to. Fall back to
        # local DB search so the user at least sees a consistent
        # "No sightings found" reply in the same format as everything else.
        await _search_local(event, text)
        return

    top_in_sg = ebird.is_in_sg(candidates[0][0], candidates[0][1])
    if top_in_sg:
        # The user named a place inside SG (e.g. "Windsor Park",
        # "Pulau Ubin") — the local archive has better data for those
        # than eBird's regional feed, so search it.
        await _search_local(event, text)
        return

    non_sg = [c for c in candidates if not ebird.is_in_sg(c[0], c[1])]
    if len(non_sg) == 1:
        lat, lng, display_name = non_sg[0]
        await _send_ebird_picker(event, lat, lng, display_name)
    else:
        await _send_geo_picker(event, non_sg[:5])


async def _search_local(event, text):
    rows = db.search(text, limit=100, acronym_map=ACRONYM_MAP)
    await _reply_local_rows(event, text, rows)


async def _reply_local_rows(event, text, rows):
    rows = maybe_dedupe_by_species(rows)
    messages = build_chat_messages(text, rows, visible=5)
    await _reply_messages(event, messages)


@bot.on(events.CallbackQuery(pattern=rb"^geo:"))
async def on_geo_choice(event):
    """User tapped a disambiguation button — resolve to lat/lng and run eBird."""
    try:
        _, token, idx_str = event.data.decode().split(":", 2)
        idx = int(idx_str)
    except Exception:
        await event.answer("Invalid selection.", alert=True)
        return

    stash = PENDING_GEO_CHOICES.pop(token, None)
    cands = stash["candidates"] if stash else None
    if not cands or idx < 0 or idx >= len(cands):
        await event.answer("That selection has expired — please search again.", alert=True)
        try:
            await event.edit("(selection expired)", buttons=None)
        except Exception:
            pass
        return

    lat, lng, display_name = cands[idx]
    await event.answer()  # dismiss the loading spinner
    try:
        await event.edit(f"Selected: **{_short_label(display_name)}**", buttons=None)
    except Exception:
        pass
    await _send_ebird_picker(
        event, lat, lng, display_name,
        species_code=stash.get("species_code"),
        species_name=stash.get("species_name"),
    )


@bot.on(events.CallbackQuery(pattern=rb"^refine:"))
async def on_refine(event):
    """
    Radius/days taps stage the selection (update ✓ marks in place).
    The Run tap commits and fetches with the staged values.
    """
    try:
        _, token, kind, value = event.data.decode().split(":", 3)
        value = int(value)
    except Exception:
        await event.answer("Invalid refine request.", alert=True)
        return

    stashed = PENDING_EBIRD_QUERIES.get(token)
    if not stashed:
        await event.answer("That search has expired — please query again.", alert=True)
        return

    if kind == "r":
        stashed["pending_dist"] = value
        await event.answer(f"Staged: {value} km")
        try:
            await event.edit(buttons=_build_refine_keyboard(
                token, stashed["pending_dist"], stashed["pending_days"]
            ))
        except Exception:
            pass
        return

    if kind == "d":
        stashed["pending_days"] = value
        await event.answer(f"Staged: {value} days")
        try:
            await event.edit(buttons=_build_refine_keyboard(
                token, stashed["pending_dist"], stashed["pending_days"]
            ))
        except Exception:
            pass
        return

    if kind == "run":
        lat = stashed["lat"]
        lng = stashed["lng"]
        place_label = stashed["place"]
        dist_km = stashed["pending_dist"]
        back_days = stashed["pending_days"]
        await event.answer(f"Searching {dist_km} km · {back_days} d…")
        await _handle_ebird_at(
            event, lat, lng, place_label, dist_km=dist_km, back_days=back_days,
            species_code=stashed.get("species_code"),
            species_name=stashed.get("species_name"),
        )
        return

    await event.answer("Unknown refine type.", alert=True)


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
