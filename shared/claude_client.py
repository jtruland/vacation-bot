import logging
import os
import json
import anthropic
from anthropic import Anthropic
from datetime import datetime
from dotenv import load_dotenv
from shared.paths import chat_dir
from shared.web_fetcher import extract_urls, build_url_context
from shared.serpapi_client import (
    search_flights, search_hotels, search_rentals,
    search_places, search_reviews, search_events, search_explore,
    search_web, search_weather, convert_currency,
    search_gas_nearby, search_gas_along_route,
)
from shared.bookings import (
    add_booking as _add_booking,
    get_bookings as _get_bookings,
    update_booking as _update_booking,
    remove_booking as _remove_booking,
    format_for_prompt as _format_bookings_for_prompt,
)

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

logger = logging.getLogger(__name__)

TRIGGER_WORD = os.getenv("TRIGGER_WORD", "!claude")
MAX_HISTORY = 20          # Max messages in active memory per trip (10 exchanges)
MAX_TOOL_ITERATIONS = 10  # Max tool-call rounds per Claude response

_client: Anthropic | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set in .env")
        _client = Anthropic(api_key=api_key)
    return _client

BASE_SYSTEM_PROMPT = (
    "You are a helpful vacation planning assistant embedded in a family group chat. "
    "Help with destination ideas, itinerary planning, activity recommendations, "
    "packing tips, budgeting, and any other travel-related questions. "
    "Be friendly, concise, and practical. You have memory of the current planning "
    "conversation and a running summary of all previous planning sessions, so you "
    "can build on previous suggestions and refine ideas as the discussion evolves. "
    "If a question is unrelated to travel or vacation planning, gently redirect back "
    "to trip planning topics.\n\n"
    "You have access to real-time search tools for flights, hotels, vacation rentals, "
    "places, reviews, events, travel exploration, web search, weather, currency conversion, "
    "and gas prices along driving routes. "
    "Use them proactively whenever the user's question would benefit from live data — "
    "don't just answer from memory when a search would give a better, more accurate answer. "
    "When you use a search tool, briefly acknowledge what you're looking up before "
    "presenting the results. "
    "If search_places or search_reviews can't find a specific hotel or venue, immediately "
    "try search_web with the property name and city — don't give up after one tool. "
    "If search_web still comes up short, tell the user to paste the hotel's website URL "
    "and you'll pull the info directly from it. "
    "Use search_weather proactively when dates or seasons are part of the conversation. "
    "Use convert_currency for any budget or price question involving foreign currencies. "
    "Use search_gas_nearby when someone asks about gas prices at or near a destination. "
    "Use search_gas_along_route when planning a road trip leg — it returns GasBuddy-priced "
    "stops at roughly equal intervals with Top Tier ratings and composite scores.\n\n"
    "You also have tools to record, list, update, and remove confirmed bookings "
    "(flights, hotels, car rentals, activities/dining). Use add_booking whenever "
    "someone mentions they have confirmed or booked something. Use list_bookings to "
    "answer questions about what's already booked or total spend. Always confirm with "
    "the user what you saved after adding a booking. When asked how the booking system "
    "works, explain it clearly: say something naturally in a message and Claude will "
    "detect it and record it, or use !claude booked to see everything saved, "
    "!claude scan email to pull confirmations from Gmail, and !claude book save/skip "
    "to confirm or discard found items."
)

SUMMARIZE_PROMPT = (
    "Based on the vacation planning conversation so far, write a concise summary "
    "covering: destinations or locations discussed, itinerary ideas proposed, "
    "key preferences or constraints mentioned (budget, dates, travel style), "
    "decisions made, and any open questions or next steps. "
    "Be factual and brief — this summary will be used as long-term memory for future sessions."
)

# In-memory histories: {chat_id: {trip_name: [messages]}}
_histories: dict[str, dict[str, list[dict]]] = {}

