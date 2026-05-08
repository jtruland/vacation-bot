"""
DM routing: join codes, DM↔group links, trip routing, and DM activity log.

Join codes are stored per group in data/{group_chat_id}/config.json under "dm_code".
DM links are stored globally in data/dm_store.json: {dm_chat_id: [group_chat_id, ...]}.
DM activity log is stored per group in data/{group_chat_id}/dm_activity.json.
"""

import json
import logging
import os
import random
import string

from shared.paths import chat_dir, dm_store_path

logger = logging.getLogger(__name__)


class AmbiguousTripError(Exception):
    """Raised when a trip name matches trips in multiple linked groups."""
    def __init__(self, trip_name: str, group_ids: list[str]):
        self.trip_name = trip_name
        self.group_ids = group_ids
        super().__init__(f"Trip '{trip_name}' exists in multiple linked groups: {group_ids}")


# ─── Join code management ──────────────────────────────────────────────────────

def _group_config_path(group_chat_id: str) -> str:
    return os.path.join(chat_dir(group_chat_id), "config.json")


def _load_group_config(group_chat_id: str) -> dict:
    p = _group_config_path(group_chat_id)
    if not os.path.exists(p):
        return {}
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_group_config(group_chat_id: str, cfg: dict) -> None:
    with open(_group_config_path(group_chat_id), "w") as f:
        json.dump(cfg, f, indent=2)


def generate_dm_code(group_chat_id: str) -> str:
    """Generate and store a short alphanumeric join code for this group."""
    code = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    cfg = _load_group_config(group_chat_id)
    cfg["dm_code"] = code
    _save_group_config(group_chat_id, cfg)
    return code


def disable_dm_code(group_chat_id: str) -> None:
    """Remove the active join code for this group."""
    cfg = _load_group_config(group_chat_id)
    cfg.pop("dm_code", None)
    _save_group_config(group_chat_id, cfg)


def get_group_for_code(code: str) -> str | None:
    """Scan all chat data directories for a matching dm_code. Returns group chat_id or None."""
    from shared.paths import DATA_DIR
    if not os.path.isdir(DATA_DIR):
        return None
    for name in os.listdir(DATA_DIR):
        dir_path = os.path.join(DATA_DIR, name)
        if not os.path.isdir(dir_path):
            continue
        cfg_path = os.path.join(dir_path, "config.json")
        if not os.path.exists(cfg_path):
            continue
        try:
            with open(cfg_path) as f:
                cfg = json.load(f)
            if cfg.get("dm_code") == code:
                # Reverse the safe encoding to get the original chat_id
                return '-' + name[3:] if name.startswith('neg') else name
        except Exception:
            continue
    return None


# ─── DM link management ────────────────────────────────────────────────────────

def _load_store() -> dict:
    p = dm_store_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_store(store: dict) -> None:
    with open(dm_store_path(), "w") as f:
        json.dump(store, f, indent=2)


def link_dm_to_group(dm_chat_id: str, group_chat_id: str) -> None:
    store = _load_store()
    groups = store.setdefault(str(dm_chat_id), [])
    if str(group_chat_id) not in groups:
        groups.append(str(group_chat_id))
    _save_store(store)


def unlink_dm_from_group(dm_chat_id: str, group_chat_id: str) -> bool:
    store = _load_store()
    groups = store.get(str(dm_chat_id), [])
    if str(group_chat_id) in groups:
        groups.remove(str(group_chat_id))
        _save_store(store)
        return True
    return False


def get_linked_groups(dm_chat_id: str) -> list[str]:
    return _load_store().get(str(dm_chat_id), [])


# ─── Trip routing ──────────────────────────────────────────────────────────────

def resolve_dm_trip(dm_chat_id: str, trip_name: str | None) -> tuple[str, str] | None:
    """
    Given a DM chat_id and optional trip name, return (group_chat_id, trip_name).

    - If trip_name is given, finds which linked group owns it.
      Raises AmbiguousTripError if the name exists in multiple groups.
    - If trip_name is None, returns the sole linked group + its default trip.
      Returns None if DM is not linked to any group.
    """
    from shared.claude_client import get_trips, get_default_trip

    linked = get_linked_groups(dm_chat_id)
    if not linked:
        return None

    if trip_name:
        matches = [(g, trip_name) for g in linked if trip_name in get_trips(g)]
        if not matches:
            return None
        if len(matches) > 1:
            raise AmbiguousTripError(trip_name, [m[0] for m in matches])
        return matches[0]
    else:
        if len(linked) == 1:
            default = get_default_trip(linked[0])
            return (linked[0], default) if default else None
        # Multiple groups, no trip specified — caller must ask user to specify
        return None


# ─── DM activity log ───────────────────────────────────────────────────────────

def _activity_path(group_chat_id: str) -> str:
    return os.path.join(chat_dir(group_chat_id), "dm_activity.json")


def log_dm_activity(group_chat_id: str, trip_name: str, entry: str) -> None:
    """Append a one-line DM activity entry for display in the group."""
    p = _activity_path(group_chat_id)
    entries = []
    if os.path.exists(p):
        try:
            with open(p) as f:
                entries = json.load(f)
        except Exception:
            entries = []
    entries.append(f"[{trip_name}] {entry}")
    with open(p, "w") as f:
        json.dump(entries, f, indent=2)


def pop_dm_activity(group_chat_id: str) -> list[str]:
    """Return all pending DM activity entries and clear the log."""
    p = _activity_path(group_chat_id)
    if not os.path.exists(p):
        return []
    try:
        with open(p) as f:
            entries = json.load(f)
        os.remove(p)
        return entries
    except Exception:
        return []
