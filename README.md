# SG Birds Daily Digest

A small Python project that turns a Singapore birding Telegram group into:

1. **A daily digest** — Claude Haiku reads the day's messages and produces a clean, grouped summary of bird species, locations, and notable details, delivered to your Telegram Saved Messages (or any chat).
2. **A searchable 90-day archive** — every sighting is extracted into a local SQLite database with full-text search.
3. **A Telegram search bot** — DM the bot with a species or location and it replies with matching sightings from the archive. For places outside Singapore, it falls back to the eBird API. You can also share a 📍 GPS pin and get recent eBird sightings at your current coordinates — great for trip planning or on-the-ground "what's here?" queries.

The summary script runs on macOS via `launchd`; the bot runs 24/7 on a small Linux VM (Hetzner CX23, ~€5/mo) via `systemd`. Recurring cost is dominated by a tiny Anthropic API spend (~pennies per day).

## Why

The SG Birds Telegram group is a wonderful firehose of sightings, but:

- It's hard to skim if you've been away for the day
- It's hard to search ("when was the last Fairy Pitta?")
- Asking the group spams everyone else

This project solves all three problems while keeping the source of truth (the group itself) untouched.

## Features

- 📋 **Daily summary**: structured digest of every species seen today, delivered to Telegram
- 🔍 **Private search**: DM the bot — results are only visible to you
- 🌍 **Worldwide fallback**: non-SG queries automatically use the eBird API (10 km, last 30 days)
- 📍 **GPS mode**: share a Telegram location pin and get recent eBird sightings at your exact coordinates
- 🗂️ **Acronym expansion**: typing `SBG` finds Singapore Botanic Gardens sightings, `BTNR` finds Bukit Timah, etc.
- 🎯 **Smart deduplication**: location queries collapse to one row per species; species queries show all sightings
- 🔗 **Deep links**: every SG result links back to the original group message
- 🗺️ **Map links**: every location (local or eBird) links to Google Maps
- 🪵 **Auto-pruning**: 90-day rolling window keeps the DB small
- ⚡ **Self-updating acronyms**: new acronyms in messages are auto-detected and appended to `acronyms.md`

## Architecture

Three processes share one SQLite database (`sightings.db`):

```
┌─────────────────────┐    writes    ┌─────────────────┐
│ sg_birds_summary.py │ ───────────► │  sightings.db   │
│ (Mac, daily via     │              │ (SQLite + FTS5) │
│  launchd)           │              └─────────────────┘
└─────────────────────┘                       ▲
                                               │ reads
┌─────────────────────┐    writes             │
│ backfill.py         │ ──────────────────────┤
│ (one-time)          │                       │
└─────────────────────┘                       │
                                      ┌────────────────┐         ┌──────────┐
                                      │  bot.py        │ ──────► │ Nominatim│
                                      │ (Hetzner VM,   │         └──────────┘
                                      │  systemd)      │         ┌──────────┐
                                      └────────────────┘ ──────► │  eBird   │
                                                                 └──────────┘
```

| Process | Host | Role |
|---|---|---|
| `sg_birds_summary.py` | Mac (launchd) | Fetches the day's messages, calls Claude Haiku to extract sightings + write a digest, persists to DB, prunes >90-day rows. |
| `bot.py` | Linux VM (systemd) | Long-running Telegram bot. Handles DMs and GPS pins; routes queries between the local DB and the eBird API based on whether the query resolves inside Singapore. |
| `backfill.py` | Mac (one-time) | Populates the DB from historical group messages. |
| `ebird.py` | — | Library module used by `bot.py` for Nominatim geocoding + eBird `/obs/geo/recent` calls. |

The `sightings.db` file lives next to `sg_birds_summary.py` on the Mac; a copy is kept on the VM for the bot to read.

## Setup

### Prerequisites