# ---------------------------------------------------------------------------
# Tool definitions for Claude's tool-calling API
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "search_flights",
        "description": (
            "Search for live flight prices and availability between two cities or airports. "
            "Use whenever the user asks about flights, airfare, or travel options between locations. "
            "Args format: '<origin> <destination> <departure-date> [return-date] [adults]' "
            "using IATA codes or city names. Dates in YYYY-MM-DD. "
            "Example: 'JFK Rome 2026-07-15 2026-07-25 2'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "args": {"type": "string", "description": "origin destination date [return-date] [adults]"}
            },
            "required": ["args"]
        }
    },
    {
        "name": "search_hotels",
        "description": (
            "Search for live hotel availability and prices in a city. "
            "Use whenever the user asks about hotels, accommodation, or places to stay. "
            "Args format: '<city> <checkin> <checkout> [guests]'. Dates in YYYY-MM-DD. "
            "Example: 'Rome 2026-07-15 2026-07-22 2'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "args": {"type": "string", "description": "city checkin checkout [guests]"}
            },
            "required": ["args"]
        }
    },
    {
        "name": "search_rentals",
        "description": (
            "Search for vacation rental properties (Airbnb-style) in a city or region. "
            "Use when the user asks about vacation rentals, villas, apartments, or holiday homes. "
            "Args format: '<city> <checkin> <checkout> [guests]'. Dates in YYYY-MM-DD. "
            "Example: 'Tuscany 2026-07-15 2026-07-22 6'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "args": {"type": "string", "description": "city checkin checkout [guests]"}
            },
            "required": ["args"]
        }
    },
    {
        "name": "search_places",
        "description": (
            "Search Google Maps for restaurants, attractions, activities, shops, or any point of interest. "
            "Use whenever the user asks about places to eat, things to do, sights to see, "
            "or any location-based recommendation. "
            "Args: a natural language query like 'best trattorias in Trastevere Rome' "
            "or 'things to do near the Colosseum'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "args": {"type": "string", "description": "natural language place search query"}
            },
            "required": ["args"]
        }
    },
    {
        "name": "search_reviews",
        "description": (
            "Fetch Google Maps reviews for a specific place, hotel, restaurant, or attraction. "
            "Use when the user wants to know what people think of a specific place. "
            "Args: place name and city, e.g. 'Colosseum Rome' or 'Hotel de Russie Rome'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "args": {"type": "string", "description": "place name and city"}
            },
            "required": ["args"]
        }
    },
    {
        "name": "search_events",
        "description": (
            "Search for upcoming events, festivals, concerts, exhibitions, or local happenings in a city. "
            "Use when the user asks what's going on in a destination, or about festivals and events. "
            "Args: '<city> [date]'. Date is optional. "
            "Example: 'Rome 2026-07-15' or just 'Florence'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "args": {"type": "string", "description": "city [date YYYY-MM-DD]"}
            },
            "required": ["args"]
        }
    },
    {
        "name": "search_explore",
        "description": (
            "Get travel inspiration, top attractions, and destination highlights for a place. "
            "Use when the user wants a general overview of a destination, asks what a place is known for, "
            "or is deciding between destinations. "
            "Args: destination name, e.g. 'Amalfi Coast' or 'Rome'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "args": {"type": "string", "description": "destination name"}
            },
            "required": ["args"]
        }
    },

    # ── Booking tools ─────────────────────────────────────────────────────────
    {
        "name": "add_booking",
        "description": (
            "Record a confirmed booking (flight, hotel, car rental, or activity/dining). "
            "Call this when the user confirms they have booked something — e.g. 'I booked our flights', "
            "'we got the Airbnb', 'I made a reservation at'. "
            "Extract as much detail as the user provides. "
            "Always confirm back to the user what was saved."
        ),
        "input_schema": {
            "type": "object",
            "required": ["type", "title"],
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["flight", "hotel", "rental", "activity"],
                    "description": "Category of booking"
                },
                "title": {
                    "type": "string",
                    "description": "Short descriptive title, e.g. 'Delta JFK→FCO', 'Hotel de Russie Rome'"
                },
                "start_date": {"type": "string", "description": "YYYY-MM-DD or YYYY-MM-DDTHH:MM"},
                "end_date":   {"type": "string", "description": "Return/check-out/end date"},
                "confirmation": {"type": "string", "description": "Booking/record locator code"},
                "cost":       {"type": "number"},
                "currency":   {"type": "string", "description": "Currency code, default USD"},
                "notes":      {"type": "string"},
                "booked_by":  {"type": "string"},
                "details": {
                    "type": "object",
                    "description": "Type-specific details",
                    "properties": {
                        "airline":           {"type": "string"},
                        "flight_number":     {"type": "string"},
                        "departure_airport": {"type": "string"},
                        "arrival_airport":   {"type": "string"},
                        "departure_time":    {"type": "string"},
                        "arrival_time":      {"type": "string"},
                        "cabin_class":       {"type": "string"},
                        "passengers":        {"type": "integer"},
                        "property_name":     {"type": "string"},
                        "address":           {"type": "string"},
                        "rooms":             {"type": "integer"},
                        "guests":            {"type": "integer"},
                        "company":           {"type": "string"},
                        "vehicle":           {"type": "string"},
                        "pickup_location":   {"type": "string"},
                        "dropoff_location":  {"type": "string"},
                        "venue":             {"type": "string"},
                        "time":              {"type": "string"},
                        "participants":      {"type": "integer"},
                    }
                }
            }
        }
    },
    {
        "name": "list_bookings",
        "description": (
            "Retrieve all confirmed bookings for the current trip. "
            "Use to answer 'what have we booked?', 'when is check-in?', 'what's our total spend?'."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "update_booking",
        "description": (
            "Update an existing booking — add a confirmation code, correct a date, or add notes. "
            "Requires the booking ID (visible via list_bookings or !claude booked)."
        ),
        "input_schema": {
            "type": "object",
            "required": ["booking_id"],
            "properties": {
                "booking_id":   {"type": "string"},
                "title":        {"type": "string"},
                "start_date":   {"type": "string"},
                "end_date":     {"type": "string"},
                "confirmation": {"type": "string"},
                "cost":         {"type": "number"},
                "currency":     {"type": "string"},
                "notes":        {"type": "string"},
                "details":      {"type": "object"},
            }
        }
    },
    {
        "name": "remove_booking",
        "description": "Delete a cancelled or erroneous booking by ID. Confirm deletion with the user.",
        "input_schema": {
            "type": "object",
            "required": ["booking_id"],
            "properties": {
                "booking_id": {"type": "string"}
            }
        }
    },
    {
        "name": "search_web",
        "description": (
            "General Google web search. Use when search_places or search_reviews can't find "
            "a specific hotel, restaurant, or venue — especially boutique properties not well-indexed "
            "on Google Maps. Also useful for visa requirements, destination guides, travel advisories, "
            "packing lists, and any question that benefits from current web results. "
            "Args: plain search query, e.g. 'Hotel Waldstätterhof Lucerne reviews'"
        ),
        "input_schema": {
            "type": "object",
            "required": ["args"],
            "properties": {"args": {"type": "string", "description": "Search query"}}
        }
    },
    {
        "name": "search_weather",
        "description": (
            "Get current weather conditions and a 3-day forecast for any city or destination. "
            "Use whenever the user asks about weather, what to pack, or best time to visit. "
            "Args: city or location name, e.g. 'Lucerne' or 'Swiss Alps'"
        ),
        "input_schema": {
            "type": "object",
            "required": ["args"],
            "properties": {"args": {"type": "string", "description": "City or location"}}
        }
    },
    {
        "name": "convert_currency",
        "description": (
            "Convert an amount between currencies using current ECB exchange rates. "
            "Use for budget questions like 'how much is 200 CHF in dollars?' or 'what's the EUR/USD rate?' "
            "Args format: '200 CHF to USD' or 'EUR to GBP' (amount optional, defaults to 1)"
        ),
        "input_schema": {
            "type": "object",
            "required": ["args"],
            "properties": {"args": {"type": "string", "description": "'200 CHF to USD' or 'EUR to GBP'"}}
        }
    },
    {
        "name": "search_gas_nearby",
        "description": (
            "Find cheapest gas stations near a city or location with real-time GasBuddy prices, "
            "Top Tier ratings, and composite scores. "
            "Use when someone asks about gas prices at a destination or 'where's cheap gas near X?' "
            "Args: city name, address, or 'lat,lng'. Example: 'Burlington VT' or '44.47,-73.21'"
        ),
        "input_schema": {
            "type": "object",
            "required": ["args"],
            "properties": {"args": {"type": "string", "description": "City name, address, or 'lat,lng'"}}
        }
    },
    {
        "name": "search_gas_along_route",
        "description": (
            "Find gas stops with live GasBuddy pricing along a driving route. "
            "Returns zones spaced along the route with ranked stations, prices, scores, and Top Tier status. "
            "Use when planning a road trip leg — 'where should we fill up driving to X?' "
            "Args: 'origin to destination' with optional road type in brackets. "
            "Examples: 'Philadelphia PA to Montreal QC' or 'NYC to Miami [interstate]'"
        ),
        "input_schema": {
            "type": "object",
            "required": ["args"],
            "properties": {
                "args": {
                    "type": "string",
                    "description": "'origin to destination [road type: interstate|us_highway|state_route|local]'"
                }
            }
        }
    },
]

