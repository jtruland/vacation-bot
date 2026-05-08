#!/bin/bash
# Installs or reinstalls the Telegram bot LaunchDaemon.
# Run from any directory — it locates itself automatically.
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
USERNAME="$(whoami)"
PLIST_TEMPLATE="$PROJECT_DIR/launchd/com.vacationbot.telegram.plist"
PLIST_DST="/Library/LaunchDaemons/com.vacationbot.telegram.plist"

echo "Project dir : $PROJECT_DIR"
echo "Username    : $USERNAME"
echo "Installing  : $PLIST_DST"

# Substitute placeholders and write to a temp file
sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__USERNAME__|$USERNAME|g" \
    "$PLIST_TEMPLATE" > /tmp/com.vacationbot.telegram.plist

# Unload existing daemon if running
sudo launchctl bootout system/com.vacationbot.telegram 2>/dev/null || true

# Install and load
sudo cp /tmp/com.vacationbot.telegram.plist "$PLIST_DST"
sudo launchctl bootstrap system "$PLIST_DST"

echo "Done. Bot is starting — check logs/telegram.log"
