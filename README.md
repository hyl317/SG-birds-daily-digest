# SG Birds Daily Digest

A small Python project that turns a Singapore birding Telegram group into:

1. **A daily digest** — Claude Haiku reads the day's messages and produces a clean, grouped summary of bird species, locations, and notable details, delivered to your Telegram Saved Messages (or any chat).
2. **A searchable 90-day archive** — every sighting is extracted into a local SQLite database with full-text search.
3. **An inline-query Telegram bot** — anyone in the group can type `@SGBirdsSearchBot fairy pitta` (or DM the bot) to look up recent sightings privately, without spamming the group.

It runs entirely on a Mac via `launchd` — no servers, no cloud, no recurring costs beyond a small Anthropic API spend (~pennies per day).

## Why

The SG Birds Telegram group is a wonderful firehose of sightings, but:

- It's hard to skim if you've been away for the day
- It's hard to search ("when was the last Fairy Pitta?")
- Asking the group spams everyone else

This project solves all three problems while keeping the source of truth (the group itself) untouched.

## Features

- 📋 **Daily summary**: structured digest of every species seen today, delivered to Telegram
- 🔍 **Private search**: DM the bot or use inline mode — results are visible only to you
- 🗂️ **Acronym expansion**: typing `SBG` finds Singapore Botanic Gardens sightings, `BTNR` finds Bukit Timah, etc.
- 🎯 **Smart deduplication**: location queries collapse to one row per species; species queries show all sightings
- 🔗 **Deep links**: every result links back to the original group message
- 🪵 **Auto-pruning**: 90-day rolling window keeps the DB small
- ⚡ **Self-updating acronyms**: new acronyms in messages are auto-detected and appended to `acronyms.md`

## Architecture

Three processes share one SQLite database (`sightings.db`):

```
┌─────────────────────┐    writes    ┌─────────────────┐
│ sg_birds_summary.py │ ───────────► │  sightings.db   │
│ (scheduled daily)   │              │ (SQLite + FTS5) │
└─────────────────────┘              └─────────────────┘
                                              ▲
┌─────────────────────┐    writes            │
│ backfill.py         │ ─────────────────────┤
│ (one-time)          │                      │
└─────────────────────┘                      │ reads
                                             │
                                    ┌────────────────┐
                                    │  bot.py        │
                                    │ (long-running, │
                                    │ inline + DM)   │
                                    └────────────────┘
```

| Process | Role |
|---|---|
| `sg_birds_summary.py` | Scheduled via launchd. Fetches the day's messages, calls Claude Haiku to extract sightings + write a digest, persists to DB, prunes >90-day rows. |
| `bot.py` | Long-running Telegram bot account. Listens for inline queries and DMs, searches the DB, returns formatted results. |
| `backfill.py` | One-time tool to populate the DB from historical group messages. |

## Setup

### Prerequisites

- Python 3.10+
- A Telegram account that's a member of the source group
- A [Telegram API ID & hash](https://my.telegram.org)
- An [Anthropic API key](https://console.anthropic.com)
- macOS (the launchd plists are macOS-specific; the Python code itself runs anywhere)

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
3. (Optional) Run `/setinline` in BotFather to enable inline mode
4. (Optional) Polish the profile with `/setdescription`, `/setabouttext`, `/setcommands`, `/setuserpic`

Test it:

```bash
python bot.py
```

Then DM your bot or type `@yourbotname fairy pitta` in any chat. Once it's working, install as a launchd service so it runs 24/7:

```bash
ln -sf "$PWD/com.hyl.sgbirds-bot.plist" ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.hyl.sgbirds-bot.plist
```

The plist wraps the bot in `caffeinate -is` so the Mac stays awake (system-wide; your screen still sleeps).

## Usage examples

### Searching the bot

In a DM with the bot, just type:

| Query | What you get |
|---|---|
| `fairy pitta` | All Fairy Pitta sightings, most recent first |
| `pitta` | One row per pitta species (Fairy, Mangrove, Hooded, Blue-winged, ...) |
| `SBG` | Recent species seen at Singapore Botanic Gardens, deduped |
| `sungei buloh` | Recent species at Sungei Buloh, deduped |
| `oriental darter` | All Oriental Darter sightings |
| `BTNR` | Recent species at Bukit Timah Nature Reserve |

Or use inline mode in any chat: `@yourbotname fairy pitta`

### Daily digest

Delivered automatically on your configured schedule. Each entry shows the species in **bold**, followed by location(s) and notable details. A stats line at the top highlights rare visitors and unusual behaviour.

## Key files

| File | Purpose |
|---|---|
| `sg_birds_summary.py` | Main scheduled script |
| `bot.py` | Long-running search bot |
| `backfill.py` | One-time historical import |
| `db.py` | SQLite schema + FTS5 search + acronym expansion |
| `acronyms.md` | Known acronyms (human-editable + auto-appended) |
| `requirements.txt` | Python dependencies |
| `.env.example` | Template for your credentials |
| `com.hyl.sgbirds-summary.plist` | launchd schedule for the digest |
| `com.hyl.sgbirds-bot.plist` | launchd config for the bot |

## Costs

- **Anthropic API**: a few cents per day for the daily digest (Claude Haiku is cheap). The 90-day backfill is a one-time ~$0.25.
- **Telegram**: free.
- **Hosting**: free (your own Mac).

## Limitations & caveats

- **The bot only runs while your Mac is awake.** `caffeinate` prevents idle/system sleep, but if you close the lid without external power, the bot goes offline. For true 24/7 you'd want a small VPS or Raspberry Pi.
- **Data quality depends on Claude's extraction.** Most entries are accurate, but occasional misreads happen (e.g. wrong species attribution from chat context). The prompt is tightened to minimise this.
- **Deep links** in search results only work for members of the source group.
- **macOS only for the launchd integration.** The Python code runs anywhere; you'd just need to adapt the scheduling.

## Acknowledgments

- The wonderful birding community of the SG Birds Telegram group, which makes this possible
- [Telethon](https://github.com/LonamiWebs/Telethon) for the Telegram client
- [Anthropic Claude Haiku](https://www.anthropic.com/) for fast, cheap, accurate extraction

## License

MIT