# Map search tool names to their functions
_SEARCH_FUNCTIONS = {
    "search_flights":   search_flights,
    "search_hotels":    search_hotels,
    "search_rentals":   search_rentals,
    "search_places":    search_places,
    "search_reviews":   search_reviews,
    "search_events":    search_events,
    "search_explore":   search_explore,
    "search_web":              search_web,
    "search_weather":          search_weather,
    "convert_currency":        convert_currency,
    "search_gas_nearby":       search_gas_nearby,
    "search_gas_along_route":  search_gas_along_route,
}


def _execute_tool(name: str, tool_input: dict, chat_id: str = "", trip_name: str = "", _images_out: list | None = None) -> str:
    """Execute a tool call and return the result as a string."""

    # ── Booking tools ─────────────────────────────────────────────────────────
    if name == "add_booking":
        booking_id = _add_booking(chat_id, trip_name, dict(tool_input))
        cost_str = (
            f" Cost: {tool_input.get('currency', 'USD')} {float(tool_input['cost']):,.2f}."
            if tool_input.get("cost") else ""
        )
        conf_str = f" Confirmation: {tool_input['confirmation']}." if tool_input.get("confirmation") else ""
        return (
            f"Booking recorded. ID: {booking_id}. "
            f"Type: {tool_input.get('type')}. Title: {tool_input.get('title')}."
            f"{cost_str}{conf_str}"
        )

    if name == "list_bookings":
        bookings = _get_bookings(chat_id, trip_name)
        if not bookings:
            return "No bookings recorded yet for this trip."
        icons = {"flight": "✈️", "hotel": "🏨", "rental": "🚗", "activity": "🎭"}
        lines = []
        total = 0.0
        for b in bookings:
            icon      = icons.get(b.get("type"), "📌")
            date_part = b.get("start_date", "")
            if b.get("end_date") and b["end_date"] != b.get("start_date"):
                date_part += f" → {b['end_date']}"
            cost_part = f"  ${float(b['cost']):,.2f}" if b.get("cost") else ""
            conf_part = f"  Conf: {b['confirmation']}" if b.get("confirmation") else ""
            lines.append(f"{icon} {b.get('title')} [ID: {b['id']}]  {date_part}{cost_part}{conf_part}".strip())
            try:
                total += float(b.get("cost") or 0)
            except (ValueError, TypeError):
                pass
        result = "\n".join(lines)
        if total > 0:
            result += f"\n\nTotal booked: ${total:,.2f}"
        return result

    if name == "update_booking":
        booking_id = tool_input.get("booking_id")
        if not booking_id:
            return "Error: booking_id is required."
        updates = {k: v for k, v in tool_input.items() if k != "booking_id"}
        success = _update_booking(chat_id, trip_name, booking_id, updates)
        return f"Booking {booking_id} updated." if success else f"No booking found with ID {booking_id}."

    if name == "remove_booking":
        booking_id = tool_input.get("booking_id")
        if not booking_id:
            return "Error: booking_id is required."
        success = _remove_booking(chat_id, trip_name, booking_id)
        return f"Booking {booking_id} removed." if success else f"No booking found with ID {booking_id}."

    # ── Search tools ──────────────────────────────────────────────────────────
    fn = _SEARCH_FUNCTIONS.get(name)
    if fn:
        try:
            result = fn(tool_input.get("args", ""))
            if isinstance(result, tuple):
                text, imgs = result
                if _images_out is not None:
                    _images_out.extend(imgs)
                return text
            return result
        except Exception as e:
            return f"Tool error: {e}"

    return f"Unknown tool: {name}"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _config_path(chat_id: str) -> str:
    return os.path.join(chat_dir(chat_id), "config.json")


