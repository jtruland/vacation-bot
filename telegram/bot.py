import asyncio
import datetime
import logging
import os
import sys
import threading

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes, MessageHandler, filters
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from shared.claude_client import (
    ask_claude, clear_history, save_summarize_now, strip_trigger,
    parse_trip_selector, get_trips, get_default_trip, has_trips,
    create_trip, set_default_trip, delete_trip
)
from shared.serpapi_client import (
    search_flights, search_hotels, search_rentals,
    search_places, search_reviews, search_events, search_explore
)
from shared.bookings import format_for_telegram as _format_bookings_telegram
from shared.email_scanner import scan_for_bookings, mark_seen
from shared.pending_bookings import (
    set_pending, get_pending, clear_pending, has_pending,
    pick_pending, format_pending_for_telegram,
)
from shared.bookings import add_booking as _add_booking

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

# ─── Logging ───────────────────────────────────────────────────────────────────

PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
LOG_DIR  = os.path.join(PROJECT_DIR, 'logs')
LOG_FILE = os.path.join(LOG_DIR, 'telegram.log')


def _setup_logging() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = logging.Formatter('%(asctime)s  %(name)s  %(levelname)s  %(message)s')

    fh = logging.FileHandler(LOG_FILE)
    fh.setFormatter(fmt)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    root.addHandler(sh)


_setup_logging()
logger = logging.getLogger(__name__)

# ─── Config ────────────────────────────────────────────────────────────────────

TRIGGER_WORD   = os.getenv("TRIGGER_WORD", "!claude")
ALLOWED_CHAT_ID = os.getenv("ALLOWED_CHAT_ID")

# Tracks original text for every message we've seen, keyed by (chat_id, message_id)
_message_texts: dict[tuple[str, int], str] = {}

# Tracks which message IDs we've already responded to, keyed by chat_id
_responded_ids: dict[str, set[int]] = {}

HELP_TEXT = (
    "🗺️ *Vacation Planning Bot*\n\n"
    "Just ask naturally — Claude will search for live data when needed:\n"
    "  _\"What flights are available from JFK to Rome in July?\"_\n"
    "  _\"Find us a villa in Tuscany for a week in July, 6 people\"_\n"
    "  _\"Best restaurants near the Colosseum\"_\n"
    "  _\"What events are happening in Florence that week?\"_\n"
    "  _\"What do people think of Hotel de Russie?\"_\n\n"
    "*Quick search shortcuts:*\n"
    "  `!claude flights JFK Rome 2026-07-15 2026-07-25 2`\n"
    "  `!claude hotels Rome 2026-07-15 2026-07-22 2`\n"
    "  `!claude rentals Tuscany 2026-07-15 2026-07-22 6`\n"
    "  `!claude places best trattorias in Trastevere`\n"
    "  `!claude reviews Colosseum Rome`\n"
    "  `!claude events Florence 2026-07-20`\n"
    "  `!claude explore Amalfi Coast`\n\n"
    "*Trip management:*\n"
    "  `!claude trips` — list all trips\n"
    "  `!claude trip new <name>` — create a trip\n"
    "  `!claude trip default <name>` — set default trip\n"
    "  `!claude trip delete <name>` — _(admin only)_\n\n"
    "*Memory:*\n"
    "  `!claude reset` — clear recent conversation\n"
    "  `!claude reset #tripname` — clear a specific trip\n"
    "  `!claude summarize` — save planning summary now\n"
    "  `!claude summarize #tripname` — save for specific trip\n\n"
    "*Bookings:*\n"
    "  `!claude booked` — show confirmed bookings for current trip\n"
    "  `!claude booked #tripname` — show bookings for a specific trip\n"
    "  _Just tell Claude you've booked something and it will record it_\n"
    "  _\"We booked the villa — €1,200, conf ABC123\"_\n\n"
    "*Email scanning:*\n"
    "  `!claude scan email` — scan Gmail for booking confirmations\n"
    "  `!claude book save all` — save all found bookings\n"
    "  `!claude book save 1 3` — save specific items by number\n"
    "  `!claude book skip` — discard without saving\n"
    "  _Scans also run automatically at 8am daily_\n\n"
    "  `!claude help` — show this message\n\n"
    "_Paste any travel link and Claude will read it._\n"
    "_Dates: YYYY-MM-DD or MM/DD/YYYY. Prices in USD._"
)

# ─── Message helpers ────────────────────────────────────────────────────────────

