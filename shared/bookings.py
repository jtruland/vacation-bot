"""
Persistent booking storage for the vacation planning bot.

One JSON file per trip: data/{chat_id}_{trip_name}_bookings.json

Booking types: flight, hotel, rental, activity
"""

import json
import os
import uuid
from datetime import datetime

from shared.paths import chat_dir

# ─── Type metadata ─────────────────────────────────────────────────────────────

BOOKING_ICONS = {
    "flight":   "✈️",
    "hotel":    "🏨",
    "rental":   "🚗",
    "activity": "🎭",
}

# Display order for sorted lists
BOOKING_ORDER = {"flight": 0, "hotel": 1, "rental": 2, "activity": 3}


# ─── Storage helpers ───────────────────────────────────────────────────────────

def _path(chat_id: str, trip_name: str) -> str:
    return os.path.join(chat_dir(chat_id), f"{trip_name}_bookings.json")


def _load(chat_id: str, trip_name: str) -> list[dict]:
    p = _path(chat_id, trip_name)
    if not os.path.exists(p):
        return []
    try:
        with open(p) as f:
            return json.load(f).get("bookings", [])
    except Exception:
        return []


def _save(chat_id: str, trip_name: str, bookings: list[dict]) -> None:
    with open(_path(chat_id, trip_name), "w") as f:
        json.dump({"bookings": bookings}, f, indent=2)


# ─── Public CRUD ───────────────────────────────────────────────────────────────

def get_bookings(chat_id: str, trip_name: str) -> list[dict]:
    """Return all bookings for this trip."""
    return _load(chat_id, trip_name)


def add_booking(chat_id: str, trip_name: str, booking: dict) -> str:
    """
    Persist a new booking. Adds 'id' and 'booked_at' automatically.
    Returns the generated booking ID.
    """
    bookings = _load(chat_id, trip_name)
    booking_id = f"{booking.get('type', 'b')[0]}{len(bookings) + 1:03d}_{uuid.uuid4().hex[:4]}"
    booking = {**booking, "id": booking_id, "booked_at": datetime.now().isoformat()}
    bookings.append(booking)
    _save(chat_id, trip_name, bookings)
    return booking_id


def update_booking(chat_id: str, trip_name: str, booking_id: str, updates: dict) -> bool:
    """
    Merge updates into an existing booking. Returns True if found and updated.
    """
    bookings = _load(chat_id, trip_name)
    for b in bookings:
        if b["id"] == booking_id:
            # Merge nested 'details' dict rather than replacing it
            if "details" in updates and "details" in b:
                b["details"] = {**b["details"], **updates.pop("details")}
            b.update(updates)
            b["updated_at"] = datetime.now().isoformat()
            _save(chat_id, trip_name, bookings)
            return True
    return False


def remove_booking(chat_id: str, trip_name: str, booking_id: str) -> bool:
    """Remove a booking by ID. Returns True if it existed."""
    bookings = _load(chat_id, trip_name)
    filtered = [b for b in bookings if b["id"] != booking_id]
    if len(filtered) == len(bookings):
        return False
    _save(chat_id, trip_name, filtered)
    return True


# ─── Formatters ────────────────────────────────────────────────────────────────

def _booking_lines_prompt(b: dict) -> list[str]:
    """Render one booking as compact lines for the Claude system prompt."""
    icon = BOOKING_ICONS.get(b.get("type"), "📌")
    lines = [f"{icon} {b.get('title', 'Unnamed')}  [ID: {b['id']}]"]

    start = b.get("start_date", "")
    end   = b.get("end_date", "")
    if start and end and end != start:
        lines.append(f"   {start} → {end}")
    elif start:
        lines.append(f"   {start}")

    if b.get("confirmation"):
        lines.append(f"   Confirmation: {b['confirmation']}")

    cost = b.get("cost")
    if cost is not None:
        lines.append(f"   {b.get('currency', 'USD')} {float(cost):,.2f}")

    details = b.get("details") or {}
    btype = b.get("type")
    if btype == "flight":
        parts = [details.get("airline", ""), details.get("flight_number", "")]
        desc = " ".join(p for p in parts if p)
        if desc:
            lines.append(f"   {desc}")
        if details.get("departure_airport") and details.get("arrival_airport"):
            lines.append(f"   {details['departure_airport']} → {details['arrival_airport']}")
        if details.get("passengers"):
            lines.append(f"   {details['passengers']} passenger(s)")
    elif btype == "hotel":
        if details.get("rooms"):
            lines.append(f"   {details['rooms']} room(s)")
        if details.get("guests"):
            lines.append(f"   {details['guests']} guest(s)")
    elif btype == "rental":
        parts = [details.get("company", ""), details.get("vehicle", "")]
        desc = " · ".join(p for p in parts if p)
        if desc:
            lines.append(f"   {desc}")
        if details.get("pickup_location"):
            lines.append(f"   Pickup: {details['pickup_location']}")
    elif btype == "activity":
        if details.get("venue"):
            lines.append(f"   @ {details['venue']}")
        if details.get("time"):
            lines.append(f"   Time: {details['time']}")
        if details.get("participants"):
            lines.append(f"   {details['participants']} participant(s)")

    if b.get("notes"):
        lines.append(f"   Notes: {b['notes']}")

    return lines