def _summary_path(chat_id: str, trip_name: str) -> str:
    return os.path.join(chat_dir(chat_id), f"{trip_name}_summary.txt")


# ---------------------------------------------------------------------------
# Config: trip list and default
# ---------------------------------------------------------------------------

def _load_config(chat_id: str) -> dict:
    path = _config_path(chat_id)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"trips": [], "default": None}


def _save_config(chat_id: str, config: dict) -> None:
    with open(_config_path(chat_id), "w") as f:
        json.dump(config, f, indent=2)


def get_trips(chat_id: str) -> list[str]:
    return _load_config(chat_id).get("trips", [])


def get_default_trip(chat_id: str) -> str | None:
    return _load_config(chat_id).get("default")


def has_trips(chat_id: str) -> bool:
    return len(get_trips(chat_id)) > 0


def create_trip(chat_id: str, trip_name: str) -> tuple[bool, str]:
    """Create a new trip. Returns (success, message)."""
    trip_name = trip_name.lower().strip()
    if not trip_name:
        return False, "Trip name can't be empty."
    if len(trip_name) > 30:
        return False, "Trip name must be 30 characters or fewer."
    if " " in trip_name:
        return False, "Trip name can't contain spaces. Try something like `italy2026` or `beach-trip`."

    config = _load_config(chat_id)
    if trip_name in config["trips"]:
        return False, f'A trip called "{trip_name}" already exists.'

    config["trips"].append(trip_name)
    if config["default"] is None:
        config["default"] = trip_name
    _save_config(chat_id, config)
    return True, trip_name


