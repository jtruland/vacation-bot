import os
import sys
import logging
import requests
from flask import Flask, request, jsonify
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

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")
TRIGGER_WORD = os.getenv("TRIGGER_WORD", "!claude")

# Comma-separated phone numbers allowed to delete trips, e.g. "15551234567,15559876543"
ADMIN_PHONES = set(
    p.strip() for p in os.getenv("ADMIN_PHONES", "").split(",") if p.strip()
)

HELP_TEXT = (
    "🗺️ Vacation Planning Bot — Commands\n\n"
    "Ask questions:\n"
    "  !claude <question> — ask the default trip planner\n"
    "  !claude #tripname <question> — ask about a specific trip\n\n"
    "Flights:\n"
    "  !claude flights JFK Rome 2026-07-15 — one-way\n"
    "  !claude flights JFK Rome 2026-07-15 2026-07-25 2 — round trip, 2 adults\n\n"
    "Hotels:\n"
    "  !claude hotels Rome 2026-07-15 2026-07-22 2 — 2 guests\n\n"
    "Vacation Rentals:\n"
    "  !claude rentals Rome 2026-07-15 2026-07-22 4 — 4 guests\n\n"
    "Places & Attractions:\n"
    "  !claude places best restaurants in Rome\n"
    "  !claude places things to do near the Colosseum\n\n"
    "Reviews:\n"
    "  !claude reviews Colosseum Rome\n\n"
    "Events:\n"
    "  !claude events Rome — upcoming events\n"
    "  !claude events Rome 2026-07-15 — events around a date\n\n"
    "Travel Explore:\n"
    "  !claude explore Rome — top attractions and travel insights\n\n"
    "Trip management:\n"
    "  !claude trips — list all trips and which is default\n"
    "  !claude trip new <name> — create a new trip plan\n"
    "  !claude trip default <name> — set a trip as the default\n"
    "  !claude trip delete <name> — (admin only) permanently delete a trip\n\n"
    "Memory:\n"
    "  !claude reset — clear recent conversation for the default trip\n"
    "  !claude reset #tripname — clear for a specific trip\n"
    "  !claude summarize — save a planning summary now\n"
    "  !claude summarize #tripname — save summary for a specific trip\n\n"
    "  !claude help — show this message\n\n"
    "Trip names: one word, e.g. italy2026 or beach-trip.\n"
    "Dates: YYYY-MM-DD or MM/DD/YYYY. Prices in USD."
)


