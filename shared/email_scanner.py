"""
Email scanner for vacation booking confirmations.

Uses Gmail IMAP (app password) to search for booking-related emails,
then passes each through Claude Haiku to extract structured booking data.

Environment variables required:
    GMAIL_ADDRESS      – e.g. jtruland@gmail.com
    GMAIL_APP_PASSWORD – 16-char app password from myaccount.google.com/apppasswords

Add to .env:
    GMAIL_ADDRESS=your@gmail.com
    GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
"""

import imaplib
import email as email_lib
import json
import os
import re
from datetime import datetime, timedelta
from email.header import decode_header as _decode_header

import logging
import anthropic
from dotenv import load_dotenv
from shared.paths import chat_dir

logger = logging.getLogger(__name__)

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
IMAP_HOST   = "imap.gmail.com"
IMAP_PORT   = 993
SCAN_WINDOW = int(os.getenv("EMAIL_SCAN_DAYS", "90"))   # days back to search

# Gmail search terms — broad enough to catch airlines, hotels, OTAs, rental cos
GMAIL_QUERY = (
    '(subject:(booking confirmation) OR subject:(reservation confirmation) '
    'OR subject:(your booking) OR subject:(your reservation) '
    'OR subject:itinerary OR subject:e-ticket OR subject:eticket '
    'OR subject:(your flight) OR subject:(flight confirmation) '
    'OR subject:(hotel confirmation) OR subject:(check-in information) '
    'OR subject:(rental confirmation) OR subject:(car rental) '
    'OR subject:(tour confirmation) OR subject:(activity confirmation) '
    'OR subject:(restaurant reservation))'
)

# ─── Seen-IDs store ────────────────────────────────────────────────────────────

def _seen_path(chat_id: str) -> str:
    return os.path.join(chat_dir(chat_id), "scanned_email_ids.json")


def _load_seen(chat_id: str) -> set[str]:
    p = _seen_path(chat_id)
    if not os.path.exists(p):
        return set()
    try:
        with open(p) as f:
            return set(json.load(f).get("ids", []))
    except Exception:
        return set()