def set_default_trip(chat_id: str, trip_name: str) -> tuple[bool, str]:
    """Set the default trip. Returns (success, message)."""
    trip_name = trip_name.lower().strip()
    config = _load_config(chat_id)
    if trip_name not in config["trips"]:
        return False, f'No trip called "{trip_name}" found. Use `!claude trips` to see available trips.'
    config["default"] = trip_name
    _save_config(chat_id, config)
    return True, trip_name


def delete_trip(chat_id: str, trip_name: str) -> tuple[bool, str]:
    """Delete a trip and its summary. Returns (success, message)."""
    trip_name = trip_name.lower().strip()
    config = _load_config(chat_id)
    if trip_name not in config["trips"]:
        return False, f'No trip called "{trip_name}" found.'

    config["trips"].remove(trip_name)
    if config["default"] == trip_name:
        config["default"] = config["trips"][0] if config["trips"] else None
    _save_config(chat_id, config)

    # Remove summary file
    path = _summary_path(chat_id, trip_name)
    if os.path.exists(path):
        os.remove(path)

    # Clear in-memory history
    if chat_id in _histories and trip_name in _histories[chat_id]:
        del _histories[chat_id][trip_name]

    return True, trip_name


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def _load_summary(chat_id: str, trip_name: str) -> str:
    path = _summary_path(chat_id, trip_name)
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    return ""


def _append_summary(chat_id: str, trip_name: str, new_summary: str) -> None:
    path = _summary_path(chat_id, trip_name)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n--- {timestamp} ---\n{new_summary}\n"
    with open(path, "a") as f:
        f.write(entry)


def _generate_and_save_summary(chat_id: str, trip_name: str) -> None:
    history = _histories.get(chat_id, {}).get(trip_name, [])
    if not history:
        return
    try:
        response = _get_client().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SUMMARIZE_PROMPT,
            messages=history
        )
        from shared.api_usage import record_anthropic
        record_anthropic(response.usage.input_tokens, response.usage.output_tokens)
        _append_summary(chat_id, trip_name, response.content[0].text.strip())
    except Exception:
        logger.exception("Failed to save summary for %s/%s — history will still be trimmed", chat_id, trip_name)


def save_summarize_now(chat_id: str, trip_name: str | None = None) -> tuple[bool, str]:
    """Manually trigger a summary save. Returns (success, trip_name_used)."""
    if trip_name is None:
        trip_name = get_default_trip(chat_id)
    if trip_name is None:
        return False, ""
    history = _histories.get(chat_id, {}).get(trip_name, [])
    if not history:
        return False, trip_name
    _generate_and_save_summary(chat_id, trip_name)
    return True, trip_name


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def _build_system_prompt(chat_id: str, trip_name: str) -> str:
    prompt = BASE_SYSTEM_PROMPT + f'\n\nYou are currently helping plan the trip "{trip_name}".'

    summary = _load_summary(chat_id, trip_name)
    if summary:
        prompt += (
            "\n\nHere is a running summary of the vacation planning discussions so far:\n\n"
            + summary
        )

    bookings_block = _format_bookings_for_prompt(chat_id, trip_name)
    if bookings_block:
        prompt += "\n\n" + bookings_block

    return prompt


# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------

