# Vacation Planning Bot — Project Context

A Claude-powered Telegram bot for a family group chat. Members ask about flights, hotels, rentals, activities, and the bot responds with live search data via SerpApi. It stores confirmed bookings per trip and can scan Gmail for booking confirmation emails.

Runs as a **macOS LaunchDaemon** on a Mac Mini (`/Library/LaunchDaemons/com.vacationbot.telegram.plist`). A **rumps tray app** (`tray/bot_tray.py`) provides Start/Stop/Restart controls and status monitoring from the menu bar.

---

## Project Layout

```
~/projects/vacation-bot/
├── .env                          # secrets (never commit)
├── telegram/
│   └── bot.py                    # Telegram bot — main entry point
├── shared/
│   ├── claude_client.py          # Claude Haiku + tool-calling + conversation history
│   ├── serpapi_client.py         # SerpApi wrappers: flights, hotels, rentals, places, etc.
│   ├── web_fetcher.py            # URL extraction + content fetching
│   ├── bookings.py               # Persistent booking CRUD (per chat_id + trip_name)
│   ├── pending_bookings.py       # Temporary store for email-found bookings awaiting confirm
│   └── email_scanner.py         # Gmail IMAP + Claude Haiku extraction pipeline
├── tray/
│   └── bot_tray.py               # macOS menu bar tray (rumps)
├── launchd/
│   └── com.vacationbot.telegram.plist   # LaunchDaemon config (copy to /Library/LaunchDaemons/)
├── data/                         # Runtime data — gitignored
│   └── {safe_chat_id}/
│       ├── config.json                        # trip list + default trip
│       ├── {trip}_summary.txt                 # rolling Claude-generated summaries
│       ├── {trip}_bookings.json               # confirmed bookings
│       ├── {trip}_pending.json                # pending (unconfirmed email finds)
│       └── scanned_email_ids.json             # dedup store for email scanner
├── logs/                         # Runtime logs — gitignored
│   ├── telegram.log
│   ├── telegram.error.log
│   └── tray.log
└── run_telegram.sh               # Shell wrapper used by LaunchDaemon
```

`chat_id` is stored as a "safe" string: negative group IDs have the `-` replaced with `neg` (e.g., `-1234` → `neg1234`).

---

## Environment Variables (`.env`)

```
TELEGRAM_BOT_TOKEN=...        # from BotFather
ANTHROPIC_API_KEY=...         # claude-haiku-4-5-20251001
SERPAPI_KEY=...               # SerpApi
TRIGGER_WORD=!claude          # default
ALLOWED_CHAT_ID=              # optional — restricts bot to one group chat ID
GMAIL_ADDRESS=...             # for email scanning
GMAIL_APP_PASSWORD=...        # 16-char Google app password
EMAIL_SCAN_DAYS=90            # how far back to search (default 90)
```

**Important**: The bot token was exposed in conversation logs and should be regenerated via BotFather (`/mybots` → API Token → Revoke).

---

## Key Module Notes

### `telegram/bot.py`
- `python-telegram-bot` 22.x with `asyncio` + `JobQueue`
- Install: `pip install "python-telegram-bot[job-queue]"`
- `handle_message` / `handle_edited_message` → `process_message`
- `process_message` dispatches all `!claude` commands
- `_daily_email_scan` runs at 08:00 via `app.job_queue.run_daily()`
- `ALLOWED_CHAT_ID` must be set for the daily scan to know which chat to notify
- `chunk_message` splits long replies at paragraph/sentence boundaries (Telegram 4096-char limit)
- Edited message support: re-runs if question changed, ignores trivial whitespace/punctuation edits
- `_responded_ids` tracks which message IDs have been answered to prevent double-responses on edit

### `shared/claude_client.py`
- Model: `claude-haiku-4-5-20251001`
- Agentic loop: keeps calling Claude until `stop_reason == "end_turn"` (handles multi-tool chains)
- `MAX_HISTORY = 20` messages in RAM per trip; overflow triggers summarization to disk
- System prompt is rebuilt each call: base instructions + rolling summary + current bookings block
- Booking tools injected into every call: `add_booking`, `list_bookings`, `update_booking`, `remove_booking`
- Search tools: `search_flights`, `search_hotels`, `search_rentals`, `search_places`, `search_reviews`, `search_events`, `search_explore`
- URL content is prepended to the user message when URLs are detected (`web_fetcher.py`)

### `shared/bookings.py`
- One JSON file per trip: `data/{safe_chat_id}/{trip_name}_bookings.json`
- `add_booking` auto-generates ID: `{type[0]}{count:03d}_{uuid4[:4]}` (e.g., `f001_3a7c`)
- `format_for_prompt` → compact string injected into system prompt
- `format_for_telegram` → Markdown for display in chat
- Booking types: `flight`, `hotel`, `rental`, `activity`

