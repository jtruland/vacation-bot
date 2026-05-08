#!/bin/bash
# Shell wrapper for the vacation bot tray app.
# Used by both manual launch and the LaunchAgent plist.

PROJECT_DIR="/Users/jon/projects/vacation-bot"
VENV_PYTHON="$PROJECT_DIR/venv/bin/python3"
TRAY_SCRIPT="$PROJECT_DIR/tray/bot_tray.py"
LOG_DIR="$PROJECT_DIR/logs"

mkdir -p "$LOG_DIR"

# Ensure rumps is installed in the venv
"$VENV_PYTHON" -c "import rumps" 2>/dev/null || {
    echo "Installing rumps into venv..."
    "$PROJECT_DIR/venv/bin/pip" install rumps --quiet
}

exec "$VENV_PYTHON" "$TRAY_SCRIPT"