- Python 3.10+
- A Telegram account that's a member of the source group
- A [Telegram API ID & hash](https://my.telegram.org)
- An [Anthropic API key](https://console.anthropic.com)
- (Optional) An [eBird API key](https://ebird.org/api/keygen) — free, enables the worldwide fallback
- macOS for the daily summary scheduler (launchd plist); any Linux box for the bot (systemd unit example included for Hetzner)

### Install

```bash
git clone https://github.com/hyl317/SG-birds-daily-digest.git
cd SG-birds-daily-digest
pip install -r requirements.txt
cp .env.example .env
# edit .env with your credentials
```

### First run (interactive setup)

```bash
python sg_birds_summary.py
```

On first run, the script will:

1. Prompt for your Telegram phone number + verification code (one-time)
2. Show your group list and ask which one to monitor
3. Ask what time and how often to send the digest
4. Ask where to deliver it (Saved Messages or another chat)
5. Generate a launchd plist and install it so the digest runs on schedule

### Backfill (optional, one-time)

If you want the search bot to have history immediately:

```bash
python backfill.py            # last 90 days, default
python backfill.py --days 30  # smaller test run
```

This burns some Claude tokens (~$0.25 for 90 days of a moderate-traffic group).

### Bot setup

The search bot is a *separate* Telegram bot account, distinct from your personal account:

1. Open [@BotFather](https://t.me/BotFather) and run `/newbot`. Pick a name and username.
2. Copy the token BotFather gives you and add it to `.env` as `BOT_TOKEN=...`
3. (Optional) Add `EBIRD_API_KEY=...` to `.env` to enable the worldwide eBird fallback
4. (Optional) Polish the profile with `/setdescription`, `/setabouttext`, `/setcommands`, `/setuserpic`

Test it locally:

```bash
python bot.py
```

Then DM your bot with `fairy pitta`, `foster city`, or a location pin.

### Running the bot 24/7 (Linux VM, systemd)

A laptop works for local testing, but for always-on coverage you'll want to run the bot on a small Linux VM. A Hetzner CX23 (€5/mo) is plenty.

On the VM:

```bash
sudo apt install -y python3-venv python3-pip
git clone https://github.com/hyl317/SG-birds-daily-digest.git ~/sg-birds
cd ~/sg-birds
python3 -m venv sg-birds-env
source sg-birds-env/bin/activate
pip install -r requirements.txt
# copy your .env, session/ dir, and sightings.db from your Mac via rsync/scp
sudo timedatectl set-timezone Asia/Singapore
```

Create `/etc/systemd/system/sgbirds-bot.service`:

```ini
[Unit]
Description=SG Birds inline search bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/root/sg-birds
ExecStart=/root/sg-birds/sg-birds-env/bin/python -u bot.py
Restart=always
RestartSec=10
StandardOutput=append:/root/sg-birds/bot.log
StandardError=append:/root/sg-birds/bot.log

[Install]
WantedBy=multi-user.target
```

Enable:

```bash
systemctl daemon-reload
systemctl enable --now sgbirds-bot.service
systemctl status sgbirds-bot.service
```

### Running the summary on a Mac (launchd)

The `sg_birds_summary.py` interactive first-run installs its own launchd plist for you. See the "First run" section above. If you prefer to run the summary on the same Linux VM as the bot, a systemd timer works equivalently — see `CLAUDE.md` for the unit file.

## Usage examples

### Searching the bot

Open a private chat with the bot and send one of:

**Species or SG location → local archive (SG Birds group, last 90 days)**

| Query | What you get |
|---|---|
| `fairy pitta` | All Fairy Pitta sightings, most recent first |
| `pitta` | One row per pitta species (Fairy, Mangrove, Hooded, Blue-winged, ...) |
| `oriental darter` | All Oriental Darter sightings |
| `SBG` | Recent species seen at Singapore Botanic Gardens, deduped |
| `sungei buloh` | Recent species at Sungei Buloh, deduped |
| `BTNR` | Recent species at Bukit Timah Nature Reserve |

**Non-SG place name → eBird fallback (10 km radius, last 30 days)**

Handy for trip planning anywhere in the world. Requires an `EBIRD_API_KEY` in `.env` (get one free at https://ebird.org/api/keygen).

| Query | What you get |
|---|---|
| `foster city` | Recent species seen within 10 km of Foster City, CA |
| `taipei` | Recent species around Taipei, Taiwan |
| `kaeng krachan` | Recent species at/near Kaeng Krachan National Park, Thailand |

**📍 Share a location pin → eBird at your exact coordinates**

Tap the 📎 attach button in Telegram → **Location** → **Send My Current Location** (or pick a point on the map). The bot replies with recent eBird sightings near those coordinates — the fastest way to ask "what's being seen *right here, right now?*" while you're out in the field.

Each result shows the species, date, count, and a Google Maps link to the exact hotspot.

### Daily digest

Delivered automatically on your configured schedule. Each entry shows the species in **bold**, followed by location(s) and notable details. A stats line at the top highlights rare visitors and unusual behaviour.

## Key files

| File | Purpose |
|---|---|
| `sg_birds_summary.py` | Main scheduled script (fetch, summarize, send, persist) |
| `bot.py` | Long-running search bot (DM + GPS pin) |
| `ebird.py` | Nominatim + eBird API wrapper used by `bot.py` |
| `backfill.py` | One-time historical import |
| `db.py` | SQLite schema + FTS5 search + acronym expansion |
| `acronyms.md` | Known acronyms (human-editable + auto-appended) |
| `tests/test_ebird.py` | Unit tests for `ebird.py` (network calls mocked) |
| `requirements.txt` | Python dependencies |
| `.env.example` | Template for your credentials |
| `com.hyl.sgbirds-summary.plist` | launchd schedule for the digest (Mac) |
| `com.hyl.sgbirds-bot.plist` | launchd config for the bot (Mac, optional) |
| `FOLLOWUPS.md` | Deferred enhancements to revisit |

## Costs

- **Anthropic API**: a few cents per day for the daily digest (Claude Haiku is cheap). The 90-day backfill is a one-time ~$0.25.
- **eBird API**: free.
- **Telegram**: free.
- **Hosting**: free for the summary script (your own Mac), ~€5/mo for the always-on bot VM (Hetzner CX23). A home Raspberry Pi or an Oracle Cloud Free Tier instance also works.

## Limitations & caveats

- **Data quality depends on Claude's extraction.** Most entries are accurate, but occasional misreads happen (e.g. wrong species attribution from chat context). The prompt is tightened to minimise this.
- **Deep links** in SG search results only work for members of the source group.
- **eBird fallback scope**: fixed at 10 km / 30 days in the current version. User-configurable radius + date window is in `FOLLOWUPS.md`.
- **Geocoding is Nominatim-based** (free, public): occasionally a place name won't resolve or will resolve to an unexpected location. Falling back to a shared 📍 GPS pin always works.
- **The daily summary scheduler is macOS-specific** (launchd plist). The Python itself runs anywhere; swap in a systemd timer or cron on Linux. The bot is already Linux-friendly and runs on the VM under systemd.

## Acknowledgments

- The wonderful birding community of the SG Birds Telegram group, which makes this possible
- [Telethon](https://github.com/LonamiWebs/Telethon) for the Telegram client
- [Anthropic Claude Haiku](https://www.anthropic.com/) for fast, cheap, accurate extraction
- [eBird](https://ebird.org/) and the Cornell Lab of Ornithology for the global sightings API
- [Nominatim / OpenStreetMap](https://nominatim.openstreetmap.org/) for free geocoding

## License

MIT
