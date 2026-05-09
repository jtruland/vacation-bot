# Changelog

## [Unreleased] — 2026-05-09

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
