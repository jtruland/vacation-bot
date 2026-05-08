#!/bin/bash
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$PROJECT_DIR/venv/bin/activate"
exec python3 "$PROJECT_DIR/telegram/bot.py"
