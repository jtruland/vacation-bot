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
pip install "python-telegram-bot[job-queue]" anthropic python-dotenv requests serpapi rumps
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
ALLOWED_CHAT_ID=              # optional — your Telegram group chat ID
GMAIL_ADDRESS=...             # optional — for email scanning
GMAIL_APP_PASSWORD=...        # 16-char app password from myaccount.google.com/apppasswords
```

To find your group chat ID, add the bot to the group, send a message, then check:
```
https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
```

---

## Running Manually (development / testing)

```bash
cd ~/projects/vacation-bot
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
