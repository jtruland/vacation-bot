# Vacation Planning Bot — Project Context

A Claude-powered Telegram bot supporting multiple group chats and private DM access. Members ask about flights, hotels, rentals, activities, and the bot responds with live search data via SerpApi. It stores confirmed bookings per trip and can scan Gmail for booking confirmation emails.

Runs as a **macOS LaunchDaemon** on a Mac Mini (`/Library/LaunchDaemons/com.vacationbot.telegram.plist`). The tray icon and menu are integrated directly into the bot process (`tray/tray.py`) — there is no separate tray launcher.

---

## Project Layout

```
~/projects/vacation-bot/
├── .env                          # secrets (never commit)
├── env.template                  # template for .env
├── install_daemon.sh             # one-command LaunchDaemon install (substitutes paths)
├── run_telegram.sh               # shell wrapper used by LaunchDaemon
├── telegram/
│   └── bot.py                    # main entry point: BotManager + handlers + tray launch
├── shared/
│   ├── paths.py                  # DATA_DIR + chat_dir() + list_chat_ids() — data path helpers
│   ├── admin_config.py           # Allowed-chat allowlist (data/admin_config.json) + rehome_chat()
│   ├── dm_router.py              # DM join codes, link store, trip routing, activity log
│   ├── claude_client.py          # Claude Haiku + tool-calling + conversation history
│   ├── serpapi_client.py         # SerpApi wrappers: flights, hotels, rentals, places, etc.
│   ├── web_fetcher.py            # URL extraction + content fetching
│   ├── bookings.py               # Persistent booking CRUD (per chat_id + trip_name)
│   ├── pending_bookings.py       # Temporary store for email-found bookings awaiting confirm
│   └── email_scanner.py          # Gmail IMAP + Claude Haiku extraction pipeline
├── tray/
│   └── tray.py                   # macOS menu bar tray (rumps) — runs inside the bot process
├── launchd/
│   └── com.vacationbot.telegram.plist   # LaunchDaemon template (__PROJECT_DIR__, __USERNAME__ placeholders)
├── data/                         # Runtime data — gitignored
│   ├── admin_config.json                      # allowed + pending chat IDs (managed via bot commands)
│   ├── dm_store.json                          # DM chat_id → [group_chat_ids] links
│   └── {safe_chat_id}/
│       ├── config.json                        # trip list, default trip, dm_code (if enabled)
│       ├── {trip}_summary.txt                 # rolling Claude-generated summaries
│       ├── {trip}_bookings.json               # confirmed bookings
│       ├── {trip}_pending.json                # pending (unconfirmed email finds)
│       ├── dm_activity.json                   # pending DM activity entries for group summary
│       └── scanned_email_ids.json             # dedup store for email scanner
└── logs/                         # Runtime logs — gitignored
    └── telegram.log              # all log output (written by FileHandler in bot.py)
```

`chat_id` is stored as a "safe" string: negative group IDs have the `-` replaced with `neg` (e.g., `-1234` → `neg1234`).

---

## Environment Variables (`.env`)

```
TELEGRAM_BOT_TOKEN=...        # from BotFather
ANTHROPIC_API_KEY=...         # claude-haiku-4-5-20251001
SERPAPI_KEY=...               # SerpApi
TRIGGER_WORD=!claude          # default
BOT_OWNER_ID=...              # your Telegram user ID — receives new-group approval DMs, gates admin commands
ALLOWED_CHAT_IDS=             # optional bootstrap seed (comma-separated); ignored after admin_config.json exists
GMAIL_ADDRESS=...             # for email scanning
GMAIL_APP_PASSWORD=...        # 16-char Google app password (not login password)
EMAIL_SCAN_DAYS=90            # how far back to search (default 90)
```

---

## Architecture

### Process model

`telegram/bot.py` is the single entry point. `main()`:
1. Creates a `BotManager` and calls `start()` — this runs the Telegram async polling loop in a background thread
2. Imports and runs `VacationBotTray` from `tray/tray.py` on the main thread (required by macOS AppKit)
3. If `rumps` is not installed, falls back to headless mode (bot thread runs until killed)

