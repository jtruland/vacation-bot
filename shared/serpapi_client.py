"""
SerpApi integration for travel search via Google engines.
Requires: pip install google-search-results
Sign up for a free API key (250 searches/month) at: https://serpapi.com

Supported engines:
  - google_flights         -> !claude flights
  - google_hotels          -> !claude hotels
  - google_vacation_rentals-> !claude rentals
  - google_maps            -> !claude places
  - google_maps_reviews    -> !claude reviews
  - google_events          -> !claude events
  - google_travel_explore  -> !claude explore
"""
import math
import os
import re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))


def _get_api_key() -> str:
    key = os.getenv("SERPAPI_KEY")
    if not key:
        raise RuntimeError(
            "⚠️ SerpApi key not set. Ask Jon to add SERPAPI_KEY to the .env file."
        )
    return key


def _search(params: dict) -> dict:
    """Run a SerpApi search and return the result dict."""
    from serpapi import GoogleSearch
    params["api_key"] = _get_api_key()
    return GoogleSearch(params).get_dict()


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def _parse_date(date_str: str) -> str:
    formats = [
        "%Y-%m-%d", "%m/%d/%Y",
        "%B %d %Y", "%b %d %Y",
        "%B %d, %Y", "%b %d, %Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(
        f'Couldn\'t parse date "{date_str}". '
        'Use a format like 2026-07-15 or 07/15/2026.'
    )


