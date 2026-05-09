import asyncio
import datetime
import logging
import os
import re
import sys
import threading
import uuid

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, ChatMemberHandler, ContextTypes,
    MessageHandler, filters,
)
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from shared.claude_client import (
    ask_claude, clear_history, rehome_history, save_summarize_now, strip_trigger,
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
from shared.bookings import add_booking as _add_booking, get_bookings as _get_bookings, remove_booking as _remove_booking, update_booking as _update_booking
import shared.admin_config as admin_config
from shared.admin_config import set_group_name, get_group_name, resolve_group_by_name
from shared.dm_router import (
    AmbiguousTripError,
    generate_dm_code, disable_dm_code,
    link_dm_to_group, unlink_dm_from_group, get_linked_groups,
    get_group_for_code, resolve_dm_trip,
    log_dm_activity, pop_dm_activity,
)

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

TRIGGER_WORD = os.getenv("TRIGGER_WORD", "!claude")
BOT_OWNER_ID = os.getenv("BOT_OWNER_ID", "")

# Tracks original text for every message we've seen, keyed by (chat_id, message_id)
_message_texts: dict[tuple[str, int], str] = {}
_MAX_TRACKED_MESSAGES = 2000

# Tracks which message IDs we've already responded to, keyed by chat_id
_responded_ids: dict[str, set[int]] = {}
_MAX_RESPONDED_PER_CHAT = 500


def _evict_message_texts() -> None:
    if len(_message_texts) > _MAX_TRACKED_MESSAGES:
        excess = len(_message_texts) - _MAX_TRACKED_MESSAGES
        for key in list(_message_texts)[:excess]:
            _message_texts.pop(key, None)


def _evict_responded_ids(chat_id: str) -> None:
    ids = _responded_ids.get(chat_id)
    if ids and len(ids) > _MAX_RESPONDED_PER_CHAT:
        to_remove = sorted(ids)[:len(ids) - _MAX_RESPONDED_PER_CHAT]
        ids.difference_update(to_remove)

# ─── Pending image store ───────────────────────────────────────────────────────

_PENDING_IMAGES: dict[str, list[str]] = {}
_MAX_PENDING_IMAGES = 100


def _store_images(urls: list[str]) -> str:
    key = uuid.uuid4().hex[:12]
    _PENDING_IMAGES[key] = urls
    if len(_PENDING_IMAGES) > _MAX_PENDING_IMAGES:
        oldest = next(iter(_PENDING_IMAGES))
        _PENDING_IMAGES.pop(oldest, None)
    return key


async def send_images(chat_id_int: int, context: ContextTypes.DEFAULT_TYPE, urls: list[str]) -> None:
    valid = [u for u in urls if u][:10]
    if not valid:
        return
    try:
        if len(valid) == 1:
            await context.bot.send_photo(chat_id=chat_id_int, photo=valid[0])
        else:
            media = [InputMediaPhoto(media=url) for url in valid]
            await context.bot.send_media_group(chat_id=chat_id_int, media=media)
    except Exception as e:
        logger.warning("Failed to send images: %s", e)


async def offer_images(message, images: list[str]) -> None:
    if not images:
        return
    key = _store_images(images)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📷 Show photos", callback_data=f"photos:{key}")
    ]])
    await message.reply_text("Photos available — tap to view.", reply_markup=keyboard)


async def handle_photo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not query.data or not query.data.startswith("photos:"):
        return
    key = query.data[len("photos:"):]
    images = _PENDING_IMAGES.pop(key, [])
    if images:
        await send_images(query.message.chat_id, context, images)
        await query.edit_message_text("📷 Photos sent.")
    else:
        await query.edit_message_text("⚠️ Photos no longer available (session expired).")


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