def _save_seen(chat_id: str, seen: set[str]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(_seen_path(chat_id), "w") as f:
        json.dump({"ids": list(seen)}, f, indent=2)


def mark_seen(chat_id: str, msg_ids: list[str]) -> None:
    """Record that these email message IDs have been processed for this chat."""
    seen = _load_seen(chat_id)
    seen.update(msg_ids)
    _save_seen(chat_id, seen)


# ─── IMAP helpers ──────────────────────────────────────────────────────────────

def _decode_str(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:
            return str(value)
    return str(value)


def _decode_header_field(raw: str | None) -> str:
    if not raw:
        return ""
    parts = []
    for fragment, charset in _decode_header(raw):
        if isinstance(fragment, bytes):
            try:
                parts.append(fragment.decode(charset or "utf-8", errors="replace"))
            except Exception:
                parts.append(fragment.decode("latin-1", errors="replace"))
        else:
            parts.append(fragment)
    return " ".join(parts)


def _get_body(msg) -> str:
    """Extract plain-text body from a Message object (prefers text/plain)."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body = payload.decode(charset, errors="replace")
                    break
        # Fallback to html if no plain text found
        if not body:
            for part in msg.walk():
                ct = part.get_content_type()
                cd = str(part.get("Content-Disposition", ""))
                if ct == "text/html" and "attachment" not in cd:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        raw_html = payload.decode(charset, errors="replace")
                        # Strip tags for Claude
                        body = re.sub(r'<[^>]+>', ' ', raw_html)
                        body = re.sub(r'\s+', ' ', body).strip()
                        break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="replace")
    return body[:8000]  # cap at 8k chars — plenty for Claude


def _connect_gmail() -> imaplib.IMAP4_SSL:
    address  = os.getenv("GMAIL_ADDRESS", "")
    password = os.getenv("GMAIL_APP_PASSWORD", "")
    if not address or not password:
        raise RuntimeError(
            "GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set in .env\n"
            "Get an app password at: myaccount.google.com/apppasswords"
        )
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mail.login(address, password)
    return mail


def fetch_booking_emails(chat_id: str, since_days: int = SCAN_WINDOW) -> list[dict]:
    """
    Connect to Gmail, search for booking-related emails from the last `since_days` days,
    skip already-seen message IDs, and return a list of raw email dicts.

    Each dict: {msg_id, subject, sender, date, body}
    """
    seen = _load_seen(chat_id)
    since_date = (datetime.now() - timedelta(days=since_days)).strftime("%Y/%m/%d")

    mail = _connect_gmail()
    try:
        mail.select("inbox")

        # Gmail IMAP supports X-GM-RAW for full Gmail search syntax
        query = f'X-GM-RAW "{GMAIL_QUERY} after:{since_date}"'
        status, data = mail.uid("search", None, query)
        if status != "OK" or not data[0]:
            return []

        uids = data[0].split()
        results = []

        for uid in uids:
            uid_str = uid.decode()
            if uid_str in seen:
                continue

            status, msg_data = mail.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue

            raw = msg_data[0][1]
            if not isinstance(raw, bytes):
                continue

            msg = email_lib.message_from_bytes(raw)
            subject = _decode_header_field(msg.get("Subject", ""))
            sender  = _decode_header_field(msg.get("From", ""))
            date    = msg.get("Date", "")
            body    = _get_body(msg)

            if not body.strip():
                continue

            results.append({
                "msg_id":  uid_str,
                "subject": subject,
                "sender":  sender,
                "date":    date,
                "body":    body,
            })

        return results

    finally:
        try:
            mail.logout()
        except Exception:
            pass


# ─── Claude extraction ─────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a travel booking data extractor.

Given an email, determine if it is a booking confirmation for travel (flight, hotel, car rental, restaurant, tour, activity, or similar).

If it IS a booking confirmation, respond with ONLY valid JSON in this exact shape:
{
  "is_booking": true,
  "type": "flight" | "hotel" | "rental" | "activity",
  "title": "Short descriptive title, e.g. Delta JFK→FCO or Hotel de Russie Rome",
  "start_date": "YYYY-MM-DD or null",
  "end_date": "YYYY-MM-DD or null",
  "confirmation": "booking/record locator code or null",
  "cost": number or null,
  "currency": "USD",
  "notes": "Any relevant notes: seat numbers, meal prefs, cancellation policy, etc. or null",
  "booked_by": "name of guest/passenger if visible, or null",
  "details": {
    // For flights:
    "airline": "...", "flight_number": "...",
    "departure_airport": "IATA", "arrival_airport": "IATA",
    "departure_time": "HH:MM", "arrival_time": "HH:MM",
    "cabin_class": "economy|business|first", "passengers": N,
    // For hotels:
    "property_name": "...", "address": "...", "rooms": N, "guests": N,
    // For rentals:
    "company": "...", "vehicle": "...",
    "pickup_location": "...", "dropoff_location": "...",
    // For activities:
    "venue": "...", "time": "HH:MM", "participants": N
  }
}

If it is NOT a booking confirmation (e.g. marketing email, itinerary suggestion, price alert), respond with ONLY:
{"is_booking": false}

Never include explanation text outside the JSON.
"""


def extract_booking(email: dict) -> dict | None:
    """
    Pass one email through Claude Haiku and return a parsed booking dict,
    or None if the email is not a booking confirmation.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key)

    email_text = (
        f"From: {email['sender']}\n"
        f"Date: {email['date']}\n"
        f"Subject: {email['subject']}\n\n"
        f"{email['body']}"
    )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": email_text}],
    )

    raw = response.content[0].text.strip()

    # Strip markdown code fences if present
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if not data.get("is_booking"):
        return None

    # Attach the source email ID for deduplication
    data["_email_msg_id"] = email["msg_id"]
    data["_email_subject"] = email["subject"]
    data["_email_sender"]  = email["sender"]
    data["_email_date"]    = email["date"]
    return data


# ─── Main scan function ────────────────────────────────────────────────────────

def scan_for_bookings(chat_id: str, trip_name: str, since_days: int = SCAN_WINDOW) -> list[dict]:
    """
    Full scan pipeline: fetch emails → extract bookings → return unseen candidates.

    Returns a list of booking dicts (not yet saved to disk).
    The caller is responsible for confirming and saving via bookings.add_booking().
    """
    emails = fetch_booking_emails(chat_id, since_days=since_days)
    candidates = []
    for em in emails:
        try:
            booking = extract_booking(em)
            if booking:
                candidates.append(booking)
        except Exception as e:
            # Log and continue — one bad email shouldn't abort the whole scan
            logger.error("Error processing email '%s': %s", em.get('subject'), e)

    return candidates
