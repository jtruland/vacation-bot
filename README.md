# Vacation Planning Bot

A Claude-powered Telegram bot for family group chats. Ask about flights, hotels, rentals, and activities — Claude searches for live data and remembers what you've booked.

## Prerequisites

- macOS (tray requires AppKit; the bot itself runs headless on Linux)
- Python 3.11+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- An [Anthropic API key](https://console.anthropic.com/)
- A [SerpApi key](https://serpapi.com/) (250 free searches/month)
- A Gmail app password for email scanning (optional)

---

## First-time Setup

### 1. Clone and create the virtual environment

```bash
git clone https://github.com/jtruland/vacation-bot.git
cd vacation-bot
python3 -m venv venv
source venv/bin/activate
pip install "python-telegram-bot[job-queue]" anthropic python-dotenv requests beautifulsoup4 serpapi rumps
```

### 2. Configure environment variables

```bash
cp env.template .env
```

Edit `.env` and fill in your keys:

```
TELEGRAM_BOT_TOKEN=...        # from BotFather
ANTHROPIC_API_KEY=...
SERPAPI_KEY=...
BOT_OWNER_ID=...              # your Telegram user ID (find it via @userinfobot)
ALLOWED_CHAT_IDS=             # optional — comma-separated group chat IDs to pre-allow on first run
GMAIL_ADDRESS=...             # optional — for email scanning
GMAIL_APP_PASSWORD=...        # 16-char app password from myaccount.google.com/apppasswords
```

> **Important:** each key must be on its own line with no trailing content. A missing newline between two keys causes both to be silently misread — the most common symptom is the bot not responding to any messages because `TRIGGER_WORD` loaded incorrectly.

**Finding your Telegram user ID:** message [@userinfobot](https://t.me/userinfobot) on Telegram — it replies with your numeric ID.

To find your group chat ID, add the bot to the group, send a message, then check:
```
https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
```

---

## Running Manually (development / testing)

```bash
cd vacation-bot
source venv/bin/activate
python3 telegram/bot.py
```

The tray icon will appear in the menu bar. Logs are written to `logs/telegram.log`.

To run headless (no tray, e.g. on a server):
```bash
pip uninstall rumps   # or just don't install it
python3 telegram/bot.py
```

---

## Running as a LaunchDaemon (production)

The LaunchDaemon starts the bot automatically at boot and restarts it if it crashes.

### Install

Run the install script from the project root — it detects the current directory and username automatically:

```bash
./install_daemon.sh
```

This substitutes the correct paths into the plist template and loads the daemon. The bot starts immediately and will restart on every reboot.

### Update after code changes

```bash
git pull
# Use Reload from the tray, or force-restart via launchctl:
sudo launchctl kickstart -k system/com.vacationbot.telegram
```

To reinstall the daemon after pulling plist changes:

```bash
./install_daemon.sh
```

### Other launchctl commands

```bash
# Check status and last exit code
sudo launchctl list com.vacationbot.telegram

# Stop (does not restart)
sudo launchctl stop com.vacationbot.telegram

# Start
sudo launchctl kickstart system/com.vacationbot.telegram

# Force restart
sudo launchctl kickstart -k system/com.vacationbot.telegram

# Uninstall
sudo launchctl bootout system/com.vacationbot.telegram
sudo rm /Library/LaunchDaemons/com.vacationbot.telegram.plist
```

---

## Tray Controls

When running on macOS the bot icon appears in the menu bar:

| Icon | Meaning |
|------|---------|
| 🟢 | Bot is running and polling |
| 🔴 | Bot is stopped or crashed |
| 🟡 | Starting or stopping |

| Menu item | Action |
|-----------|--------|
| **Start Bot** | Start polling (available when stopped) |
| **Stop Bot** | Stop polling gracefully; tray stays up |
| **Reload** | Stop + restart the whole process via launchd (picks up code changes) |
| **Quit** | Stop + exit cleanly; launchd does **not** restart |
| **View Logs** | Show last 60 lines of `logs/telegram.log` in an alert |
| **Open Log File** | Open `logs/telegram.log` in Console.app |

> **Reload vs Quit**: Reload exits with a non-zero code so launchd restarts the process — useful after pulling code changes. Quit exits cleanly so launchd leaves it stopped.

---

## Logs

All output is written to `logs/telegram.log` (created automatically). View it live:

```bash
tail -f logs/telegram.log
```

---

## Bot Commands

Type `!claude help` in the Telegram group for the full reference. Quick overview:

| Command | Description |
|---------|-------------|
| `!claude trip new <name>` | Create a trip |
| `!claude trips` | List all trips |
| `!claude trip default <name>` | Set default trip |
| `!claude <question>` | Ask Claude (searches live data as needed) |
| `!claude #tripname <question>` | Ask about a specific trip |
| `!claude booked` | Show confirmed bookings as a day-by-day itinerary |
| `!claude scan email` | Scan Gmail for booking confirmations |
| `!claude book save all` | Save all found bookings |
| `!claude book save 1 3` | Save specific found bookings by number |
| `!claude book skip` | Discard found bookings without saving |
| `!claude book remove <id>` | Remove a booking by ID |
| `!claude book edit <id> field=value ...` | Edit booking fields (title, start_date, end_date, confirmation, cost, notes) |
| `!claude reset` | Clear in-memory conversation history |
| `!claude summarize` | Save a planning summary to disk |
| `!claude flights JFK Rome 2026-07-15 2026-07-25 2` | Direct flight search |
| `!claude hotels Rome 2026-07-15 2026-07-22 2` | Direct hotel search |
| `!claude places best trattorias in Trastevere` | Place search |
| `!claude events Florence 2026-07-20` | Local events |
| `!claude dm enable` | (Group admin) Generate DM join code for private access |
| `!claude join <code>` | (DM) Link your DM to a group's trips |
| `!claude dm linked` | (DM) See which groups you're linked to |

---

## Troubleshooting

**Bot doesn't respond to any messages**
Two common causes: (1) Check that `TRIGGER_WORD` is on its own line in `.env` with no other content appended. If the line is malformed (e.g. `TRIGGER_WORD=!claudeGMAIL_ADDRESS=...`), the bot loads the wrong trigger word and silently ignores everything. (2) The group may not be in the allowed list — check `data/admin_config.json` or DM the bot as the owner with `!claude admin list`.

**Bot was added to a group but never started responding**
The group needs to be approved. If `BOT_OWNER_ID` is set, you should have received a DM with approve/deny commands. If not, DM the bot with `!claude admin allow <chat_id>`.

**Email scanning fails with "GMAIL_ADDRESS must be set"**
Same root cause — if `GMAIL_ADDRESS` is concatenated onto the end of another variable it will never be loaded. Verify each key is on its own line.

**Bot responds in development but not under launchd**
Ensure the daemon is loading `.env` from the correct path. Check `logs/telegram.log` for startup errors:
```bash
tail -20 logs/telegram.log
```

**409 Conflict error in logs**
Another bot process is already polling. Check for duplicate processes:
```bash
pgrep -fl "telegram/bot.py"
```
Kill any extras, then restart via the tray or `launchctl`.