async def process_message(message, context: ContextTypes.DEFAULT_TYPE, text: str, is_edit: bool = False, is_dm: bool = False) -> None:
    chat_id = str(message.chat_id)
    sender_id = str(message.from_user.id) if message.from_user else ""
    body = strip_trigger(text)
    lower = body.lower()

    # ── DM handling ─────────────────────────────────────────────────────────────
    if is_dm:
        return await _handle_dm(message, context, body, lower, chat_id, sender_id)

    # ── Persist group name for owner DM commands ────────────────────────────────
    if message.chat.title:
        set_group_name(chat_id, message.chat.title)

    # ── DM activity summary (group only) ────────────────────────────────────────
    pending_dm = pop_dm_activity(chat_id)
    if pending_dm:
        summary = "📱 *Recent DM activity:*\n" + "\n".join(f"  • {e}" for e in pending_dm)
        try:
            await message.reply_text(summary, parse_mode="Markdown")
        except Exception:
            pass

    await _process_group_message(message, context, body, lower, chat_id, forced_trip=None)


async def _handle_dm(message, context, body: str, lower: str, dm_chat_id: str, sender_id: str) -> None:
    """Dispatch DM-specific commands and route trip queries to the linked group."""
    is_owner = BOT_OWNER_ID and sender_id == BOT_OWNER_ID

    # ── Owner admin commands ─────────────────────────────────────────────────────
    if is_owner and lower.startswith("admin "):
        args = body[len("admin "):].strip()
        args_lower = args.lower()

        if args_lower == "list":
            allowed = admin_config.get_allowed()
            pending = admin_config.get_pending()
            lines = ["*Allowed chats:*"]
            for cid in allowed:
                lines.append(f"  `{cid}`")
            if pending:
                lines.append("\n*Pending approval:*")
                for cid, info in pending.items():
                    lines.append(f"  `{cid}` — {info.get('name', '?')}")
            await message.reply_text("\n".join(lines) or "No chats configured.", parse_mode="Markdown")
            return

        if args_lower.startswith("allow "):
            cid = args[len("allow "):].strip()
            admin_config.allow_chat(cid)
            await message.reply_text(f"✅ `{cid}` is now allowed.", parse_mode="Markdown")
            try:
                await context.bot.send_message(
                    chat_id=int(cid),
                    text="👋 This group has been approved. Type `!claude help` to get started.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception as e:
                logger.warning("Could not send welcome to %s: %s", cid, e)
            return

        if args_lower.startswith("deny "):
            cid = args[len("deny "):].strip()
            admin_config.deny_chat(cid)
            await message.reply_text(f"🚫 `{cid}` denied and removed from pending.", parse_mode="Markdown")
            return

        if args_lower.startswith("revoke "):
            cid = args[len("revoke "):].strip()
            admin_config.revoke_chat(cid)
            try:
                await context.bot.leave_chat(int(cid))
            except Exception:
                pass
            await message.reply_text(f"🗑️ `{cid}` revoked. Bot left the group.", parse_mode="Markdown")
            return

        if args_lower.startswith("rehome "):
            parts = args[len("rehome "):].strip().split()
            if len(parts) == 2:
                old_cid, new_cid = parts
                admin_config.rehome_chat(old_cid, new_cid)
                rehome_history(old_cid, new_cid)
                await message.reply_text(f"✅ Rehomed `{old_cid}` → `{new_cid}`.", parse_mode="Markdown")
            else:
                await message.reply_text("Usage: `!claude admin rehome <old_id> <new_id>`", parse_mode="Markdown")
            return

        if args_lower == "usage":
            from shared.api_usage import format_usage_report
            report = format_usage_report()
            await message.reply_text(report, parse_mode="Markdown")
            return

        await message.reply_text(
            "Admin commands:\n"
            "`!claude admin list`\n"
            "`!claude admin allow <chat_id>`\n"
            "`!claude admin deny <chat_id>`\n"
            "`!claude admin revoke <chat_id>`\n"
            "`!claude admin rehome <old_id> <new_id>`\n"
            "`!claude admin usage`",
            parse_mode="Markdown"
        )
        return

    # ── Owner book save/skip from DM (acts on a specific group's pending) ────────
    if is_owner and (lower.startswith("book save") or lower.startswith("book skip")):
        cmd = "book save" if lower.startswith("book save") else "book skip"
        args = body[len(cmd):].strip()
        if "#" in args:
            group_ref, rest = args.split("#", 1)
            group_ref = group_ref.strip()
            # Resolve: numeric ID (backward compat) or human name
            if group_ref.lstrip("-").isdigit():
                target_chat_id = group_ref
            else:
                target_chat_id = resolve_group_by_name(group_ref)
                if not target_chat_id:
                    await message.reply_text(f'❌ No group named "{group_ref}" found.', parse_mode="Markdown")
                    return
            trip_selector, remaining = parse_trip_selector(rest.strip())
            if trip_selector:
                reconstructed = f"{cmd} {remaining}".strip() if remaining else cmd
                await _process_group_message(
                    message, context, reconstructed, reconstructed.lower(),
                    target_chat_id, trip_selector, is_dm=True
                )
                return
        await message.reply_text(
            f"Usage: `!claude {cmd} <group name> #<trip> [all|1 3]`",
            parse_mode="Markdown"
        )
        return

    # ── DM join/link commands ────────────────────────────────────────────────────
    if lower.startswith("join "):
        code = body[len("join "):].strip()
        group_id = get_group_for_code(code)
        if not group_id:
            await message.reply_text("❌ Invalid or expired join code.")
            return
        link_dm_to_group(dm_chat_id, group_id)
        try:
            chat = await context.bot.get_chat(int(group_id))
            group_name = chat.title or group_id
        except Exception:
            group_name = group_id
        await message.reply_text(
            f"✅ Linked to *{group_name}*. Use `!claude #tripname <question>` to work on a trip.",
            parse_mode="Markdown"
        )
        return

    if lower == "dm linked":
        linked = get_linked_groups(dm_chat_id)
        if not linked:
            await message.reply_text("No groups linked. Use `!claude join <code>` to link one.", parse_mode="Markdown")
            return
        lines = ["*Linked groups:*"]
        for gid in linked:
            trips = get_trips(gid)
            trip_list = ", ".join(f"`{t}`" for t in trips) if trips else "_no trips_"
            lines.append(f"  `{gid}` — trips: {trip_list}")
        await message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    if lower.startswith("dm unlink"):
        linked = get_linked_groups(dm_chat_id)
        if not linked:
            await message.reply_text("No groups linked.")
            return
        if len(linked) == 1:
            unlink_dm_from_group(dm_chat_id, linked[0])
            await message.reply_text(f"✅ Unlinked from `{linked[0]}`.", parse_mode="Markdown")
        else:
            args = body[len("dm unlink"):].strip()
            if not args:
                lines = ["Multiple groups linked. Specify which to unlink:"]
                for gid in linked:
                    lines.append(f"  `!claude dm unlink {gid}`")
                await message.reply_text("\n".join(lines), parse_mode="Markdown")
            else:
                if unlink_dm_from_group(dm_chat_id, args):
                    await message.reply_text(f"✅ Unlinked from `{args}`.", parse_mode="Markdown")
                else:
                    await message.reply_text(f"❌ Not linked to `{args}`.", parse_mode="Markdown")
        return

    # ── Route to linked group trip ───────────────────────────────────────────────
    trip_selector, question = parse_trip_selector(body)
    try:
        resolved = resolve_dm_trip(dm_chat_id, trip_selector)
    except AmbiguousTripError as e:
        lines = [f"❌ Trip `{e.trip_name}` exists in multiple linked groups. Specify the group:"]
        for gid in e.group_ids:
            lines.append(f"  — `{gid}`")
        await message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    if resolved is None:
        linked = get_linked_groups(dm_chat_id)
        if not linked:
            await message.reply_text(
                "This DM isn't linked to any group yet.\n"
                "Ask a group admin to run `!claude dm enable`, then use `!claude join <code>` here.",
                parse_mode="Markdown"
            )
        elif len(linked) > 1 and not trip_selector:
            lines = ["Linked to multiple groups — specify a trip with `#tripname`:"]
            for gid in linked:
                trips = get_trips(gid)
                for t in trips:
                    lines.append(f"  `#{t}` — from `{gid}`")
            await message.reply_text("\n".join(lines), parse_mode="Markdown")
        else:
            await message.reply_text("❌ Trip not found in any linked group.")
        return

    group_chat_id, trip_name = resolved

    if lower == "help" or body == "":
        await message.reply_text(HELP_TEXT, parse_mode="Markdown")
        return

    # Delegate to the main process_message logic using the group's chat_id
    await _process_group_message(message, context, body, lower, group_chat_id, trip_name, is_dm=True)


async def _process_group_message(message, context, body: str, lower: str, chat_id: str, forced_trip: str | None, is_dm: bool = False) -> None:
    """Core message dispatch — shared by group messages and DM-routed messages."""
    dm_chat_id = str(message.chat_id) if is_dm else None

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
            if is_dm and forced_trip:
                log_dm_activity(chat_id, result, f"Trip created: {result}")
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
        trip_name = forced_trip or trip_selector or get_default_trip(chat_id)
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
        trip_name = forced_trip or trip_selector or get_default_trip(chat_id)
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
        trip_name = forced_trip or trip_selector or get_default_trip(chat_id)
        if not trip_name:
            await message.reply_text("No default trip set. Use `!claude trips` to see your trips.", parse_mode="Markdown")
            return
        await send_chunked(message, _format_bookings_telegram(chat_id, trip_name))
        return

    # ── Group admin DM enable/disable (only from group, not DM) ─────────────────
    if not is_dm:
        if lower == "dm enable":
            if not await is_admin(message, context):
                await message.reply_text("❌ Only group admins can enable DM access.")
                return
            code = generate_dm_code(chat_id)
            await message.reply_text(
                f"✅ DM access enabled.\n\n"
                f"Share this join code with group members:\n"
                f"`!claude join {code}`\n\n"
                f"They can paste it in a direct message with the bot.",
                parse_mode="Markdown"
            )
            return

        if lower == "dm disable":
            if not await is_admin(message, context):
                await message.reply_text("❌ Only group admins can disable DM access.")
                return
            disable_dm_code(chat_id)
            await message.reply_text("🔒 DM join code disabled. Existing DM links remain active.")
            return

    if lower == "scan email" or lower.startswith("scan email "):
        remainder = body[len("scan email"):].strip()
        trip_selector, _ = parse_trip_selector(remainder)
        trip_name = forced_trip or trip_selector or get_default_trip(chat_id)
        if not trip_name:
            await message.reply_text("No default trip set.", parse_mode="Markdown")
            return
        if trip_name not in get_trips(chat_id):
            await message.reply_text(f'❌ No trip called "{trip_name}" found.', parse_mode="Markdown")
            return
        try:
            candidates = scan_for_bookings(chat_id, trip_name)
        except RuntimeError as e:
            logger.error("Email scan failed for %s/%s: %s", chat_id, trip_name, e)
            return
        except Exception as e:
            logger.error("Unexpected error during email scan for %s/%s: %s", chat_id, trip_name, e)
            return
        if not candidates:
            logger.info("Email scan: no new bookings for %s/%s", chat_id, trip_name)
            return
        set_pending(chat_id, trip_name, candidates)
        if BOT_OWNER_ID:
            gname = get_group_name(chat_id)
            text = format_pending_for_telegram(
                chat_id, trip_name,
                owner_context=(chat_id, trip_name),
                group_name=gname,
            )
            for chunk in chunk_message(text):
                try:
                    await context.bot.send_message(
                        chat_id=int(BOT_OWNER_ID), text=chunk, parse_mode=ParseMode.MARKDOWN
                    )
                except Exception as e:
                    logger.error("Failed to send scan results to owner DM: %s", e)
        else:
            await send_chunked(message, format_pending_for_telegram(chat_id, trip_name))
        return

    if lower.startswith("book save"):
        args_str = body[len("book save"):].strip()
        trip_selector, args_str = parse_trip_selector(args_str)
        trip_name = forced_trip or trip_selector or get_default_trip(chat_id)
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
        all_email_ids = [b["_email_msg_id"] for b in pending if b.get("_email_msg_id")]
        saved = []
        for b in to_save:
            clean = {k: v for k, v in b.items() if not k.startswith("_")}
            saved.append((_add_booking(chat_id, trip_name, clean), b.get("title", "Unnamed"), b.get("type", "")))
        if all_email_ids:
            mark_seen(chat_id, all_email_ids)
        clear_pending(chat_id, trip_name)
        icons = {"flight": "✈️", "hotel": "🏨", "rental": "🚗", "activity": "🎭"}
        lines = [f"✅ *{len(saved)} booking(s) saved to {trip_name}:*\n"]
        for bid, title, btype in saved:
            lines.append(f"  {icons.get(btype, '📌')} {title}  `{bid}`")
            if is_dm:
                log_dm_activity(chat_id, trip_name, f"Booking saved: {title}")
        lines.append("\nUse `!claude booked` to see all confirmed bookings.")
        # Flag any saved bookings that are missing dates
        incomplete_warnings = []
        for bid, title, btype in saved:
            saved_b = next((b for b in _get_bookings(chat_id, trip_name) if b["id"] == bid), None)
            if saved_b:
                missing = []
                if not saved_b.get("start_date"):
                    missing.append("start_date")
                if btype == "hotel" and not saved_b.get("end_date"):
                    missing.append("end_date")
                if missing:
                    miss_str = " + ".join(missing)
                    incomplete_warnings.append(
                        f"  {icons.get(btype, '📌')} {title} `{bid}` — missing {miss_str}\n"
                        f"  → `!claude book edit {bid} {' '.join(f + '=YYYY-MM-DD' for f in missing)}`"
                    )
        if incomplete_warnings:
            lines.append("\n⚠️ *These bookings need dates:*")
            lines.extend(incomplete_warnings)
        await send_chunked(message, "\n".join(lines))
        if is_dm:
            group_lines = [f"✅ *{len(saved)} booking(s) added to {trip_name}:*"]
            for _, title, btype in saved:
                group_lines.append(f"  {icons.get(btype, '📌')} {title}")
            try:
                await context.bot.send_message(
                    chat_id=int(chat_id), text="\n".join(group_lines), parse_mode=ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.error("Failed to notify group %s of saved bookings: %s", chat_id, e)
        return

    if lower == "book skip" or lower.startswith("book skip "):
        remainder = body[len("book skip"):].strip()
        trip_selector, _ = parse_trip_selector(remainder)
        trip_name = forced_trip or trip_selector or get_default_trip(chat_id)
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

    if lower.startswith("book remove"):
        remainder = body[len("book remove"):].strip()
        trip_selector, remainder = parse_trip_selector(remainder)
        trip_name = forced_trip or trip_selector or get_default_trip(chat_id)
        booking_id = remainder.strip()
        if not trip_name:
            await message.reply_text("No default trip set.")
            return
        if not booking_id:
            await message.reply_text("❌ Usage: `!claude book remove <id>`", parse_mode="Markdown")
            return
        if _remove_booking(chat_id, trip_name, booking_id):
            await message.reply_text(f"🗑️ Booking `{booking_id}` removed from *{trip_name}*.", parse_mode="Markdown")
        else:
            await message.reply_text(f"❌ No booking `{booking_id}` found in *{trip_name}*. Use `!claude booked` to see IDs.", parse_mode="Markdown")
        return

    _EDITABLE = {"title", "start_date", "end_date", "confirmation", "cost", "notes"}
    if lower.startswith("book edit"):
        remainder = body[len("book edit"):].strip()
        trip_selector, remainder = parse_trip_selector(remainder)
        trip_name = forced_trip or trip_selector or get_default_trip(chat_id)
        if not trip_name:
            await message.reply_text("No default trip set.")
            return
        parts = remainder.split(None, 1)
        if len(parts) < 2:
            await message.reply_text(
                "❌ Usage: `!claude book edit <id> field=value ...`\n"
                "Editable fields: `title start_date end_date confirmation cost notes`",
                parse_mode="Markdown"
            )
            return
        booking_id, fields_str = parts[0], parts[1]
        pat = "|".join(sorted(_EDITABLE, key=len, reverse=True))
        updates: dict = {}
        error = None
        for m in re.finditer(rf"({pat})=(.+?)(?=\s+(?:{pat})=|$)", fields_str, re.IGNORECASE):
            key, val = m.group(1).lower(), m.group(2).strip()
            if key == "cost":
                try:
                    val = float(val)
                except ValueError:
                    error = "❌ `cost` must be a number."
                    break
            updates[key] = val
        if error:
            await message.reply_text(error, parse_mode="Markdown")
            return
        if not updates:
            await message.reply_text(
                "❌ No valid field=value pairs found.\nEditable fields: `title start_date end_date confirmation cost notes`",
                parse_mode="Markdown"
            )
            return
        if _update_booking(chat_id, trip_name, booking_id, updates):
            changed = " ".join(f"`{k}`" for k in updates)
            await message.reply_text(f"✅ Booking `{booking_id}` updated: {changed}.", parse_mode="Markdown")
        else:
            await message.reply_text(f"❌ No booking `{booking_id}` found in *{trip_name}*.", parse_mode="Markdown")
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
            text, images = result if isinstance(result, tuple) else (result, [])
            await send_chunked(message, text)
            await offer_images(message, images)
            return

    trip_selector, question = parse_trip_selector(body)
    trip_name = forced_trip or trip_selector or get_default_trip(chat_id)

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
        response, images = ask_claude(question, chat_id, trip_name)
        header = f"_{trip_name}_\n" if trip_selector and not forced_trip else ""
        await send_chunked(message, header + response)
        await offer_images(message, images)
        if is_dm:
            preview = question[:60] + ("…" if len(question) > 60 else "")
            log_dm_activity(chat_id, trip_name, f"Asked: {preview}")
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

    is_dm = message.chat.type == "private"
    if not is_dm and not admin_config.is_allowed(str(message.chat_id)):
        logger.warning("Ignored message from unauthorized chat_id: %s", message.chat_id)
        return

    text = message.text.strip()
    chat_id = str(message.chat_id)
    message_id = message.message_id

    _message_texts[(chat_id, message_id)] = text
    _evict_message_texts()

    is_reply_to_bot = bool(
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == context.bot.id
    )

    if not text.lower().startswith(TRIGGER_WORD.lower()) and not is_reply_to_bot:
        return

    if chat_id not in _responded_ids:
        _responded_ids[chat_id] = set()
    _responded_ids[chat_id].add(message_id)
    _evict_responded_ids(chat_id)

    body = strip_trigger(text).strip()
    lower = body.lower()
    instant_commands = {"help", "trips", "reset", "summarize", "booked", "book skip"}
    is_instant = (
        body == ""
        or lower in instant_commands
        or any(lower.startswith(p) for p in (
            "trip new ", "trip default ", "trip delete ",
            "reset ", "summarize ", "booked ",
            "book save ", "book skip ", "book remove ", "book edit ",
            "flights ", "hotels ", "rentals ",
            "places ", "reviews ", "events ", "explore ",
        ))
    )
    if not is_instant:
        await message.reply_text("🔍 On it, give me a moment…")

    await process_message(message, context, text, is_dm=is_dm)


async def handle_edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.edited_message
    if not message or not message.text:
        return

    is_dm = message.chat.type == "private"
    if not is_dm and not admin_config.is_allowed(str(message.chat_id)):
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
        await process_message(message, context, text, is_edit=True, is_dm=is_dm)
        return

    if new_has_trigger and original_had_trigger and already_responded:
        new_body = strip_trigger(text).strip()
        old_body = strip_trigger(original_text).strip()
        if " ".join(new_body.lower().split()) == " ".join(old_body.lower().split()):
            logger.info("Trivial edit on message %d — ignoring.", message_id)
            return
        logger.info("Substantive edit on message %d: '%s' → '%s'", message_id, old_body, new_body)
        await message.reply_text("📝 _(Message edited — responding to updated question)_", parse_mode="Markdown")
        await process_message(message, context, text, is_edit=True, is_dm=is_dm)
        return

    if new_has_trigger and not already_responded:
        if chat_id not in _responded_ids:
            _responded_ids[chat_id] = set()
        _responded_ids[chat_id].add(message_id)
        await process_message(message, context, text, is_edit=True, is_dm=is_dm)
        return

    logger.info("Edit on message %d ignored (no actionable change).", message_id)


async def _daily_email_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_ids = admin_config.get_allowed()
    if not chat_ids:
        logger.info("Daily email scan: no allowed chats configured")
        return
    for chat_id in chat_ids:
        trips = get_trips(chat_id)
        if not trips:
            continue
        for trip_name in trips:
            try:
                candidates = scan_for_bookings(chat_id, trip_name)
            except Exception as e:
                logger.error("Daily email scan failed for %s/%s: %s", chat_id, trip_name, e)
                continue
            if not candidates:
                logger.info("Daily scan: no new bookings for %s/%s", chat_id, trip_name)
                continue
            set_pending(chat_id, trip_name, candidates)
            header = f"📬 *Daily booking scan — {len(candidates)} new item(s) found for {trip_name}!*\n\n"
            if BOT_OWNER_ID:
                gname = get_group_name(chat_id)
                text = header + format_pending_for_telegram(
                    chat_id, trip_name,
                    owner_context=(chat_id, trip_name),
                    group_name=gname,
                )
                dest = int(BOT_OWNER_ID)
            else:
                text = header + format_pending_for_telegram(chat_id, trip_name)
                dest = int(chat_id)
            for chunk in chunk_message(text):
                try:
                    await context.bot.send_message(
                        chat_id=dest, text=chunk, parse_mode=ParseMode.MARKDOWN
                    )
                except Exception as e:
                    logger.error("Failed to send daily scan results to %s: %s", dest, e)


async def handle_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fires when the bot's membership status changes in any chat."""
    result = update.my_chat_member
    if not result:
        return
    new_status = result.new_chat_member.status
    chat = result.chat

    if chat.type == "private":
        return

    if new_status in ("member", "administrator"):
        chat_id = str(chat.id)
        chat_name = chat.title or chat_id
        if admin_config.is_allowed(chat_id):
            return
        admin_config.add_pending(chat_id, chat_name)
        logger.info("Bot added to new group: %s (%s)", chat_name, chat_id)
        if BOT_OWNER_ID:
            msg = (
                f"🤖 The bot was added to *{chat_name}* (chat ID: `{chat_id}`).\n\n"
                f"`!claude admin allow {chat_id}`\n"
                f"`!claude admin deny {chat_id}`"
            )
            try:
                await context.bot.send_message(
                    chat_id=int(BOT_OWNER_ID), text=msg, parse_mode=ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.error("Failed to notify owner of new group: %s", e)


async def handle_migration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles group→supergroup migration (chat_id changes)."""
    msg = update.message
    if not msg:
        return
    if msg.migrate_to_chat_id:
        old_id = str(msg.chat_id)
        new_id = str(msg.migrate_to_chat_id)
        logger.info("Group migration detected: %s → %s", old_id, new_id)
        admin_config.rehome_chat(old_id, new_id)
        rehome_history(old_id, new_id)


def _setup_handlers(app: Application) -> None:
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.TEXT, handle_edited_message))
    app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, handle_migration))
    app.add_handler(CallbackQueryHandler(handle_photo_callback, pattern=r"^photos:"))
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

    def join(self) -> None:
        if self._thread:
            self._thread.join()

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
    admin_config.bootstrap_from_env()

    bot = BotManager(token)
    bot.start()

    try:
        from tray.tray import VacationBotTray
        logger.info("Starting tray app")
        VacationBotTray(bot).run()
    except ImportError:
        logger.info("rumps not available — running without tray (headless mode)")
        bot.join()


if __name__ == "__main__":
    main()
