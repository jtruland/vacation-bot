"""
macOS menu bar tray for the Vacation Planning Bot.
Monitors the Telegram bot LaunchDaemon and provides start/stop/restart controls.

Requires: pip install rumps
"""

import os
import re
import sys
import subprocess
import threading
import logging
import rumps

# ─── Config ────────────────────────────────────────────────────────────────────

SERVICE_LABEL  = "com.vacationbot.telegram"
SERVICE_PATH   = f"system/{SERVICE_LABEL}"
PROJECT_DIR    = os.path.expanduser("~/projects/vacation-bot")
BOT_SCRIPT     = os.path.join(PROJECT_DIR, "telegram", "bot.py")
LOG_FILE       = os.path.join(PROJECT_DIR, "logs", "telegram.log")
TRAY_LOG       = os.path.join(PROJECT_DIR, "logs", "tray.log")
LOG_LINES      = 50
POLL_INTERVAL  = 15

# ─── Tray-specific logging ─────────────────────────────────────────────────────

os.makedirs(os.path.join(PROJECT_DIR, "logs"), exist_ok=True)
logging.basicConfig(
    filename=TRAY_LOG,
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)s  %(message)s",
)
log = logging.getLogger("tray")


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout + r.stderr).strip()
        log.debug("cmd=%s  rc=%d  out=%s", cmd, r.returncode, out[:300])
        return r.returncode, out
    except subprocess.TimeoutExpired:
        return -1, "command timed out"
    except Exception as e:
        return -1, str(e)


def _run_privileged(shell_cmd: str) -> tuple[bool, str]:
    """Run a launchctl command via sudo (no password prompt — requires visudo entry)."""
    args = ["sudo"] + shell_cmd.split()
    log.debug("sudo: %s", args)
    rc, out = _run(args, timeout=15)
    return rc == 0, out


def _pgrep_pids() -> list[int]:
    """Return PIDs of any running bot.py process, regardless of how it was launched."""
    rc, out = _run(["pgrep", "-f", "telegram/bot.py"])
    if rc != 0 or not out.strip():
        return []
    pids = []
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def _ppid_of(pid: int) -> int | None:
    """Return the parent PID of a given process. Returns None on error."""
    rc, out = _run(["ps", "-o", "ppid=", "-p", str(pid)])
    if rc != 0:
        return None
    out = out.strip()
    return int(out) if out.isdigit() else None


def _daemon_exit_status() -> int | None:
    """Read LastExitStatus from launchctl list (no privileges needed for this field)."""
    rc, out = _run(["launchctl", "list", SERVICE_LABEL])
    if rc != 0:
        return None
    m = re.search(r'"LastExitStatus"\s*=\s*(\d+)', out)
    return int(m.group(1)) if m else None


def _service_info() -> dict:
    """
    Status check via pgrep + parent-PID inspection.

    A bot.py process is considered daemon-managed if its parent PID is 1 (launchd).
    Any process with a different parent was started manually from a terminal/shell.

    Returns:
        running     bool
        pids        list[int]   – all bot.py PIDs found
        daemon_pids list[int]   – pids owned by launchd (ppid == 1)
        orphan_pids list[int]   – pids NOT owned by launchd
        orphan      bool        – True if ANY orphan exists
        exit_status int | None  – LastExitStatus when stopped
    """
    pids = _pgrep_pids()

    daemon_pids = []
    orphan_pids = []
    for pid in pids:
        ppid = _ppid_of(pid)
        log.debug("pid=%d ppid=%s", pid, ppid)
        if ppid == 1:
            daemon_pids.append(pid)
        else:
            orphan_pids.append(pid)

    running = len(pids) > 0
    orphan  = len(orphan_pids) > 0

    exit_status = None
    if not running:
        exit_status = _daemon_exit_status()

    log.debug("service_info: running=%s daemon_pids=%s orphan_pids=%s exit=%s",
              running, daemon_pids, orphan_pids, exit_status)

    return {
        "running":     running,
        "pids":        pids,
        "daemon_pids": daemon_pids,
        "orphan_pids": orphan_pids,
        "orphan":      orphan,
        "exit_status": exit_status,
    }