def ask_claude(message: str, chat_id: str, trip_name: str) -> tuple[str, list[str]]:
    """Send a message to Claude with conversation history, tool access, and URL fetching.

    Claude will autonomously decide when to call search tools based on the question.
    Supports multi-turn tool use (Claude can call multiple tools per response).
    Returns (reply_text, image_urls) where image_urls is populated when search tools
    that return images were called.
    """
    if chat_id not in _histories:
        _histories[chat_id] = {}
    if trip_name not in _histories[chat_id]:
        _histories[chat_id][trip_name] = []

    # Fetch any URLs in the message and prepend their content
    urls = extract_urls(message)
    url_context = build_url_context(urls) if urls else ""
    full_message = url_context + message if url_context else message

    collected_images: list[str] = []
    history = _histories[chat_id][trip_name]
    history.append({"role": "user", "content": full_message})

    # Summarize and trim when window is full
    if len(history) > MAX_HISTORY:
        _generate_and_save_summary(chat_id, trip_name)
        _histories[chat_id][trip_name] = history[-MAX_HISTORY:]
        history = _histories[chat_id][trip_name]

    try:
        # Agentic loop — Claude may call tools multiple times before giving a final answer
        for _iteration in range(MAX_TOOL_ITERATIONS):
            response = _get_client().messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2048,
                system=_build_system_prompt(chat_id, trip_name),
                tools=TOOLS,
                messages=history
            )
            from shared.api_usage import record_anthropic
            record_anthropic(response.usage.input_tokens, response.usage.output_tokens)

            # Claude is done — return the final text response
            if response.stop_reason == "end_turn":
                reply = next(
                    (block.text for block in response.content
                     if hasattr(block, "text")), ""
                )
                history.append({"role": "assistant", "content": response.content})
                return reply, collected_images[:10]

            # Claude wants to use one or more tools
            if response.stop_reason == "tool_use":
                history.append({"role": "assistant", "content": response.content})

                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        result = _execute_tool(
                            block.name, block.input,
                            chat_id=chat_id, trip_name=trip_name,
                            _images_out=collected_images,
                        )
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })

                history.append({"role": "user", "content": tool_results})

            else:
                # Unexpected stop reason — return whatever text we have
                reply = next(
                    (block.text for block in response.content
                     if hasattr(block, "text")), "I wasn't able to generate a response."
                )
                history.append({"role": "assistant", "content": response.content})
                return reply, collected_images[:10]

        # Exhausted iteration limit
        logger.warning("Tool iteration limit (%d) reached for %s/%s", MAX_TOOL_ITERATIONS, chat_id, trip_name)
        return "⚠️ I got a bit stuck working through that. Could you try rephrasing your question?", []

    except anthropic.AuthenticationError:
        history.pop()
        raise RuntimeError("⚠️ Bot config error: invalid Anthropic API key. Ask Jon to check the setup.")

    except anthropic.PermissionDeniedError as e:
        history.pop()
        if "credit balance" in str(e).lower():
            raise RuntimeError("⚠️ Out of API credits. Ask Jon to top up at console.anthropic.com.")
        raise RuntimeError("⚠️ API access denied. Ask Jon to check the Anthropic account.")

    except anthropic.RateLimitError:
        history.pop()
        raise RuntimeError("⚠️ Too many requests right now. Wait a minute and try again.")

    except anthropic.APIStatusError as e:
        history.pop()
        if "credit balance" in str(e).lower():
            raise RuntimeError("⚠️ Out of API credits. Ask Jon to top up at console.anthropic.com.")
        raise RuntimeError(f"⚠️ API error ({e.status_code}). Try again in a moment.")

    except anthropic.APIConnectionError:
        history.pop()
        raise RuntimeError("⚠️ Couldn't reach the Anthropic API. Check the internet connection on the Mac Mini.")


def clear_history(chat_id: str, trip_name: str) -> None:
    """Clear short-term conversation history for a trip. Summary on disk is preserved."""
    if chat_id in _histories and trip_name in _histories[chat_id]:
        _histories[chat_id][trip_name] = []


def rehome_history(old_chat_id: str, new_chat_id: str) -> None:
    """Re-key in-memory histories when a group migrates to a supergroup."""
    if old_chat_id in _histories:
        _histories[new_chat_id] = _histories.pop(old_chat_id)


# ---------------------------------------------------------------------------
# Message parsing
# ---------------------------------------------------------------------------

def strip_trigger(message: str) -> str:
    """Remove the trigger word from the start of a message."""
    if message.lower().startswith(TRIGGER_WORD.lower()):
        return message[len(TRIGGER_WORD):].strip()
    return message.strip()


def parse_trip_selector(text: str) -> tuple[str | None, str]:
    """
    Extract an optional #tripname from the start of a string.
    '!claude #rome what hotels are nearby' -> ('rome', 'what hotels are nearby')
    '!claude what hotels are nearby'        -> (None, 'what hotels are nearby')
    """
    if text.startswith("#"):
        parts = text.split(None, 1)
        trip = parts[0][1:].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        return trip, rest
    return None, text