`BotManager` owns the asyncio event loop in its thread. The tray signals it via `loop.call_soon_threadsafe(stop_event.set)` for clean shutdown.

### Tray controls

| Action | Behaviour |
|--------|-----------|
| **Start Bot** | Starts a new bot thread (available when stopped/crashed) |
| **Stop Bot** | Signals the async loop to stop gracefully; tray stays up |
| **Reload** | Stops bot cleanly, then `os._exit(1)` → launchd restarts the process (picks up code changes) |
| **Quit** | Stops bot cleanly, then `os._exit(0)` → launchd does NOT restart |

Reload works because `KeepAlive.SuccessfulExit=false` in the plist means launchd only restarts on non-zero exit.

### Logging

All log output goes to `logs/telegram.log` via a `FileHandler` configured in `_setup_logging()` at the top of `bot.py`. A `StreamHandler` (stderr) is also added as a fallback. All `shared/` modules use standard `logging` — no `print()` statements.

---

## Multi-Chat and DM Access

### Group allowlist (admin_config.py)

Allowed chats are stored in `data/admin_config.json`, not env vars. On first run, `bootstrap_from_env()` seeds the list from `ALLOWED_CHAT_IDS` if set. After that, the file is the source of truth, managed via bot commands.

When the bot is added to a new group, `handle_my_chat_member` DMs `BOT_OWNER_ID` with approve/deny commands. Until approved, messages from the group are silently ignored.

**Owner commands (via DM, only from `BOT_OWNER_ID`):**
| Command | Action |
|---------|--------|
| `!claude admin list` | Show allowed + pending chats |
| `!claude admin allow <chat_id>` | Approve a group; sends welcome message |
| `!claude admin deny <chat_id>` | Remove from pending |
| `!claude admin revoke <chat_id>` | Remove from allowed; bot leaves |
| `!claude admin rehome <old> <new>` | Manual chat ID migration |

### Group migration (supergroup upgrade)

Telegram chat IDs change when a regular group is upgraded to a supergroup. `handle_migration` detects this automatically (`Message.migrate_to_chat_id`), calls `admin_config.rehome_chat()` to rename the data directory + update all references, and calls `rehome_history()` to re-key in-memory histories.

### DM trip access (dm_router.py)

Group members can DM the bot to work on shared trips privately, without posting in the group.

**Setup flow:**
1. Group admin: `!claude dm enable` → generates 8-char join code, posts it in the group
2. Member DMs the bot: `!claude join <code>` → DM linked to that group
3. In DM, use `#tripname` to route to the correct group's data (existing selector, no new syntax)

**DM commands:**
| Command | Action |
|---------|--------|
| `!claude join <code>` | Link DM to a group |
| `!claude dm linked` | List linked groups and their trips |
| `!claude dm unlink [chat_id]` | Remove a link |
| `!claude dm enable` | (Group admin) Generate join code |
| `!claude dm disable` | (Group admin) Revoke join code |

**DM activity summary:** Any DM action that changes data (booking saved, Claude response) is logged to `dm_activity.json`. On the group's next `!claude` command, the bot posts a brief summary of DM activity first, then responds normally.

---

## Key Module Notes

### `telegram/bot.py`
- `python-telegram-bot` 22.x with `asyncio` + `JobQueue`
- `BotManager.start()` / `stop()` / `join()` manage the bot thread lifecycle
- `_setup_handlers(app)` adds message, chat member, and migration handlers; schedules daily email scan at 08:00
- `process_message` → dispatches to `_handle_dm` (DM) or `_process_group_message` (group/DM-routed)
- `chunk_message` splits long replies at paragraph/sentence boundaries (Telegram 4096-char limit)
- `_responded_ids` tracks which message IDs have been answered to prevent double-responses on edit
- `_evict_message_texts()` / `_evict_responded_ids()` cap unbounded dicts (2000 messages, 500 IDs per chat)