def _format_duration(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m" if m else f"{h}h"


# ---------------------------------------------------------------------------
# Flights
# ---------------------------------------------------------------------------

def _format_flights(data: dict, origin: str, dest: str, return_date: str | None) -> str:
    trip_type = "Round trip" if return_date else "One-way"
    best = data.get("best_flights", []) + data.get("other_flights", [])
    if not best:
        return f"No flights found from {origin} to {dest} for those dates."

    lines = [f"✈️ *{trip_type}: {origin} → {dest}* — top {min(len(best), 5)} options:\n"]
    for i, option in enumerate(best[:5], 1):
        price = option.get("price", "N/A")
        flights = option.get("flights", [])
        total_duration = option.get("total_duration", 0)
        layovers = option.get("layovers", [])
        stops = len(layovers)
        stop_text = "Nonstop" if stops == 0 else f"{stops} stop{'s' if stops > 1 else ''}"
        if flights:
            first, last = flights[0], flights[-1]
            dep = first.get("departure_airport", {})
            arr = last.get("arrival_airport", {})
            airline = first.get("airline", "")
            dur = _format_duration(total_duration) if total_duration else ""
            lines.append(
                f"{i}. *{airline}* | {dep.get('id','')} {dep.get('time','')} → "
                f"{arr.get('id','')} {arr.get('time','')} | {stop_text} | {dur} | *${price}*"
            )
            if layovers:
                linfo = ", ".join(
                    f"{l.get('name','')} ({_format_duration(l.get('duration',0))})"
                    for l in layovers
                )
                lines.append(f"   Layover: {linfo}")
        else:
            lines.append(f"{i}. *${price}* | {stop_text}")
    lines.append("\n_Prices per person. Book on Google Flights or directly with the airline._")
    return "\n".join(lines)


def search_flights(args: str) -> str:
    parts = args.strip().split()
    if len(parts) < 3:
        return (
            "❌ Format: `!claude flights <from> <to> <date> [return-date] [guests]`\n"
            "Example: `!claude flights JFK Rome 2026-07-15 2026-07-25 2`"
        )
    origin, dest = parts[0].upper(), parts[1].upper()
    adults = 1
    if parts[-1].isdigit() and len(parts) > 3:
        adults = int(parts[-1])
        parts = parts[:-1]
    try:
        departure_date = _parse_date(parts[2])
        return_date = _parse_date(parts[3]) if len(parts) > 3 else None
    except ValueError as e:
        return f"❌ {e}"
    try:
        params = {
            "engine": "google_flights",
            "departure_id": origin,
            "arrival_id": dest,
            "outbound_date": departure_date,
            "currency": "USD",
            "hl": "en",
            "adults": adults,
            "type": "1" if return_date else "2",
        }
        if return_date:
            params["return_date"] = return_date
        data = _search(params)
        if "error" in data:
            return f"⚠️ Flight search error: {data['error']}"
        return _format_flights(data, origin, dest, return_date)
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        return f"⚠️ Flight search failed: {e}"


# ---------------------------------------------------------------------------
# Hotels
# ---------------------------------------------------------------------------

def _format_hotels(results: list, city: str, checkin: str, checkout: str) -> tuple[str, list[str]]:
    if not results:
        return f"No hotels found in {city} for those dates.", []
    lines = [f"🏨 *Hotels in {city}* ({checkin} → {checkout}):\n"]
    images = []
    for i, h in enumerate(results[:5], 1):
        name = h.get("name", "Unknown")
        rating = h.get("overall_rating", "")
        reviews = h.get("reviews", "")
        price = h.get("rate_per_night", {}).get("lowest", "N/A")
        total = h.get("total_rate", {}).get("lowest", "")
        stars = h.get("hotel_class", "")
        rating_str = f" | ⭐ {rating}/5 ({reviews} reviews)" if rating else ""
        stars_str = f" | {stars}" if stars else ""
        total_str = f" _(~{total} total)_" if total else ""
        lines.append(f"{i}. *{name}*{stars_str}{rating_str}\n   {price}/night{total_str}")
        thumb = ((h.get("images") or [{}])[0]).get("thumbnail", "")
        if thumb:
            images.append(thumb)
    lines.append("\n_Verify and book on Google Hotels or the hotel's site._")
    return "\n".join(lines), images


def search_hotels(args: str) -> str:
    parts = args.strip().split()
    if len(parts) < 3:
        return (
            "❌ Format: `!claude hotels <city> <checkin> <checkout> [guests]`\n"
            "Example: `!claude hotels Rome 2026-07-15 2026-07-22 2`"
        )
    city = parts[0]
    adults = 2
    if parts[-1].isdigit() and len(parts) > 3:
        adults = int(parts[-1])
        parts = parts[:-1]
    try:
        checkin, checkout = _parse_date(parts[1]), _parse_date(parts[2])
    except ValueError as e:
        return f"❌ {e}"
    try:
        data = _search({
            "engine": "google_hotels",
            "q": f"hotels in {city}",
            "check_in_date": checkin,
            "check_out_date": checkout,
            "adults": adults,
            "currency": "USD",
            "hl": "en",
        })
        if "error" in data:
            return f"⚠️ Hotel search error: {data['error']}"
        return _format_hotels(data.get("properties", []), city.title(), checkin, checkout)
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        return f"⚠️ Hotel search failed: {e}"


# ---------------------------------------------------------------------------
# Vacation Rentals
# ---------------------------------------------------------------------------

def search_rentals(args: str) -> str:
    """!claude rentals <city> <checkin> <checkout> [guests]"""
    parts = args.strip().split()
    if len(parts) < 3:
        return (
            "❌ Format: `!claude rentals <city> <checkin> <checkout> [guests]`\n"
            "Example: `!claude rentals Rome 2026-07-15 2026-07-22 4`"
        )
    city = parts[0]
    adults = 2
    if parts[-1].isdigit() and len(parts) > 3:
        adults = int(parts[-1])
        parts = parts[:-1]
    try:
        checkin, checkout = _parse_date(parts[1]), _parse_date(parts[2])
    except ValueError as e:
        return f"❌ {e}"
    try:
        data = _search({
            "engine": "google_vacation_rentals",
            "q": f"vacation rentals in {city}",
            "check_in_date": checkin,
            "check_out_date": checkout,
            "adults": adults,
            "currency": "USD",
            "hl": "en",
        })
        if "error" in data:
            return f"⚠️ Rental search error: {data['error']}"

        rentals = data.get("rentals_results", [])
        if not rentals:
            return f"No vacation rentals found in {city} for those dates."

        lines = [f"🏠 *Vacation Rentals in {city.title()}* ({checkin} → {checkout}):\n"]
        images = []
        for i, r in enumerate(rentals[:5], 1):
            name = r.get("name", "Unknown")
            rating = r.get("overall_rating", "")
            reviews = r.get("reviews", "")
            price = r.get("rate_per_night", {}).get("extracted_lowest", "N/A")
            total = r.get("total_rate", {}).get("extracted_lowest", "")
            prop_type = r.get("type", "")
            rating_str = f" | ⭐ {rating}/5 ({reviews} reviews)" if rating else ""
            type_str = f" | {prop_type}" if prop_type else ""
            total_str = f" _(~${total} total)_" if total else ""
            lines.append(f"{i}. *{name}*{type_str}{rating_str}\n   ${price}/night{total_str}")
            if r.get("thumbnail"):
                images.append(r["thumbnail"])

        lines.append("\n_Prices are estimates. Book directly on the listing platform._")
        return "\n".join(lines), images
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        return f"⚠️ Rental search failed: {e}"


# ---------------------------------------------------------------------------
# Places (Google Maps)
# ---------------------------------------------------------------------------

def search_places(args: str) -> str:
    """!claude places <query> — e.g. 'best restaurants in Rome' or 'things to do Florence'"""
    if not args.strip():
        return (
            "❌ Format: `!claude places <query>`\n"
            "Examples:\n"
            "`!claude places best restaurants in Rome`\n"
            "`!claude places things to do near the Colosseum`\n"
            "`!claude places coffee shops Florence`"
        )
    try:
        data = _search({
            "engine": "google_maps",
            "q": args.strip(),
            "type": "search",
            "hl": "en",
        })
        if "error" in data:
            return f"⚠️ Places search error: {data['error']}"

        places = data.get("local_results", [])
        if not places:
            return f"No places found for \"{args.strip()}\"."

        lines = [f"📍 *Places: {args.strip()}*\n"]
        images = []
        for i, p in enumerate(places[:5], 1):
            name = p.get("title", "Unknown")
            rating = p.get("rating", "")
            reviews = p.get("reviews", "")
            category = p.get("type", "")
            address = p.get("address", "")
            hours = p.get("hours", "")
            price = p.get("price", "")

            rating_str = f" | ⭐ {rating} ({reviews} reviews)" if rating else ""
            price_str = f" | {price}" if price else ""
            category_str = f" | _{category}_" if category else ""
            hours_str = f"\n   🕐 {hours}" if hours else ""
            addr_str = f"\n   📍 {address}" if address else ""

            lines.append(
                f"{i}. *{name}*{category_str}{rating_str}{price_str}{hours_str}{addr_str}"
            )
            if p.get("thumbnail"):
                images.append(p["thumbnail"])

        lines.append("\n_Results from Google Maps._")
        return "\n".join(lines), images
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        return f"⚠️ Places search failed: {e}"


# ---------------------------------------------------------------------------
# Reviews (Google Maps Reviews)
# ---------------------------------------------------------------------------

def search_reviews(args: str) -> str:
    """!claude reviews <place name and city> — e.g. 'Colosseum Rome'"""
    if not args.strip():
        return (
            "❌ Format: `!claude reviews <place> <city>`\n"
            "Example: `!claude reviews Colosseum Rome`"
        )
    try:
        # Step 1: find the place to get its data_id
        maps_data = _search({
            "engine": "google_maps",
            "q": args.strip(),
            "type": "search",
            "hl": "en",
        })
        if "error" in maps_data:
            return f"⚠️ Could not find place: {maps_data['error']}"

        places = maps_data.get("local_results", [])
        if not places:
            return f"Couldn't find \"{args.strip()}\" on Google Maps."

        place = places[0]
        place_name = place.get("title", args.strip())
        data_id = place.get("data_id")

        if not data_id:
            return f"Found *{place_name}* but couldn't retrieve its reviews."

        # Step 2: get reviews
        reviews_data = _search({
            "engine": "google_maps_reviews",
            "data_id": data_id,
            "hl": "en",
            "sort_by": "ratingHigh",
        })
        if "error" in reviews_data:
            return f"⚠️ Reviews error: {reviews_data['error']}"

        place_info = reviews_data.get("place_info", {})
        reviews = reviews_data.get("reviews", [])
        overall = place_info.get("rating", "")
        total_reviews = place_info.get("reviews", "")

        if not reviews:
            return f"No reviews found for *{place_name}*."

        lines = [
            f"⭐ *{place_name}*",
            f"Rating: {overall}/5 ({total_reviews} total reviews)\n"
        ]
        for i, r in enumerate(reviews[:5], 1):
            author = r.get("user", {}).get("name", "Anonymous")
            rating = r.get("rating", "")
            date = r.get("date", "")
            snippet = r.get("snippet", "No text")
            stars = "⭐" * int(rating) if rating else ""
            lines.append(f"{i}. *{author}* {stars} _{date}_\n   \"{snippet}\"")

        lines.append("\n_Reviews from Google Maps._")
        return "\n".join(lines)
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        return f"⚠️ Reviews search failed: {e}"


# ---------------------------------------------------------------------------
# Events (Google Events)
# ---------------------------------------------------------------------------

def search_events(args: str) -> str:
    """!claude events <city/region> [start_date] [end_date]
    e.g. 'Rome', 'Florence 2026-07-15', 'Lucerne Switzerland 2026-12-1 2026-12-4'
    """
    tokens = args.strip().split()
    if not tokens:
        return (
            "❌ Format: `!claude events <city/region> [start_date] [end_date]`\n"
            "Examples:\n"
            "`!claude events Rome`\n"
            "`!claude events Florence 2026-07-15`\n"
            "`!claude events Lucerne Switzerland 2026-12-1 2026-12-4`"
        )

    # Peel date tokens from the right; everything remaining is the location
    dates = []
    city_tokens = list(tokens)
    while city_tokens:
        try:
            dates.insert(0, _parse_date(city_tokens[-1]))
            city_tokens.pop()
        except ValueError:
            break
    city = " ".join(city_tokens) if city_tokens else args.strip()
    start_date = dates[0] if dates else None
    end_date   = dates[1] if len(dates) > 1 else None

    # Embed date context in the query — htichips only accepts keywords, not specific dates
    query = f"events in {city}"
    if start_date and end_date:
        s = datetime.strptime(start_date, "%Y-%m-%d")
        e = datetime.strptime(end_date, "%Y-%m-%d")
        if s.month == e.month and s.year == e.year:
            query += f" {s.strftime('%B')} {s.day}-{e.day} {s.year}"
        else:
            query += f" {s.strftime('%B %d')} to {e.strftime('%B %d %Y')}"
    elif start_date:
        s = datetime.strptime(start_date, "%Y-%m-%d")
        query += f" {s.strftime('%B %d %Y')}"

    try:
        data = _search({
            "engine": "google_events",
            "q": query,
            "hl": "en",
        })
        if "error" in data:
            return f"⚠️ Events search error: {data['error']}"

        events = data.get("events_results", [])
        if not events:
            return f"No upcoming events found in {city}."

        lines = [f"🎭 *Events in {city}*:\n"]
        for i, e in enumerate(events[:5], 1):
            title       = e.get("title", "Unknown Event")
            date_str    = e.get("date", {}).get("when", "")
            venue       = e.get("venue", {}).get("name", "")
            address     = e.get("venue", {}).get("address", "")
            description = e.get("description", "")[:120]
            link        = e.get("link", "")

            title_display = f"[{title}]({link})" if link else f"*{title}*"
            date_display  = f"\n   🗓 {date_str}" if date_str else ""
            venue_str     = f"\n   📍 {venue}" if venue else ""
            addr_str      = f"\n   🗺 {address}" if address and not venue else ""
            desc_str      = f"\n   {description}..." if description else ""

            lines.append(f"{i}. {title_display}{date_display}{venue_str}{addr_str}{desc_str}")

        lines.append("\n_Events from Google. Verify details before attending._")
        return "\n".join(lines)
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        return f"⚠️ Events search failed: {e}"


# ---------------------------------------------------------------------------
# Travel Explore (Google Travel Explore)
# ---------------------------------------------------------------------------

def search_explore(args: str) -> str:
    """!claude explore <destination> — travel insights, best times, popular spots"""
    if not args.strip():
        return (
            "❌ Format: `!claude explore <destination>`\n"
            "Examples:\n"
            "`!claude explore Rome`\n"
            "`!claude explore Amalfi Coast`"
        )
    try:
        # Google Travel Explore via google_flights with explore mode
        data = _search({
            "engine": "google_flights",
            "departure_id": "anywhere",
            "arrival_id": args.strip(),
            "type": "3",   # explore type
            "currency": "USD",
            "hl": "en",
        })

        # Fall back to a maps search for destination info if explore returns nothing useful
        if "error" in data or (not data.get("best_flights") and not data.get("explore_results")):
            data = _search({
                "engine": "google_maps",
                "q": f"top attractions in {args.strip()}",
                "type": "search",
                "hl": "en",
            })
            places = data.get("local_results", [])
            if not places:
                return f"Couldn't find travel information for \"{args.strip()}\"."

            lines = [f"🌍 *Exploring {args.strip().title()}* — top attractions:\n"]
            for i, p in enumerate(places[:5], 1):
                name = p.get("title", "")
                rating = p.get("rating", "")
                reviews = p.get("reviews", "")
                category = p.get("type", "")
                rating_str = f" | ⭐ {rating} ({reviews} reviews)" if rating else ""
                cat_str = f" | _{category}_" if category else ""
                lines.append(f"{i}. *{name}*{cat_str}{rating_str}")
            lines.append("\n_Results from Google Maps._")
            return "\n".join(lines)

        # Format explore results if available
        explore = data.get("explore_results", [])
        lines = [f"🌍 *Explore: {args.strip().title()}*\n"]
        for i, item in enumerate(explore[:5], 1):
            dest = item.get("destination", "")
            price = item.get("price", "")
            duration = item.get("duration", "")
            lines.append(f"{i}. *{dest}* | from ${price} | {duration}")

        lines.append("\n_Travel insights from Google._")
        return "\n".join(lines)

    except RuntimeError as e:
        return str(e)
    except Exception as e:
        return f"⚠️ Explore search failed: {e}"


# ---------------------------------------------------------------------------
# General web search
# ---------------------------------------------------------------------------

def search_web(args: str) -> str:
    """General Google web search — fallback for anything Maps-based tools miss."""
    if not args.strip():
        return "❌ Provide a search query."
    try:
        data = _search({"engine": "google", "q": args.strip(), "hl": "en", "num": 5})
        if "error" in data:
            return f"⚠️ Web search error: {data['error']}"
        results = data.get("organic_results", [])
        if not results:
            return f"No web results found for: {args}"
        lines = [f"🔍 *Web results for: {args}*\n"]
        for r in results[:5]:
            title   = r.get("title", "")
            snippet = r.get("snippet", "")
            link    = r.get("link", "")
            lines.append(f"*{title}*\n{snippet}\n_{link}_\n")
        return "\n".join(lines)
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        return f"⚠️ Web search failed: {e}"


# ---------------------------------------------------------------------------
# Weather (wttr.in — no API key required)
# ---------------------------------------------------------------------------

def search_weather(args: str) -> str:
    """Current conditions + 3-day forecast for any city via wttr.in."""
    location = args.strip()
    if not location:
        return "❌ Provide a city or location."
    try:
        resp = requests.get(
            f"https://wttr.in/{requests.utils.quote(location)}",
            params={"format": "j1"},
            timeout=8,
            headers={"User-Agent": "vacation-bot/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()
        current  = data["current_condition"][0]
        temp_c   = current["temp_C"]
        temp_f   = current["temp_F"]
        desc     = current["weatherDesc"][0]["value"]
        humidity = current["humidity"]
        wind     = current["windspeedKmph"]

        lines = [
            f"🌤 *Weather in {location.title()}*\n",
            f"Now: {desc}, {temp_c}°C / {temp_f}°F",
            f"Humidity: {humidity}%  Wind: {wind} km/h\n",
            "*3-Day Forecast:*",
        ]
        for label, forecast in zip(["Today", "Tomorrow", "Day 3"], data.get("weather", [])[:3]):
            hi_c  = forecast["maxtempC"]
            lo_c  = forecast["mintempC"]
            hi_f  = forecast["maxtempF"]
            lo_f  = forecast["mintempF"]
            fdesc = forecast["hourly"][4]["weatherDesc"][0]["value"]
            lines.append(f"  {label}: {fdesc}, {lo_c}–{hi_c}°C / {lo_f}–{hi_f}°F")

        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ Weather lookup failed: {e}"


# ---------------------------------------------------------------------------
# Currency conversion (frankfurter.app — no API key required)
# ---------------------------------------------------------------------------

def convert_currency(args: str) -> str:
    """Convert between currencies using ECB rates via frankfurter.app."""
    args = args.strip().upper()
    m = re.match(r'^(?:([\d,\.]+)\s+)?([A-Z]{3})\s+TO\s+([A-Z]{3})$', args)
    if not m:
        return "❌ Format: `200 CHF to USD` or `EUR to GBP`"
    amount_str, from_cur, to_cur = m.groups()
    amount = float(amount_str.replace(",", "")) if amount_str else 1.0
    try:
        resp = requests.get(
            "https://api.frankfurter.app/latest",
            params={"amount": amount, "from": from_cur, "to": to_cur},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        rate = data["rates"].get(to_cur)
        if rate is None:
            return f"❌ Unsupported currency: {to_cur}"
        return (
            f"💱 *Currency Conversion*\n"
            f"{amount:,.2f} {from_cur} = *{rate:,.2f} {to_cur}*\n"
            f"_Rate as of {data['date']} (European Central Bank)_"
        )
    except Exception as e:
        return f"⚠️ Currency conversion failed: {e}"


# ---------------------------------------------------------------------------
# Gas prices (GasBuddy + Google Maps via mcp.bitz.dev)
# Requires MCP_API_KEY in .env
# ---------------------------------------------------------------------------

def _mcp_geocode(location: str) -> tuple[float, float, str]:
    """Geocode a location string. Returns (lat, lng, display_name)."""
    from shared.mcp_client import call_tool
    result = call_tool("maps_geocode_address", {"address": location})
    lat_m = re.search(r'Latitude:\s*([\-\d.]+)', result)
    lng_m = re.search(r'Longitude:\s*([\-\d.]+)', result)
    addr_m = re.search(r'Address:\s*(.+)', result)
    if not lat_m or not lng_m:
        raise ValueError(f"Could not geocode \"{location}\"")
    return (
        float(lat_m.group(1)),
        float(lng_m.group(1)),
        addr_m.group(1).strip() if addr_m else location,
    )


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(a))


def _parse_zones(text: str) -> list[tuple[int, str, float, float]]:
    """Parse (miles, city, lat, lng) tuples from maps_find_gas_stations_nearby output."""
    pattern = re.compile(
        r'~(\d+) miles ahead — (.+?)\n\s+Waypoint:\s*([\d.]+),\s*([\-\d.]+)',
        re.MULTILINE,
    )
    return [
        (int(m.group(1)), m.group(2).strip(), float(m.group(3)), float(m.group(4)))
        for m in pattern.finditer(text)
    ]


def _parse_gas_stations(gas_text: str) -> tuple[str, list[dict]]:
    """
    Parse gasbuddy_search_gas_by_gps output into (fuel_type, stations).
    Each station: rank, brand, street, city_state, price, distance, score, top_tier.
    """
    fuel_m = re.search(r'Cheapest\s+(\w+)\s+gas\s+near', gas_text, re.IGNORECASE)
    fuel_type = fuel_m.group(1).capitalize() if fuel_m else "Regular"

    stations = []
    for block in re.split(r'\n(?=\d+\.)', gas_text.strip()):
        m = re.match(
            r'^(\d+)\.\s+(.+?)\s+—\s+\$([0-9.]+)\s+([0-9.]+)mi\s+\[score:\s+([0-9.]+)\]',
            block.strip(),
        )
        if not m:
            continue
        lines = [l.strip() for l in block.split('\n') if l.strip()]
        # GasBuddy output: line 0 = "N. Brand — ...", line 1 = street, line 2 = city/state
        street     = lines[1] if len(lines) > 1 else ""
        city_state = lines[2] if len(lines) > 2 else ""
        top_tier = (
            "✅" if any("✅ Top Tier (confirmed)" in l for l in lines)
            else ("🔶" if any("🔶 Top Tier (probable)" in l for l in lines) else "")
        )
        stations.append({
            "rank":       int(m.group(1)),
            "brand":      m.group(2).strip(),
            "street":     street,
            "city_state": city_state,
            "price":      f"${m.group(3)}",
            "distance":   f"{m.group(4)} mi",
            "score":      m.group(5),
            "top_tier":   top_tier,
        })
    return fuel_type, stations


def _format_gas_table(stations: list[dict], fuel_type: str, n: int | None = None) -> str:
    """Format stations as a Markdown table per gasbuddy_display_instructions.md."""
    subset = stations[:n] if n else stations
    if not subset:
        return ""
    rows = [
        "| Rank | Station | Price | Distance | Score | Quality |",
        "|------|---------|-------|----------|-------|---------|",
    ]
    for s in subset:
        # Station cell: Name, then city/state, then street (per instructions)
        station_cell = f"**{s['brand']}**<br>{s['city_state']}<br>{s['street']}"
        quality = f"{fuel_type} {s['top_tier']}".strip()
        rows.append(
            f"| {s['rank']} | {station_cell} | {s['price']} | {s['distance']} | {s['score']} | {quality} |"
        )
    return "\n".join(rows)


def search_gas_nearby(args: str) -> str:
    """Cheapest gas near a city or lat,lng — uses GasBuddy via mcp.bitz.dev."""
    from shared.mcp_client import call_tool
    args = args.strip()
    if not args:
        return "❌ Provide a city or location."

    lat_lng_m = re.match(r'^([\-\d.]+)\s*,\s*([\-\d.]+)$', args)
    if lat_lng_m:
        lat, lng = float(lat_lng_m.group(1)), float(lat_lng_m.group(2))
        display = args
    else:
        try:
            lat, lng, display = _mcp_geocode(args)
        except Exception as e:
            return f"⚠️ Couldn't locate \"{args}\": {e}"

    try:
        raw = call_tool("gasbuddy_search_gas_by_gps", {"lat": lat, "lng": lng})
    except RuntimeError as e:
        return f"⚠️ Gas price lookup failed: {e}"

    fuel_type, stations = _parse_gas_stations(raw)
    if not stations:
        return f"⚠️ No gas pricing data found near {display}."

    table = _format_gas_table(stations, fuel_type)
    return f"⛽ *Cheapest gas near {display}*\n\n{table}"


def search_gas_along_route(args: str) -> str:
    """Gas stops with live GasBuddy pricing along a driving route."""
    from shared.mcp_client import call_tool
    args = args.strip()
    m = re.match(r'^(.+?)\s+to\s+(.+?)(?:\s+\[([^\]]*)\])?$', args, re.IGNORECASE)
    if not m:
        return "❌ Format: `Philadelphia PA to Montreal QC` or `NYC to Miami [interstate]`"

    origin_str = m.group(1).strip()
    dest_str   = m.group(2).strip()
    road_hint  = m.group(3).strip() if m.group(3) else None

    try:
        origin_lat, origin_lng, origin_display = _mcp_geocode(origin_str)
    except Exception as e:
        return f"⚠️ Couldn't locate origin \"{origin_str}\": {e}"

    try:
        dest_lat, dest_lng, _ = _mcp_geocode(dest_str)
        lookahead = max(100, int(_haversine_miles(origin_lat, origin_lng, dest_lat, dest_lng) * 1.4))
    except Exception:
        lookahead = 500

    zone_params: dict = {
        "lat": origin_lat,
        "lng": origin_lng,
        "destination": dest_str,
        "lookahead_miles": lookahead,
    }
    if road_hint:
        zone_params["road_hint"] = road_hint

    try:
        zones_text = call_tool("maps_find_gas_stations_nearby", zone_params)
    except RuntimeError as e:
        return f"⚠️ Route lookup failed: {e}"

    zones = _parse_zones(zones_text)
    if not zones:
        return f"No gas zones found for route {origin_str} → {dest_str}."

    def _fetch_zone(zone: tuple) -> tuple:
        miles, city, lat, lng = zone
        try:
            raw = call_tool("gasbuddy_search_gas_by_gps", {"lat": lat, "lng": lng})
            fuel_type, stations = _parse_gas_stations(raw)
            return miles, city, fuel_type, stations
        except Exception:
            return miles, city, "Regular", []

    with ThreadPoolExecutor(max_workers=4) as executor:
        zone_data = list(executor.map(_fetch_zone, zones))

    # Build output: zone headers + tables, gap flags between empty zones
    parts = [f"⛽ *Gas stops: {origin_display} → {dest_str}*\n"]
    zone_num = 0
    gap_start: int | None = None

    for miles, city, fuel_type, stations in zone_data:
        if not stations:
            if gap_start is None:
                gap_start = miles
        else:
            if gap_start is not None:
                parts.append(
                    f"⚠️ Gap — ~{gap_start} to ~{miles} miles: No stations found in GasBuddy\n"
                )
                gap_start = None
            zone_num += 1
            table = _format_gas_table(stations, fuel_type, n=3)
            parts.append(f"**Zone {zone_num} — {city} (~{miles} miles)**\n{table}\n")

    if gap_start is not None:
        last_miles = zone_data[-1][0]
        parts.append(
            f"⚠️ Gap — ~{gap_start} to ~{last_miles} miles: No stations found in GasBuddy"
        )

    return "\n".join(parts)
