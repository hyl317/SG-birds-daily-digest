# SG Birds Daily Summary — Setup Guide

## Prerequisites

You need two sets of credentials before starting:

### 1. Telegram API credentials
- Go to https://my.telegram.org and log in with your phone number
- Click "API development tools" and create a new application
- Note down your `api_id` and `api_hash`
- You must be a **member** of the "SG Birds (sightings & live update)" group

### 2. Claude API key
- Get one from https://console.anthropic.com

## Install & Configure

```bash
cd "/Users/hyl/Desktop/leisure coding/sg-birds-summary"

# Install dependencies
pip install -r requirements.txt

# Create your .env file from the template
cp .env.example .env
```

Edit `.env` and fill in all values:

```
TELEGRAM_API_ID=12345
TELEGRAM_API_HASH=your_api_hash_here
ANTHROPIC_API_KEY=sk-ant-...
```

## First Run (interactive setup)

Run the script manually the first time:

```bash
python sg_birds_summary.py
```

It will walk you through:
1. **Telegram login** — phone number and verification code (one-time only, session is saved in `session/`)
2. **Group selection** — lists all your Telegram groups, pick which one to monitor
3. **Summary time** — what time of day to send the summary (24h format, e.g. 21:00)
4. **Frequency** — how often to send: every 6h, 8h, 12h, or 24h

Your choices are saved to `config.json` and the launchd schedule is automatically installed.

## Changing the schedule

To change the group, time, or frequency:

```bash
# Delete the config to trigger setup again
rm config.json

# Re-run the script
python sg_birds_summary.py
```

The setup will re-prompt for all options and automatically update the launchd schedule.

## Managing the launchd job

To verify it's loaded:

```bash
launchctl list | grep sgbirds
```

To unload (stop scheduling):

```bash
launchctl unload ~/Library/LaunchAgents/com.hyl.sgbirds-summary.plist
```

Check logs anytime with:

```bash
cat /tmp/sg_birds_summary.log
```

## How It Works

1. Fetches all text messages from the selected Telegram group for the configured time window
2. Sends them to Claude Haiku for extraction — groups results by bird species with locations, times, and notable details
3. Sends you the summary as a message in your Telegram Saved Messages

## Key Notes

- **Group membership required** — you must have joined the Telegram group for Telethon to read it
- **Photo-only messages are skipped** — only text messages and media captions are captured
- **Large message volumes** — if the group produces more than ~100K characters in a day, messages are truncated
- **Cost** — uses Claude Haiku, roughly ~$0.01/day or less
- **Logs** — output goes to `/tmp/sg_birds_summary.log`