def _tail_log(n: int) -> str:
    if not os.path.exists(LOG_FILE):
        return (
            f"No log file at:\n{LOG_FILE}\n\n"
            "The bot hasn't written any output yet.\n"
            "If Start succeeded, check tray.log for launchctl errors."
        )
    try:
        rc, out = _run(["tail", "-n", str(n), LOG_FILE])
        return out if out else "(log file is empty)"
    except Exception as e:
        return f"Could not read log: {e}"


# ─── Tray App ──────────────────────────────────────────────────────────────────

class VacationBotTray(rumps.App):

    def __init__(self):
        super().__init__("🟡 Bot", quit_button=None)
        self._info: dict = {}

        self._lbl_status  = rumps.MenuItem("● Checking…")
        self._lbl_pid     = rumps.MenuItem("")
        self._lbl_orphan  = rumps.MenuItem("")

        self._btn_restart = rumps.MenuItem("↺  Restart",              callback=self._on_restart)
        self._btn_start   = rumps.MenuItem("▶  Start",                callback=self._on_start)
        self._btn_stop    = rumps.MenuItem("■  Stop",                 callback=self._on_stop)
        self._btn_kill    = rumps.MenuItem("⚡  Kill Orphan Process",  callback=self._on_kill_orphan)
        self._btn_logs    = rumps.MenuItem("📋  View Logs",            callback=self._on_view_logs)
        self._btn_logfile = rumps.MenuItem("📂  Open Log File",        callback=self._on_open_log)
        self._btn_traylog = rumps.MenuItem("🔧  Open Tray Log",        callback=self._on_open_tray_log)
        self._btn_refresh = rumps.MenuItem("⟳  Refresh Now",          callback=self._on_refresh)
        self._btn_quit    = rumps.MenuItem("Quit Tray",                callback=self._on_quit)

        self.menu = [
            self._lbl_status,
            self._lbl_pid,
            self._lbl_orphan,
            None,
            self._btn_restart,
            self._btn_start,
            self._btn_stop,
            self._btn_kill,
            None,
            self._btn_logs,
            self._btn_logfile,
            self._btn_traylog,
            None,
            self._btn_refresh,
            None,
            self._btn_quit,
        ]

        self._refresh()
        self._timer = rumps.Timer(lambda _: self._refresh(), POLL_INTERVAL)
        self._timer.start()

    # ── Status ────────────────────────────────────────────────────────────────

    def _refresh(self):
        info = _service_info()
        self._info = info

        daemon_pids = info["daemon_pids"]
        orphan_pids = info["orphan_pids"]
        running     = info["running"]
        orphan      = info["orphan"]
        es          = info.get("exit_status")

        if daemon_pids:
            # Daemon is running cleanly
            self.title = "🟢 Bot"
            self._lbl_status.title = "✅  Bot is running  (daemon)"
            self._lbl_pid.title    = f"    PID {daemon_pids[0]}"
            self._lbl_orphan.title = (
                f"    + orphan PID {', '.join(str(p) for p in orphan_pids)}  ← ⚡ to clean up"
                if orphan_pids else ""
            )

        elif orphan_pids:
            # Only orphan(s), no daemon process
            self.title = "🟠 Bot"
            self._lbl_status.title = "⚠️  Bot running — NOT via daemon"
            self._lbl_pid.title    = f"    PID {', '.join(str(p) for p in orphan_pids)}"
            self._lbl_orphan.title = "    ← use ⚡ Kill Orphan, then ▶ Start"

        else:
            self.title = "🔴 Bot"
            if es is not None and es != 0:
                self._lbl_status.title = f"⛔  Bot stopped  (exit {es})"
                self._lbl_pid.title    = "    Check 📋 View Logs for crash details"
            else:
                self._lbl_status.title = "⛔  Bot is stopped"
                self._lbl_pid.title    = ""
            self._lbl_orphan.title = ""

    # ── Controls ──────────────────────────────────────────────────────────────

    def _on_start(self, _):
        if self._info.get("running") and not self._info.get("orphan"):
            rumps.alert("Bot is already running.", ok="OK")
            return
        if self._info.get("orphan"):
            rumps.alert(
                title="Orphan Process Detected",
                message=(
                    "A bot.py process is already running outside the daemon.\n\n"
                    "Click ⚡ Kill Orphan Process first, then try Start again."
                ),
                ok="OK"
            )
            return
        self._set_busy("Starting…")
        threading.Thread(target=self._do_start, daemon=True).start()

    def _do_start(self):
        ok, msg = _run_privileged(f"launchctl kickstart {SERVICE_PATH}")
        if not ok and msg != "cancelled":
            self._alert_error("Start Failed", msg or "launchctl returned an error with no message.")
        self._refresh()

    def _on_stop(self, _):
        if not self._info.get("running"):
            rumps.alert("Bot is not running.", ok="OK")
            return
        self._set_busy("Stopping…")
        threading.Thread(target=self._do_stop, daemon=True).start()

    def _do_stop(self):
        ok, msg = _run_privileged(f"launchctl stop {SERVICE_LABEL}")
        if not ok and msg != "cancelled":
            self._alert_error("Stop Failed", msg or "launchctl returned an error with no message.")
        self._refresh()

    def _on_restart(self, _):
        if self._info.get("orphan"):
            rumps.alert(
                title="Orphan Process Detected",
                message=(
                    "A bot.py process is running outside the daemon.\n\n"
                    "Click ⚡ Kill Orphan Process first, then restart."
                ),
                ok="OK"
            )
            return
        self._set_busy("Restarting…")
        threading.Thread(target=self._do_restart, daemon=True).start()

    def _do_restart(self):
        ok, msg = _run_privileged(f"launchctl kickstart -k {SERVICE_PATH}")
        if not ok and msg != "cancelled":
            self._alert_error("Restart Failed", msg or "launchctl returned an error with no message.")
        self._refresh()

    def _on_kill_orphan(self, _):
        pids = self._info.get("orphan_pids", [])
        if not pids:
            rumps.alert("No orphan bot process found.", ok="OK")
            return
        pid_str = ", ".join(str(p) for p in pids)
        # Orphan processes are owned by the current user — no sudo needed
        rc, msg = _run(["kill", "-9"] + [str(p) for p in pids])
        if rc == 0:
            rumps.alert(
                title="Orphan Killed",
                message=f"Terminated PID {pid_str}.\n\nYou can now press ▶ Start.",
                ok="OK"
            )
        else:
            self._alert_error("Kill Failed", msg)
        self._refresh()

    # ── Logs ──────────────────────────────────────────────────────────────────

    def _on_view_logs(self, _):
        text = _tail_log(LOG_LINES)
        if len(text) > 3500:
            text = "…(earlier lines omitted)\n\n" + text[-3500:]
        rumps.alert(title=f"Bot Logs  (last {LOG_LINES} lines)", message=text, ok="Close")

    def _on_open_log(self, _):
        if os.path.exists(LOG_FILE):
            subprocess.Popen(["open", "-a", "Console", LOG_FILE])
        else:
            log_dir = os.path.dirname(LOG_FILE)
            subprocess.Popen(["open", log_dir if os.path.isdir(log_dir) else PROJECT_DIR])
            rumps.alert(
                title="Log File Not Found",
                message=(
                    f"No log at:\n{LOG_FILE}\n\n"
                    "Opened the logs folder instead.\n"
                    "If the bot has never run, try ▶ Start, wait 5 seconds, then check again."
                ),
                ok="OK"
            )

    def _on_open_tray_log(self, _):
        if os.path.exists(TRAY_LOG):
            subprocess.Popen(["open", "-a", "Console", TRAY_LOG])
        else:
            rumps.alert("Tray log not found.", ok="OK")

    # ── Misc ──────────────────────────────────────────────────────────────────

    def _on_refresh(self, _):
        self.title = "🟡 Bot"
        self._lbl_status.title = "● Refreshing…"
        self._refresh()

    def _on_quit(self, _):
        self._timer.stop()
        rumps.quit_application()

    def _set_busy(self, label: str):
        self.title = "🟡 Bot"
        self._lbl_status.title = f"🔄  {label}"
        self._lbl_pid.title    = ""
        self._lbl_orphan.title = ""

    @staticmethod
    def _alert_error(title: str, msg: str):
        log.error("%s: %s", title, msg)
        rumps.alert(title=title, message=msg, ok="OK")


# ─── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("Tray starting  pid=%d  python=%s", os.getpid(), sys.version.split()[0])
    VacationBotTray().run()