### `shared/email_scanner.py`
- Gmail IMAP with `X-GM-RAW` search for full Gmail query syntax
- `GMAIL_QUERY` searches broad set of booking-related subject keywords
- Each email → Claude Haiku → structured JSON (or `{"is_booking": false}`)
- Returns only unseen emails (tracked by `scanned_email_ids.json`)
- `scan_for_bookings(chat_id, trip_name)` is the main entry point

### `tray/bot_tray.py`
- Status detection: `pgrep -f telegram/bot.py` + `ps -o ppid=` to distinguish daemon (ppid=1) vs orphan processes
- Three states: 🟢 daemon running, 🟠 orphan (started outside daemon), 🔴 stopped
- Start/Restart: `sudo launchctl kickstart [-k] system/com.vacationbot.telegram`
- Stop: `sudo launchctl stop com.vacationbot.telegram`
- Kill orphan: `kill -9 {pid}` (no sudo needed — process is owned by user)
- Requires visudo entry: `jon ALL=(ALL) NOPASSWD: /bin/launchctl`
- Install: `pip install rumps`

---

## LaunchDaemon

**Current issue**: `KeepAlive` is set to `<true/>` in the plist, which means `launchctl stop` immediately restarts the process. This causes 409 Conflict errors if a CLI instance of the bot is also running.

**Fix needed** — change in `/Library/LaunchDaemons/com.vacationbot.telegram.plist`:
```xml
<!-- Replace <key>KeepAlive</key><true/> with: -->
<key>KeepAlive</key>
<dict>
    <key>SuccessfulExit</key>
    <false/>
</dict>
```

After editing: `sudo launchctl bootout system/com.vacationbot.telegram && sudo launchctl bootstrap system /Library/LaunchDaemons/com.vacationbot.telegram.plist`

**Useful launchctl commands:**
```bash
# Status
sudo launchctl list com.vacationbot.telegram

# Start (if loaded but stopped)
sudo launchctl kickstart system/com.vacationbot.telegram

# Restart (kill + restart atomically)
sudo launchctl kickstart -k system/com.vacationbot.telegram

# Stop (works correctly AFTER KeepAlive fix above)
sudo launchctl stop com.vacationbot.telegram

# Full unload (removes from launchd entirely)
sudo launchctl bootout system/com.vacationbot.telegram

# Full reload
sudo launchctl bootstrap system /Library/LaunchDaemons/com.vacationbot.telegram.plist

# Check for multiple instances (should be 1)
pgrep -f "telegram/bot.py" | wc -l

# Nuclear wipe of all instances
sudo launchctl bootout system/com.vacationbot.telegram 2>/dev/null; sudo pkill -9 -f "telegram/bot.py"
```

---

## Bot Commands Reference

| Command | Description |
|---|---|
| `!claude help` | Show full help |
| `!claude trips` | List all trips |
| `!claude trip new <name>` | Create a trip (no spaces, max 30 chars) |
| `!claude trip default <name>` | Set default trip |
| `!claude trip delete <name>` | Delete trip (admin only) |
| `!claude reset [#trip]` | Clear in-memory history |
| `!claude summarize [#trip]` | Save planning summary to disk |
| `!claude booked [#trip]` | Show confirmed bookings |
| `!claude scan email [#trip]` | Scan Gmail for booking emails |
| `!claude book save all` | Save all pending email finds |
| `!claude book save 1 3` | Save specific pending items |
| `!claude book skip` | Discard pending without saving |
| `!claude flights <args>` | Direct flight search |
| `!claude hotels <args>` | Direct hotel search |
| `!claude rentals <args>` | Direct rental search |
| `!claude places <query>` | Place/restaurant search |
| `!claude reviews <place>` | Google Maps reviews |
| `!claude events <city> [date]` | Local events |
| `!claude explore <destination>` | Destination overview |
| `!claude #tripname <question>` | Ask about a specific trip |

---

## Known Issues / Pending

1. **KeepAlive plist fix** (see above) — not yet applied to the deployed plist on the Mac Mini
2. **Bot token rotation** — token was exposed in chat logs, needs to be revoked via BotFather and updated in `.env`
3. **`ALLOWED_CHAT_ID` not set** — daily email scan will silently skip; set this to the Telegram group chat ID
4. **Email scanning not tested end-to-end** — `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` need to be set in `.env`
5. **Patch files** (`bot_booked_patch.py`, `bot_email_patch.py`, `claude_client_bookings_patch.py`) — these are stale diffs from mid-session, can be deleted; the patches are already applied to the main files

---

## Dependencies

```bash
# Core
pip install "python-telegram-bot[job-queue]"
pip install anthropic python-dotenv requests

# Tray
pip install rumps

# All flags for system Python on macOS
pip install --break-system-packages <package>
```

Python 3.11+ required (uses `list[int]`, `int | None` type hints).