def chunk_message(text: str, max_length: int = 4000) -> list[str]:
    if len(text) <= max_length:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_length:
            chunks.append(text)
            break
        split_at = text.rfind('\n\n', 0, max_length)
        if split_at == -1:
            split_at = text.rfind('\n', 0, max_length)
        if split_at == -1:
            split_at = text.rfind('. ', 0, max_length)
            if split_at != -1:
                split_at += 1
        if split_at == -1:
            split_at = max_length
        chunks.append(text[:split_at].strip())
        text = text[split_at:].strip()
    return chunks


async def send_chunked(message, text: str, parse_mode: str = "Markdown") -> None:
    chunks = chunk_message(text)
    for i, chunk in enumerate(chunks):
        try:
            await message.reply_text(chunk, parse_mode=parse_mode)
        except Exception:
            try:
                await message.reply_text(chunk)
            except Exception as e:
                logger.error("Failed to send chunk %d/%d: %s", i + 1, len(chunks), e)


async def is_admin(message, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(message.chat_id, message.from_user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False

# ─── Handlers ──────────────────────────────────────────────────────────────────

async def process_message(message, context: ContextTypes.DEFAULT_TYPE, text: str, is_edit: bool = False) -> None:
    chat_id = str(message.chat_id)
    body = strip_trigger(text)
    lower = body.lower()

    if lower == "help" or body == "":
        await message.reply_text(HELP_TEXT, parse_mode="Markdown")
        return

    if not has_trips(chat_id) and not lower.startswith("trip new"):
        await message.reply_text(
            "👋 Welcome! No trip plans exist yet.\n\n"
            "Start by creating your first trip:\n"
            "`!claude trip new <name>`\n\n"
            "Give it a short name that describes the trip — like `italy2026`, `hawaii`, or `summer-beach`. "
            "It will be set as the default automatically.",
            parse_mode="Markdown"
        )
        return

    if lower == "trips":
        trips = get_trips(chat_id)
        default = get_default_trip(chat_id)
        if not trips:
            await message.reply_text("No trips yet. Create one with `!claude trip new <name>`.", parse_mode="Markdown")
        else:
            lines = [f"  • `{t}`{'  ← default' if t == default else ''}" for t in trips]
            await message.reply_text("🗺️ *Trip plans:*\n" + "\n".join(lines), parse_mode="Markdown")
        return

    if lower.startswith("trip new "):
        name = body[len("trip new "):].strip()
        success, result = create_trip(chat_id, name)
        if success:
            default = get_default_trip(chat_id)
            msg = f'✅ Trip "*{result}*" created!'
            if default == result:
                msg += " It's been set as the default."
            msg += f"\n\nStart planning: `!claude <question>` or `!claude #{result} <question>`"
            await message.reply_text(msg, parse_mode="Markdown")
        else:
            await message.reply_text(f"❌ {result}")
        return

    if lower.startswith("trip default "):
        name = body[len("trip default "):].strip()
        success, result = set_default_trip(chat_id, name)
        if success:
            await message.reply_text(f'✅ "*{result}*" is now the default trip.', parse_mode="Markdown")
        else:
            await message.reply_text(f"❌ {result}")
        return

    if lower.startswith("trip delete "):
        if not await is_admin(message, context):
            await message.reply_text("❌ Only group admins can delete trips.")
            return
        name = body[len("trip delete "):].strip()
        success, result = delete_trip(chat_id, name)
        if success:
            new_default = get_default_trip(chat_id)
            msg = f'🗑️ Trip "*{result}*" and its summary have been permanently deleted.'
            if new_default:
                msg += f'\n\nNew default trip: "*{new_default}*"'
            else:
                msg += "\n\nNo trips remaining. Create one with `!claude trip new <name>`."
            await message.reply_text(msg, parse_mode="Markdown")
        else:
            await message.reply_text(f"❌ {result}")
        return

    if lower == "reset" or lower.startswith("reset "):
        remainder = body[len("reset"):].strip()
        trip_selector, _ = parse_trip_selector(remainder)
        trip_name = trip_selector or get_default_trip(chat_id)
        if not trip_name:
            await message.reply_text("No default trip set. Use `!claude trips` to see your trips.", parse_mode="Markdown")
            return
        if trip_name not in get_trips(chat_id):
            await message.reply_text(f'❌ No trip called "{trip_name}" found.', parse_mode="Markdown")
            return
        clear_history(chat_id, trip_name)
        await message.reply_text(
            f'🗑️ Conversation history cleared for "*{trip_name}*". '
            f'Long-term summary is preserved. Starting fresh!',
            parse_mode="Markdown"
        )
        return

    if lower == "summarize" or lower.startswith("summarize "):
        remainder = body[len("summarize"):].strip()
        trip_selector, _ = parse_trip_selector(remainder)
        trip_name = trip_selector or get_default_trip(chat_id)
        if not trip_name:
            await message.reply_text("No default trip set. Use `!claude trips` to see your trips.", parse_mode="Markdown")
            return
        saved, used_trip = save_summarize_now(chat_id, trip_name)
        if saved:
            await message.reply_text(f'📝 Planning summary saved for "*{used_trip}*".', parse_mode="Markdown")
        else:
            await message.reply_text("Nothing to summarize yet — ask me something first!")
        return

    if lower == "booked" or lower.startswith("booked "):
        remainder = body[len("booked"):].strip()
        trip_selector, _ = parse_trip_selector(remainder)
        trip_name = trip_selector or get_default_trip(chat_id)
        if not trip_name:
            await message.reply_text("No default trip set. Use `!claude trips` to see your trips.", parse_mode="Markdown")
            return
        await send_chunked(message, _format_bookings_telegram(chat_id, trip_name))
        return

    if lower == "scan email" or lower.startswith("scan email "):
        remainder = body[len("scan email"):].strip()
        trip_selector, _ = parse_trip_selector(remainder)
        trip_name = trip_selector or get_default_trip(chat_id)
        if not trip_name:
            await message.reply_text("No default trip set.", parse_mode="Markdown")
            return
        if trip_name not in get_trips(chat_id):
            await message.reply_text(f'❌ No trip called "{trip_name}" found.', parse_mode="Markdown")
            return
        await context.bot.send_chat_action(chat_id=message.chat_id, action="typing")
        await message.reply_text(f"📬 Scanning your email for *{trip_name}* bookings…", parse_mode="Markdown")
        try:
            candidates = scan_for_bookings(chat_id, trip_name)
        except RuntimeError as e:
            await message.reply_text(f"❌ Email scan failed: {e}")
            return
        except Exception as e:
            await message.reply_text(f"⚠️ Unexpected error during scan: {e}")
            return
        if not candidates:
            await message.reply_text("✅ Scan complete — no new booking confirmations found.")
            return
        set_pending(chat_id, trip_name, candidates)
        await send_chunked(message, format_pending_for_telegram(chat_id, trip_name))
        return

    if lower.startswith("book save"):
        args_str = body[len("book save"):].strip()
        trip_selector, args_str = parse_trip_selector(args_str)
        trip_name = trip_selector or get_default_trip(chat_id)
        if not trip_name:
            await message.reply_text("No default trip set.", parse_mode="Markdown")
            return
        if not has_pending(chat_id, trip_name):
            await message.reply_text("No pending bookings to save. Run `!claude scan email` first.", parse_mode="Markdown")
            return
        pending = get_pending(chat_id, trip_name)
        if args_str.lower() == "all" or not args_str:
            to_save = pending
        else:
            try:
                indices = [int(n) for n in args_str.split()]
            except ValueError:
                await message.reply_text("❌ Use `!claude book save all` or `!claude book save 1 3`.", parse_mode="Markdown")
                return
            to_save = pick_pending(chat_id, trip_name, indices)
        if not to_save:
            await message.reply_text("❌ No valid items selected.")
            return
        saved, email_ids = [], []
        for b in to_save:
            clean = {k: v for k, v in b.items() if not k.startswith("_")}
            saved.append((_add_booking(chat_id, trip_name, clean), b.get("title", "Unnamed"), b.get("type", "")))
            if b.get("_email_msg_id"):
                email_ids.append(b["_email_msg_id"])
        if email_ids:
            mark_seen(chat_id, email_ids)
        clear_pending(chat_id, trip_name)
        icons = {"flight": "✈️", "hotel": "🏨", "rental": "🚗", "activity": "🎭"}
        lines = [f"✅ *{len(saved)} booking(s) saved to {trip_name}:*\n"]
        for bid, title, btype in saved:
            lines.append(f"  {icons.get(btype, '📌')} {title}  `{bid}`")
        lines.append("\nUse `!claude booked` to see all confirmed bookings.")
        await send_chunked(message, "\n".join(lines))
        return

    if lower == "book skip" or lower.startswith("book skip "):
        remainder = body[len("book skip"):].strip()
        trip_selector, _ = parse_trip_selector(remainder)
        trip_name = trip_selector or get_default_trip(chat_id)
        if not trip_name:
            await message.reply_text("No default trip set.")
            return
        pending = get_pending(chat_id, trip_name)
        if not pending:
            await message.reply_text("No pending bookings to skip.")
            return
        email_ids = [b["_email_msg_id"] for b in pending if b.get("_email_msg_id")]
        if email_ids:
            mark_seen(chat_id, email_ids)
        clear_pending(chat_id, trip_name)
        await message.reply_text("🗑️ Pending bookings discarded. They won't appear in future scans.")
        return

    search_commands = {
        "flights":  search_flights,
        "hotels":   search_hotels,
        "rentals":  search_rentals,
        "places":   search_places,
        "reviews":  search_reviews,
        "events":   search_events,
        "explore":  search_explore,
    }
    for cmd, fn in search_commands.items():
        if lower == cmd or lower.startswith(f"{cmd} "):
            args = body[len(cmd):].strip()
            await context.bot.send_chat_action(chat_id=message.chat_id, action="typing")
            try:
                result = fn(args)
            except Exception as e:
                logger.error("Search error (%s): %s", cmd, e)
                await message.reply_text(f"❌ Search failed: {e}")
                return
            await send_chunked(message, result)
            return

    trip_selector, question = parse_trip_selector(body)
    trip_name = trip_selector or get_default_trip(chat_id)

    if not trip_name:
        await message.reply_text(
            "No default trip set. Use `!claude trip default <name>` or `!claude trips` to see your options.",
            parse_mode="Markdown"
        )
        return

    if trip_name not in get_trips(chat_id):
        await message.reply_text(
            f'❌ No trip called "{trip_name}" found. Use `!claude trips` to see available trips.',
            parse_mode="Markdown"
        )
        return

    if not question:
        await message.reply_text(
            f'Ask me something about "*{trip_name}*"! Or type `!claude help` for all commands.',
            parse_mode="Markdown"
        )
        return

    await context.bot.send_chat_action(chat_id=message.chat_id, action="typing")
    try:
        response = ask_claude(question, chat_id, trip_name)
        header = f"_{trip_name}_\n" if trip_selector else ""
        await send_chunked(message, header + response)
    except RuntimeError as e:
        logger.error("Claude API error: %s", e)
        await message.reply_text(f"❌ Claude error: {e}")
    except Exception as e:
        logger.error("Unexpected error: %s", e)
        await message.reply_text(f"⚠️ Unexpected error: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return

    if ALLOWED_CHAT_ID and str(message.chat_id) != ALLOWED_CHAT_ID:
        logger.warning("Ignored message from unauthorized chat_id: %s", message.chat_id)
        return

    text = message.text.strip()
    chat_id = str(message.chat_id)
    message_id = message.message_id

    _message_texts[(chat_id, message_id)] = text

    if not text.lower().startswith(TRIGGER_WORD.lower()):
        return

    if chat_id not in _responded_ids:
        _responded_ids[chat_id] = set()
    _responded_ids[chat_id].add(message_id)

    body = strip_trigger(text).strip()
    lower = body.lower()
    instant_commands = {"help", "trips", "reset", "summarize", "booked", "book skip"}
    is_instant = (
        body == ""
        or lower in instant_commands
        or any(lower.startswith(p) for p in (
            "trip new ", "trip default ", "trip delete ",
            "reset ", "summarize ", "booked ",
            "book save ", "book skip ",
            "flights ", "hotels ", "rentals ",
            "places ", "reviews ", "events ", "explore ",
        ))
    )
    if not is_instant:
        await message.reply_text("🔍 On it, give me a moment…")

    await process_message(message, context, text)


async def handle_edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.edited_message
    if not message or not message.text:
        return

    if ALLOWED_CHAT_ID and str(message.chat_id) != ALLOWED_CHAT_ID:
        return

    text = message.text.strip()
    chat_id = str(message.chat_id)
    message_id = message.message_id

    new_has_trigger = text.lower().startswith(TRIGGER_WORD.lower())
    original_text = _message_texts.get((chat_id, message_id), "")
    original_had_trigger = original_text.lower().startswith(TRIGGER_WORD.lower())
    already_responded = message_id in _responded_ids.get(chat_id, set())

    _message_texts[(chat_id, message_id)] = text

    if new_has_trigger and not original_had_trigger:
        logger.info("Trigger added via edit on message %d — treating as new.", message_id)
        if chat_id not in _responded_ids:
            _responded_ids[chat_id] = set()
        _responded_ids[chat_id].add(message_id)
        await message.reply_text("🔍 On it, give me a moment…")
        await process_message(message, context, text, is_edit=True)
        return

    if new_has_trigger and original_had_trigger and already_responded:
        new_body = strip_trigger(text).strip()
        old_body = strip_trigger(original_text).strip()
        if " ".join(new_body.lower().split()) == " ".join(old_body.lower().split()):
            logger.info("Trivial edit on message %d — ignoring.", message_id)
            return
        logger.info("Substantive edit on message %d: '%s' → '%s'", message_id, old_body, new_body)
        await message.reply_text("📝 _(Message edited — responding to updated question)_", parse_mode="Markdown")
        await process_message(message, context, text, is_edit=True)
        return

    if new_has_trigger and not already_responded:
        if chat_id not in _responded_ids:
            _responded_ids[chat_id] = set()
        _responded_ids[chat_id].add(message_id)
        await process_message(message, context, text, is_edit=True)
        return

    logger.info("Edit on message %d ignored (no actionable change).", message_id)


async def _daily_email_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ALLOWED_CHAT_ID:
        logger.warning("ALLOWED_CHAT_ID not set — skipping daily email scan")
        return
    chat_id = ALLOWED_CHAT_ID
    trips = get_trips(chat_id)
    if not trips:
        return
    for trip_name in trips:
        try:
            candidates = scan_for_bookings(chat_id, trip_name)
        except Exception as e:
            logger.error("Daily email scan failed for %s: %s", trip_name, e)
            continue
        if not candidates:
            logger.info("Daily scan: no new bookings for %s", trip_name)
            continue
        set_pending(chat_id, trip_name, candidates)
        header = f"📬 *Daily booking scan — {len(candidates)} new item(s) found for {trip_name}!*\n\n"
        text = header + format_pending_for_telegram(chat_id, trip_name)
        for chunk in chunk_message(text):
            try:
                await context.bot.send_message(
                    chat_id=int(chat_id), text=chunk, parse_mode=ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.error("Failed to send daily scan results: %s", e)


def _setup_handlers(app: Application) -> None:
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.TEXT, handle_edited_message))
    app.job_queue.run_daily(_daily_email_scan, time=datetime.time(hour=8, minute=0))
    logger.info("Daily email scan scheduled at 08:00")

# ─── Bot manager ───────────────────────────────────────────────────────────────

class BotManager:
    """Runs the Telegram bot in a background thread; supports start/stop from the tray."""

    def __init__(self, token: str) -> None:
        self._token = token
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._lock = threading.Lock()
        self._status = "stopped"

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    def start(self) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._status = "starting"
            self._thread = threading.Thread(target=self._run, name="bot", daemon=True)
            self._thread.start()
        return True

    def stop(self) -> None:
        with self._lock:
            if not (self._thread and self._thread.is_alive()):
                self._status = "stopped"
                return
            self._status = "stopping"
            loop, ev = self._loop, self._stop_event
        if loop and ev:
            loop.call_soon_threadsafe(ev.set)
        if self._thread:
            self._thread.join(timeout=15)
        with self._lock:
            if self._status == "stopping":
                self._status = "stopped"

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        ev = asyncio.Event()
        with self._lock:
            self._loop = loop
            self._stop_event = ev
        try:
            loop.run_until_complete(self._async_main(ev))
        except Exception:
            logger.exception("Bot thread crashed")
            with self._lock:
                self._status = "error"
        finally:
            loop.close()
            with self._lock:
                self._loop = None
                self._stop_event = None
                if self._status == "stopping":
                    self._status = "stopped"

    async def _async_main(self, stop_event: asyncio.Event) -> None:
        app = Application.builder().token(self._token).build()
        _setup_handlers(app)
        async with app:
            await app.start()
            await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            with self._lock:
                self._status = "running"
            logger.info("Telegram vacation bot is running")
            await stop_event.wait()
            await app.updater.stop()
            await app.stop()
        logger.info("Telegram bot stopped")
        with self._lock:
            if self._status == "stopping":
                self._status = "stopped"

# ─── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set in .env")

    bot = BotManager(token)
    bot.start()

    try:
        from tray.tray import VacationBotTray
        logger.info("Starting tray app")
        VacationBotTray(bot).run()
    except ImportError:
        logger.info("rumps not available — running without tray (headless mode)")
        assert bot._thread is not None
        bot._thread.join()


if __name__ == "__main__":
    main()
