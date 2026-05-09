# Changelog

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
