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
import os
import requests
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

def _format_hotels(results: list, city: str, checkin: str, checkout: str) -> str:
    if not results:
        return f"No hotels found in {city} for those dates."
    lines = [f"🏨 *Hotels in {city}* ({checkin} → {checkout}):\n"]
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
    lines.append("\n_Verify and book on Google Hotels or the hotel's site._")
    return "\n".join(lines)


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

        lines.append("\n_Prices are estimates. Book directly on the listing platform._")
        return "\n".join(lines)
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

        lines.append("\n_Results from Google Maps._")
        return "\n".join(lines)
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
    """!claude events <city> [date] — e.g. 'Rome' or 'Rome 2026-07-15'"""
    parts = args.strip().split()
    if not parts:
        return (
            "❌ Format: `!claude events <city> [date]`\n"
            "Examples:\n"
            "`!claude events Rome`\n"
            "`!claude events Florence 2026-07-15`"
        )
    city = parts[0]
    date_filter = None
    if len(parts) > 1:
        try:
            date_filter = _parse_date(" ".join(parts[1:]))
        except ValueError:
            pass  # treat as part of the city name

    try:
        params = {
            "engine": "google_events",
            "q": f"events in {city}",
            "hl": "en",
            "gl": "us",
        }
        if date_filter:
            params["htichips"] = f"date:{date_filter}"

        data = _search(params)
        if "error" in data:
            return f"⚠️ Events search error: {data['error']}"

        events = data.get("events_results", [])
        if not events:
            return f"No upcoming events found in {city.title()}."

        lines = [f"🎭 *Events in {city.title()}*:\n"]
        for i, e in enumerate(events[:5], 1):
            title = e.get("title", "Unknown Event")
            date = e.get("date", {})
            date_str = date.get("when", "")
            venue_info = e.get("venue", {})
            venue = venue_info.get("name", "")
            address = venue_info.get("address", "")
            description = e.get("description", "")[:120]

            venue_str = f"\n   📍 {venue}" if venue else ""
            addr_str = f"\n   🗺 {address}" if address and not venue else ""
            date_display = f"\n   🗓 {date_str}" if date_str else ""
            desc_str = f"\n   {description}..." if description else ""

            lines.append(f"{i}. *{title}*{date_display}{venue_str}{addr_str}{desc_str}")

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
    import re
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