def send(to: str, body: str):
    """Send a WhatsApp text message."""
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body}
    }
    resp = requests.post(url, json=payload, headers=headers)
    if not resp.ok:
        logger.error(f"Failed to send WhatsApp message: {resp.status_code} {resp.text}")
    return resp


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        logger.info("Webhook verified by Meta.")
        return challenge, 200
    logger.warning("Webhook verification failed — token mismatch.")
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def handle_webhook():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"status": "ok"}), 200

    try:
        entry = data.get("entry", [])[0]
        changes = entry.get("changes", [])[0]
        value = changes.get("value", {})

        if "statuses" in value:
            return jsonify({"status": "ok"}), 200

        for message in value.get("messages", []):
            if message.get("type") != "text":
                continue

            text = message["text"]["body"].strip()
            from_number = message["from"]
            chat_id = PHONE_NUMBER_ID or "whatsapp"

            if not text.lower().startswith(TRIGGER_WORD.lower()):
                continue

            body = strip_trigger(text)
            lower = body.lower()

            # -----------------------------------------------------------
            # !claude help
            # -----------------------------------------------------------
            if lower == "help" or body == "":
                send(from_number, HELP_TEXT)
                continue

            # -----------------------------------------------------------
            # First-time setup: no trips exist yet
            # -----------------------------------------------------------
            if not has_trips(chat_id) and not lower.startswith("trip new"):
                send(from_number,
                     "👋 Welcome! No trip plans exist yet.\n\n"
                     "Start by creating your first trip:\n"
                     "!claude trip new <name>\n\n"
                     "Give it a short name like italy2026, hawaii, or summer-beach. "
                     "It will be set as the default automatically.")
                continue

            # -----------------------------------------------------------
            # !claude trips
            # -----------------------------------------------------------
            if lower == "trips":
                trips = get_trips(chat_id)
                default = get_default_trip(chat_id)
                if not trips:
                    send(from_number, "No trips yet. Create one with: !claude trip new <name>")
                else:
                    lines = [f"  • {t}{'  ← default' if t == default else ''}" for t in trips]
                    send(from_number, "🗺️ Trip plans:\n" + "\n".join(lines))
                continue

            # -----------------------------------------------------------
            # !claude trip new <name>
            # -----------------------------------------------------------
            if lower.startswith("trip new "):
                name = body[len("trip new "):].strip()
                success, result = create_trip(chat_id, name)
                if success:
                    default = get_default_trip(chat_id)
                    is_default = default == result
                    msg = f'✅ Trip "{result}" created!'
                    if is_default:
                        msg += " It's been set as the default."
                    msg += f"\n\nStart planning: !claude <question> or !claude #{result} <question>"
                    send(from_number, msg)
                else:
                    send(from_number, f"❌ {result}")
                continue

            # -----------------------------------------------------------
            # !claude trip default <name>
            # -----------------------------------------------------------
            if lower.startswith("trip default "):
                name = body[len("trip default "):].strip()
                success, result = set_default_trip(chat_id, name)
                if success:
                    send(from_number, f'✅ "{result}" is now the default trip.')
                else:
                    send(from_number, f"❌ {result}")
                continue

            # -----------------------------------------------------------
            # !claude trip delete <name>  (admin only)
            # -----------------------------------------------------------
            if lower.startswith("trip delete "):
                if from_number not in ADMIN_PHONES:
                    send(from_number, "❌ Only admins can delete trips. Ask Jon to add your number to ADMIN_PHONES in the bot config.")
                    continue
                name = body[len("trip delete "):].strip()
                success, result = delete_trip(chat_id, name)
                if success:
                    new_default = get_default_trip(chat_id)
                    msg = f'🗑️ Trip "{result}" and its summary have been permanently deleted.'
                    if new_default:
                        msg += f'\n\nNew default trip: "{new_default}"'
                    else:
                        msg += "\n\nNo trips remaining. Create one with: !claude trip new <name>"
                    send(from_number, msg)
                else:
                    send(from_number, f"❌ {result}")
                continue

            # -----------------------------------------------------------
            # !claude reset [#tripname]
            # -----------------------------------------------------------
            if lower == "reset" or lower.startswith("reset "):
                remainder = body[len("reset"):].strip()
                trip_selector, _ = parse_trip_selector(remainder)
                trip_name = trip_selector or get_default_trip(chat_id)
                if not trip_name:
                    send(from_number, "No default trip set. Use !claude trips to see your trips.")
                    continue
                if trip_name not in get_trips(chat_id):
                    send(from_number, f'❌ No trip called "{trip_name}" found.')
                    continue
                clear_history(chat_id, trip_name)
                send(from_number, f'🗑️ Conversation history cleared for "{trip_name}". Long-term summary is preserved. Starting fresh!')
                continue

            # -----------------------------------------------------------
            # !claude summarize [#tripname]
            # -----------------------------------------------------------
            if lower == "summarize" or lower.startswith("summarize "):
                remainder = body[len("summarize"):].strip()
                trip_selector, _ = parse_trip_selector(remainder)
                trip_name = trip_selector or get_default_trip(chat_id)
                if not trip_name:
                    send(from_number, "No default trip set. Use !claude trips to see your trips.")
                    continue
                saved, used_trip = save_summarize_now(chat_id, trip_name)
                if saved:
                    send(from_number, f'📝 Planning summary saved for "{used_trip}".')
                else:
                    send(from_number, "Nothing to summarize yet — ask me something first!")
                continue

            # -----------------------------------------------------------
            # Search commands — SerpApi engines
            # -----------------------------------------------------------
            search_commands = {
                "flights":  search_flights,
                "hotels":   search_hotels,
                "rentals":  search_rentals,
                "places":   search_places,
                "reviews":  search_reviews,
                "events":   search_events,
                "explore":  search_explore,
            }
            matched = False
            for cmd, fn in search_commands.items():
                if lower == cmd or lower.startswith(f"{cmd} "):
                    args = body[len(cmd):].strip()
                    result = fn(args)
                    send(from_number, result)
                    matched = True
                    break
            if matched:
                continue

            # -----------------------------------------------------------
            # !claude [#tripname] <question>
            # -----------------------------------------------------------
            trip_selector, question = parse_trip_selector(body)
            trip_name = trip_selector or get_default_trip(chat_id)

            if not trip_name:
                send(from_number, "No default trip set. Use !claude trip default <name> or !claude trips to see your options.")
                continue

            if trip_name not in get_trips(chat_id):
                send(from_number, f'❌ No trip called "{trip_name}" found. Use !claude trips to see available trips.')
                continue

            if not question:
                send(from_number, f'Ask me something about "{trip_name}"! Or type !claude help for all commands.')
                continue

            try:
                response = ask_claude(question, chat_id, trip_name)
                header = f"[{trip_name}]\n" if trip_selector else ""
                send(from_number, header + response)
            except RuntimeError as e:
                logger.error(f"Claude API error: {e}")
                send(from_number, str(e))
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                send(from_number, "⚠️ Something unexpected went wrong. Try again in a moment.")

    except (KeyError, IndexError) as e:
        logger.error(f"Error parsing webhook payload: {e}")

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logger.info(f"WhatsApp vacation bot running on port {port}...")
    app.run(host="0.0.0.0", port=port, debug=False)
