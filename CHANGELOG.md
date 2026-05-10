# Changelog

## [Unreleased] — 2026-05-10 (build 6)

### Changed

**MCP client rewritten for FastMCP 3.x streamable HTTP (`shared/mcp_client.py`)**
- FastMCP 3.x dropped the legacy `/sse` GET endpoint; rewrote client to use streamable HTTP (`POST /mcp`).
- Session flow: POST initialize with `stream=True` (background thread keeps connection open to hold session alive) → extract `mcp-session-id` from response header → POST `notifications/initialized` → POST `tools/call` → read SSE result from the tool call POST response body → signal init thread to close.
- Key discovery: FastMCP sessions are tied to the initialize connection lifetime; closing it immediately destroys the session (causes 404 on all subsequent requests). Tool call results arrive as SSE in the tool call POST response, not on the init stream.
- Removed `threading`/`queue` SSE reader pattern; replaced with simpler persistent-stream approach.

### Fixed

**Bearer token auth now working end-to-end**
- Gateway `StaticTokenVerifier` required `"openid"` in scopes (GoogleProvider defaults `required_scopes=["openid"]`; empty scopes caused 403 `insufficient_scope`). Fixed on the gateway side.
- `MCP_API_KEY` set in Portainer stack env and in `.env` on the Mac Mini.
- `search_gas_nearby` and `search_gas_along_route` tested working in Telegram.

## [Unreleased] — 2026-05-09 (build 5)

### Added

**API usage stats (`!claude admin usage`)**
- New owner-DM command showing live SerpApi quota (via `serpapi.com/account.json`) and locally-tracked Anthropic token/call counts and MCP call counts.
- New module `shared/api_usage.py`: `record_anthropic()`, `record_mcp()`, `query_serpapi()`, `format_usage_report()`. Monthly counters reset automatically when the calendar month rolls; all-time `total_calls` accumulates indefinitely. Stats stored in `data/api_usage.json`.
- `shared/claude_client.py`: `record_anthropic()` called after both `messages.create` sites (summarization + agentic loop).
- `shared/mcp_client.py`: `record_mcp()` called after each successful tool result.
- `telegram/bot.py`: `admin usage` case added to `_handle_dm` admin block; also added to admin help text.

## [Unreleased] — 2026-05-09 (build 4)

### Added

**Itinerary display (`!claude booked`)**
- Rewritten `format_for_telegram` in `shared/bookings.py`: chronological day-by-day itinerary grouped by date. Each hotel contributes two events (check-in + check-out), flights show depart/arrive on separate lines when dates differ, rentals show pickup/return.
- Bookings with no `start_date` are separated into a "⚠️ Needs Attention" section with pre-filled `!claude book edit` commands for quick date entry.

**Direct booking manipulation commands**
- `!claude book remove <id>` — delete a booking by its generated ID (`f001_3a7c` style). Works with optional `#trip` selector.
- `!claude book edit <id> field=value ...` — update one or more fields directly without going through Claude. Editable fields: `title`, `start_date`, `end_date`, `confirmation`, `cost`, `notes`. Handles values with spaces (e.g. `notes=Breakfast included great view`). Works with optional `#trip` selector.

**Incomplete-booking warnings after email save**
- After `!claude book save`, any saved bookings missing `start_date` (or `end_date` for hotels) are listed with pre-filled `!claude book edit` fix commands.

**Group names in owner DM commands**
- Bot stores the Telegram group title in `admin_config.json` (`group_names` dict) on every group message.
- Email scan pending DMs now use the group's human name in pre-filled commands (e.g. `My Family Trip` instead of `-1001234567890`).
- Owner DM `book save` / `book skip` now accepts either the group name or numeric ID (backward-compatible). Split on `#` to parse group reference from trip name.
- Files: `shared/admin_config.py` (new: `set_group_name`, `get_group_name`, `resolve_group_by_name`), `shared/pending_bookings.py` (`group_name` param), `telegram/bot.py`

### Fixed

**Email scanner HTML parsing**
- Replaced naive `re.sub(r'<[^>]+>', ...)` with BeautifulSoup (`separator="\n"`, strip script/style/head). Preserves label/value adjacency in HTML table emails so Claude can reliably extract check-in/check-out dates.
- Increased body cap from 8,000 to 12,000 chars.
- Added date-label guidance to extraction prompt (e.g. "Check-in", "Arrival", "Pick-up date").
- Added `logger.debug(...)` after extraction for per-email visibility in logs.
- File: `shared/email_scanner.py`

## [Unreleased] — 2026-05-09 (build 3)

### Added

