"""
macOS menu bar tray for the Vacation Planning Bot.
Runs on the main thread inside the bot process — no separate launcher needed.

Requires: pip install rumps
"""

import logging
import os
import subprocess
import threading

import rumps

LOG_FILE  = os.path.join(os.path.dirname(__file__), '..', 'logs', 'telegram.log')
LOG_LINES = 60

log = logging.getLogger(__name__)


class VacationBotTray(rumps.App):

    def __init__(self, bot_manager) -> None:
        super().__init__("🟡 Bot", quit_button=None)
        self._bot = bot_manager

        self._lbl_status  = rumps.MenuItem("● Starting…")
        self._btn_start   = rumps.MenuItem("▶  Start Bot",      callback=self._on_start)
        self._btn_stop    = rumps.MenuItem("■  Stop Bot",        callback=self._on_stop)
        self._btn_reload      = rumps.MenuItem("↺  Reload",          callback=self._on_reload)
        self._btn_pull_reload = rumps.MenuItem("⬇︎  Pull & Reload",   callback=self._on_pull_reload)
        self._btn_logs    = rumps.MenuItem("📋  View Logs",       callback=self._on_view_logs)
        self._btn_logfile = rumps.MenuItem("📂  Open Log File",   callback=self._on_open_log)
        self._btn_quit    = rumps.MenuItem("Quit",                callback=self._on_quit)

        self.menu = [
            self._lbl_status,
            None,
            self._btn_start,
            self._btn_stop,
            self._btn_reload,
            self._btn_pull_reload,
            None,
            self._btn_logs,
            self._btn_logfile,
            None,
            self._btn_quit,
        ]

        self._update_ui()
        self._timer = rumps.Timer(lambda _: self._update_ui(), 5)
        self._timer.start()

    # ── UI state ──────────────────────────────────────────────────────────────

    def _update_ui(self) -> None:
        status = self._bot.status
        _icons = {
            "running":  ("🟢 Bot", "✅  Bot is running"),
            "stopped":  ("🔴 Bot", "⛔  Bot is stopped"),
            "error":    ("🔴 Bot", "⚠️  Bot crashed — check logs"),
            "starting": ("🟡 Bot", "🔄  Starting…"),
            "stopping": ("🟡 Bot", "🔄  Stopping…"),
        }
        icon, label = _icons.get(status, ("🟡 Bot", f"● {status}"))
        self.title = icon
        self._lbl_status.title = label

        running = status == "running"
        stopped = status in ("stopped", "error")
        self._btn_start.set_callback(self._on_start if stopped else None)
        self._btn_stop.set_callback(self._on_stop if running else None)

    # ── Controls ──────────────────────────────────────────────────────────────

    def _on_start(self, _) -> None:
        threading.Thread(target=self._bot.start, daemon=True).start()

    def _on_stop(self, _) -> None:
        threading.Thread(target=self._bot.stop, daemon=True).start()

    def _on_reload(self, _) -> None:
        """Stop the bot cleanly, then exit non-zero so launchd restarts the process."""
        def _do() -> None:
            log.info("Reload requested — stopping bot then exiting for launchd restart")
            self._bot.stop()
            os._exit(1)
        threading.Thread(target=_do, daemon=True).start()

    def _on_pull_reload(self, _) -> None:
        """Pull latest code from git, then restart via launchd."""
        def _do() -> None:
            log.info("Pull & Reload requested — stopping bot")
            self._bot.stop()
            project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
            log.info("Running git pull in %s", project_root)
            result = subprocess.run(
                ["git", "pull", "origin", "main"],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                log.info("git pull: %s", result.stdout.strip())
            else:
                log.error("git pull failed (exit %d): %s", result.returncode, result.stderr.strip())
            os._exit(1)
        threading.Thread(target=_do, daemon=True).start()

    def _on_quit(self, _) -> None:
        """Stop the bot cleanly, then exit with 0 — launchd will NOT restart."""
        def _do() -> None:
            log.info("Quit requested — stopping bot cleanly")
            self._bot.stop()
            os._exit(0)
        threading.Thread(target=_do, daemon=True).start()

    # ── Logs ──────────────────────────────────────────────────────────────────

    def _on_view_logs(self, _) -> None:
        log_path = os.path.abspath(LOG_FILE)
        if not os.path.exists(log_path):
            rumps.alert("No log file found yet — has the bot started?", ok="OK")
            return
        try:
            result = subprocess.run(
                ["tail", "-n", str(LOG_LINES), log_path],
                capture_output=True, text=True, timeout=5,
            )
            text = result.stdout or "(log file is empty)"
        except Exception as e:
            text = f"Could not read log: {e}"
        if len(text) > 3500:
            text = "…(earlier lines omitted)\n\n" + text[-3500:]
        rumps.alert(title=f"Bot Logs (last {LOG_LINES} lines)", message=text, ok="Close")

    def _on_open_log(self, _) -> None:
        log_path = os.path.abspath(LOG_FILE)
        if os.path.exists(log_path):
            subprocess.Popen(["open", "-a", "Console", log_path])
        else:
            rumps.alert("Log file not found yet — has the bot started?", ok="OK")
