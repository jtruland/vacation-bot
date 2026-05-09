"""
Pending booking state — bookings found by the email scanner that are
awaiting user confirmation before being saved permanently.

Stored on disk so they survive a bot restart during the confirm window.
File: data/{chat_id}_{trip_name}_pending.json
"""

import json
import os
from datetime import datetime

from shared.paths import chat_dir


def _path(chat_id: str, trip_name: str) -> str:
    return os.path.join(chat_dir(chat_id), f"{trip_name}_pending.json")


def _load(chat_id: str, trip_name: str) -> list[dict]:
    p = _path(chat_id, trip_name)
    if not os.path.exists(p):
        return []
    try:
        with open(p) as f:
            return json.load(f).get("pending", [])
    except Exception:
        return []


def _save(chat_id: str, trip_name: str, items: list[dict]) -> None:
    with open(_path(chat_id, trip_name), "w") as f:
        json.dump({"pending": items, "updated_at": datetime.now().isoformat()}, f, indent=2)


# ─── Public API ────────────────────────────────────────────────────────────────

def set_pending(chat_id: str, trip_name: str, bookings: list[dict]) -> None:
    """Replace the pending list for this trip (overwrites any previous pending scan)."""
    _save(chat_id, trip_name, bookings)


def get_pending(chat_id: str, trip_name: str) -> list[dict]:
    """Return current pending bookings (1-indexed by position in list)."""
    return _load(chat_id, trip_name)


def clear_pending(chat_id: str, trip_name: str) -> None:
    """Discard all pending bookings (after save or skip)."""
    p = _path(chat_id, trip_name)
    if os.path.exists(p):
        os.remove(p)


def has_pending(chat_id: str, trip_name: str) -> bool:
    return len(_load(chat_id, trip_name)) > 0


def pick_pending(chat_id: str, trip_name: str, indices: list[int]) -> list[dict]:
    """
    Return the pending bookings at the given 1-based indices.
    Out-of-range indices are silently ignored.
    """
    items = _load(chat_id, trip_name)
    selected = []
    for i in indices:
        if 1 <= i <= len(items):
            selected.append(items[i - 1])
    return selected


# ─── Formatting ────────────────────────────────────────────────────────────────

_ICONS = {"flight": "✈️", "hotel": "🏨", "rental": "🚗", "activity": "🎭"}


def format_pending_for_telegram(
    chat_id: str,
    trip_name: str,
    owner_context: tuple[str, str] | None = None,
) -> str:
    """
    Numbered list of pending bookings for display in Telegram.
    When owner_context=(group_chat_id, trip_name) is provided, the save/skip
    commands include the group context so the bot owner can act from their DM.
    """
    items = _load(chat_id, trip_name)
    if not items:
        return "No pending bookings to confirm."

    lines = [f"📬 *Found {len(items)} booking(s) in your email for {trip_name}:*\n"]
    for i, b in enumerate(items, 1):
        icon  = _ICONS.get(b.get("type"), "📌")
        title = b.get("title", "Unnamed")
        lines.append(f"*{i}.* {icon} {title}")

        start = b.get("start_date", "")
        end   = b.get("end_date", "")
        if start and end and end != start:
            lines.append(f"   📅 {start} → {end}")
        elif start:
            lines.append(f"   📅 {start}")

        if b.get("confirmation"):
            lines.append(f"   🔖 `{b['confirmation']}`")

        cost = b.get("cost")
        if cost is not None:
            lines.append(f"   💰 {b.get('currency', 'USD')} {float(cost):,.2f}")

        src = b.get("_email_subject", "")
        if src:
            lines.append(f"   _From: {src}_")

        lines.append("")

    if owner_context:
        oc_chat_id, oc_trip = owner_context
        lines.append(
            "Reply with:\n"
            f"  `!claude book save {oc_chat_id} #{oc_trip} all` — save all\n"
            f"  `!claude book save {oc_chat_id} #{oc_trip} 1 3` — save specific items\n"
            f"  `!claude book skip {oc_chat_id} #{oc_trip}` — discard without saving"
        )
    else:
        lines.append(
            "Reply with:\n"
            "  `!claude book save all` — save all\n"
            "  `!claude book save 1 3` — save specific items\n"
            "  `!claude book skip` — discard without saving"
        )
    return "\n".join(lines)