**Gas price search (`search_gas_nearby`, `search_gas_along_route`)**
- Live GasBuddy prices, Top Tier ratings, and composite scores via `mcp.bitz.dev`.
- `search_gas_nearby` — ranked stations near a city or lat/lng. Claude uses this when someone asks about gas prices at a destination.
- `search_gas_along_route` — zone-based stops along a driving route. Origin geocoded, destination geocoded for lookahead distance, then up to ~6 GasBuddy lookups run concurrently. Output groups top 3 ranked stations per zone with price, distance, score, and Top Tier flag.
- Requires `MCP_API_KEY` in `.env` and a matching API key auth path on the `mcp.bitz.dev` gateway (gateway-side change by Jon).
- Files: `shared/mcp_client.py` (new — SSE/JSON-RPC MCP client), `shared/serpapi_client.py`, `shared/claude_client.py`, `env.template`

**Python MCP client (`shared/mcp_client.py`)**
- Lightweight SSE+JSON-RPC client for `mcp.bitz.dev`. Handles MCP protocol 2024-11-05: SSE endpoint discovery, `initialize` handshake, `notifications/initialized`, `tools/call`. Each call opens a fresh SSE connection in a background thread; results are returned synchronously via a `Queue`. Auth via `Authorization: Bearer <MCP_API_KEY>`.

## [Unreleased] — 2026-05-09 (build 2)

### Added

**General web search (`search_web`)**
- New tool using SerpApi's Google engine. Claude falls back to this automatically when `search_places` or `search_reviews` can't find a specific hotel or venue (e.g., boutique properties not well-indexed on Maps). Returns top 5 organic results with title, snippet, and URL.
- Files: `shared/serpapi_client.py`, `shared/claude_client.py`

**Weather forecasts (`search_weather`)**
- Live current conditions and 3-day forecast via wttr.in. No API key required. Claude uses this proactively when dates or seasons are mentioned.
- Files: `shared/serpapi_client.py`, `shared/claude_client.py`

**Currency conversion (`convert_currency`)**
- Exchange rates via frankfurter.app (European Central Bank data). No API key required. Args format: `"200 CHF to USD"` or `"EUR to GBP"`.
- Files: `shared/serpapi_client.py`, `shared/claude_client.py`

**Reply-based conversation (no trigger word required)**
- Users can now reply directly to any bot message without typing `!claude`. The bot detects `message.reply_to_message.from_user.id == context.bot.id` and processes the reply naturally. Commands typed with `!claude` in replies still work.
- File: `telegram/bot.py` — `handle_message`

### Changed

**System prompt tool guidance**
- Updated `BASE_SYSTEM_PROMPT` to instruct Claude to chain `search_web` as a fallback when Maps tools fail, suggest URL pasting as a last resort, and use weather/currency tools proactively.
- File: `shared/claude_client.py`

## [Unreleased] — 2026-05-09 (build 1)

### Fixed

**Email scan deduplication (partial save bug)**
- When saving only a subset of pending email-found bookings (`!claude book save 1 3`), the remaining items' email IDs were not marked as seen, so they reappeared on every future scan.
- Fix: the `book save` handler now collects all pending email IDs (not just the ones being saved) and marks them all seen before clearing pending. The scan dedup file (`scanned_email_ids.json`) is now always fully populated after any save or skip action.
- File: `telegram/bot.py` — `_process_group_message`, `book save` block

**NoneType sort error crashing all Claude responses**
- Any booking with `"start_date": null` in its JSON caused `TypeError: '<' not supported between instances of 'NoneType' and 'str'` on every Claude query, because `_build_system_prompt()` calls `format_for_prompt()` to build the bookings block on every request.
- Root cause: `dict.get("start_date", "")` returns `None` when the key is present but explicitly `null` — the default only fires when the key is absent.
- Fix: changed both sort lambdas to `x.get("start_date") or ""`.
- File: `shared/bookings.py` — `format_for_prompt` (line 167) and `format_for_telegram` (line 196)

### Changed

**Email scan output routed to owner DM**
- Email scan results (both manual `!claude scan email` and the daily 08:00 scheduled scan) now go to the bot owner's DM instead of the group chat.
- The group receives no message until a booking is confirmed — only a brief "✅ N booking(s) added to {trip}" notification is posted to the group after the owner saves via DM.
- Falls back to posting in the group if `BOT_OWNER_ID` is not set in `.env`.
- Files: `telegram/bot.py`, `shared/pending_bookings.py`

### Added

**Owner DM commands for reviewing email scan results**
- `!claude book save <group_chat_id> #<trip> [all|1 3]` — save pending bookings from the owner's DM, specifying which group and trip to save to.
- `!claude book skip <group_chat_id> #<trip>` — discard pending items from the owner's DM.
- The pending list sent to the owner's DM includes pre-filled versions of these commands (with the correct `<group_chat_id>` and `#<trip>` already inserted) so they can be copied and sent directly.
- Files: `telegram/bot.py` (`_handle_dm`), `shared/pending_bookings.py` (`format_pending_for_telegram`)