### `shared/claude_client.py`
- Model: `claude-haiku-4-5-20251001`
- Agentic loop: keeps calling Claude until `stop_reason == "end_turn"`, capped at `MAX_TOOL_ITERATIONS = 10`
- `MAX_HISTORY = 20` messages in RAM per trip; overflow triggers summarization to disk
- System prompt rebuilt each call: base instructions + rolling summary + current bookings block
- Booking tools: `add_booking`, `list_bookings`, `update_booking`, `remove_booking`
- Search tools: `search_flights`, `search_hotels`, `search_rentals`, `search_places`, `search_reviews`, `search_events`, `search_explore`
- URL content is prepended to the user message when URLs are detected (`web_fetcher.py`)

### `shared/bookings.py`
- One JSON file per trip: `data/{safe_chat_id}/{trip_name}_bookings.json`
- `add_booking` auto-generates ID: `{type[0]}{count:03d}_{uuid4[:4]}` (e.g., `f001_3a7c`)
- `format_for_prompt` → compact string injected into system prompt
- `format_for_telegram` → Markdown for display in chat
- Booking types: `flight`, `hotel`, `rental`, `activity`

### `shared/email_scanner.py`
- Gmail IMAP with `X-GM-RAW` for full Gmail search syntax
- `after:` date uses Gmail format (`YYYY/MM/DD`); subject terms use parentheses not inner quotes
- Each email → Claude Haiku → structured JSON (or `{"is_booking": false}`)
- Unseen emails tracked in `scanned_email_ids.json` to prevent reprocessing
- `scan_for_bookings(chat_id, trip_name)` is the main entry point

---

## LaunchDaemon

The plist uses `KeepAlive.SuccessfulExit=false`: launchd restarts the bot only on non-zero exit (crash or Reload), not on clean exit (Quit or manual `launchctl stop`).

The plist template in `launchd/` contains `__PROJECT_DIR__` and `__USERNAME__` placeholders. `install_daemon.sh` substitutes them and loads the daemon:

```bash
./install_daemon.sh
```

**Useful launchctl commands:**
```bash
sudo launchctl list com.vacationbot.telegram          # status + last exit code
sudo launchctl kickstart system/com.vacationbot.telegram   # start
sudo launchctl kickstart -k system/com.vacationbot.telegram  # restart
sudo launchctl stop com.vacationbot.telegram          # stop (no restart)
pgrep -f "telegram/bot.py" | wc -l                   # should be 1
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
| `!claude dm enable` | (Group admin) Generate DM join code |
| `!claude dm disable` | (Group admin) Revoke DM join code |
| `!claude join <code>` | (DM) Link DM to a group |
| `!claude dm linked` | (DM) List linked groups + trips |
| `!claude dm unlink [chat_id]` | (DM) Remove a group link |
| `!claude admin list` | (Owner DM) Show allowed + pending groups |
| `!claude admin allow <id>` | (Owner DM) Approve a group |
| `!claude admin deny <id>` | (Owner DM) Remove from pending |
| `!claude admin revoke <id>` | (Owner DM) Remove from allowed + leave |
| `!claude admin rehome <old> <new>` | (Owner DM) Manual chat ID migration |

---

## Dependencies

```bash
pip install "python-telegram-bot[job-queue]"
pip install anthropic python-dotenv requests beautifulsoup4 serpapi rumps
```

Python 3.11+ required (uses `list[int]`, `int | None` type hints).

---

## `.env` formatting

Each variable must be on its own line. A missing newline between two keys causes silent misreads — e.g. `TRIGGER_WORD=!claudeGMAIL_ADDRESS=foo` makes the bot ignore all messages (wrong trigger word) and breaks email scanning (`GMAIL_ADDRESS` never set). If the bot stops responding, check this first.

---

## Known Issues / Pending

1. **Bot token rotation** — token was exposed in chat logs; revoke via BotFather (`/mybots` → API Token → Revoke) and update `.env`
2. **`ALLOWED_CHAT_ID` not set** — daily email scan will silently skip; set to the Telegram group chat ID