def format_for_prompt(chat_id: str, trip_name: str) -> str:
    """
    Returns a compact bookings block for injection into the Claude system prompt.
    Empty string if no bookings exist.
    """
    bookings = _load(chat_id, trip_name)
    if not bookings:
        return ""

    sorted_b = sorted(bookings, key=lambda x: (
        BOOKING_ORDER.get(x.get("type"), 9),
        x.get("start_date") or ""
    ))

    lines = ["=== CONFIRMED BOOKINGS ==="]
    total = 0.0
    for b in sorted_b:
        lines.append("")
        lines.extend(_booking_lines_prompt(b))
        try:
            total += float(b.get("cost") or 0)
        except (ValueError, TypeError):
            pass

    if total > 0:
        lines.append(f"\nTotal booked: ${total:,.2f}")

    return "\n".join(lines)


def format_for_telegram(chat_id: str, trip_name: str) -> str:
    """
    Returns a Markdown-formatted bookings list for sending to Telegram.
    """
    bookings = _load(chat_id, trip_name)
    if not bookings:
        return f"No confirmed bookings yet for *{trip_name}*."

    sorted_b = sorted(bookings, key=lambda x: (
        BOOKING_ORDER.get(x.get("type"), 9),
        x.get("start_date") or ""
    ))

    lines = [f"📋 *Confirmed Bookings — {trip_name}*\n"]
    total = 0.0

    for b in sorted_b:
        btype  = b.get("type", "other")
        icon   = BOOKING_ICONS.get(btype, "📌")
        title  = b.get("title", "Unnamed")

        lines.append(f"{icon} *{title}*")
        lines.append(f"  `{b['id']}`")

        start = b.get("start_date", "")
        end   = b.get("end_date", "")
        if start and end and end != start:
            lines.append(f"  📅 {start} → {end}")
        elif start:
            lines.append(f"  📅 {start}")

        if b.get("confirmation"):
            lines.append(f"  🔖 `{b['confirmation']}`")

        cost = b.get("cost")
        if cost is not None:
            currency = b.get("currency", "USD")
            lines.append(f"  💰 {currency} {float(cost):,.2f}")
            try:
                total += float(cost)
            except (ValueError, TypeError):
                pass

        details = b.get("details") or {}
        if btype == "flight":
            parts = [details.get("airline", ""), details.get("flight_number", "")]
            desc = " ".join(p for p in parts if p)
            if desc:
                lines.append(f"  ✈️ {desc}")
            if details.get("departure_airport") and details.get("arrival_airport"):
                lines.append(f"  {details['departure_airport']} → {details['arrival_airport']}")
            if details.get("passengers"):
                lines.append(f"  👥 {details['passengers']} pax")
        elif btype == "hotel":
            if details.get("rooms"):
                lines.append(f"  🛏 {details['rooms']} room(s)")
            if details.get("guests"):
                lines.append(f"  👥 {details['guests']} guest(s)")
        elif btype == "rental":
            parts = [details.get("company", ""), details.get("vehicle", "")]
            desc = " · ".join(p for p in parts if p)
            if desc:
                lines.append(f"  🚗 {desc}")
            if details.get("pickup_location"):
                lines.append(f"  📍 Pickup: {details['pickup_location']}")
        elif btype == "activity":
            if details.get("venue"):
                lines.append(f"  📍 {details['venue']}")
            if details.get("time"):
                lines.append(f"  🕐 {details['time']}")
            if details.get("participants"):
                lines.append(f"  👥 {details['participants']} participant(s)")

        if b.get("notes"):
            lines.append(f"  📝 _{b['notes']}_")

        lines.append("")  # spacing between entries

    if total > 0:
        lines.append(f"*Total booked: ${total:,.2f}*")

    return "\n".join(lines)
