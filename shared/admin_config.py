"""
Admin configuration: allowed group chat allowlist, managed via bot commands.

Stored at data/admin_config.json. On first run, bootstrapped from the
ALLOWED_CHAT_IDS (or legacy ALLOWED_CHAT_ID) env var if set.
"""

import json
import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from shared.paths import admin_config_path, dm_store_path, rename_chat_dir

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

logger = logging.getLogger(__name__)

_EMPTY = {"allowed_chat_ids": [], "pending": {}, "group_names": {}}


def _load() -> dict:
    p = admin_config_path()
    if not os.path.exists(p):
        return dict(_EMPTY)
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return dict(_EMPTY)


def _save(cfg: dict) -> None:
    with open(admin_config_path(), "w") as f:
        json.dump(cfg, f, indent=2)


# ─── Bootstrap ─────────────────────────────────────────────────────────────────

def bootstrap_from_env() -> None:
    """If admin_config.json doesn't exist yet, seed from env vars."""
    if os.path.exists(admin_config_path()):
        return
    raw = os.getenv("ALLOWED_CHAT_IDS", os.getenv("ALLOWED_CHAT_ID", ""))
    ids = [c.strip() for c in raw.split(",") if c.strip()]
    if ids:
        cfg = dict(_EMPTY)
        cfg["allowed_chat_ids"] = ids
        _save(cfg)
        logger.info("Admin config bootstrapped from env: %s", ids)
    else:
        _save(dict(_EMPTY))
        logger.info("Admin config created (empty allowlist — all groups pending approval)")


# ─── Allowlist CRUD ────────────────────────────────────────────────────────────

def is_allowed(chat_id: str) -> bool:
    return str(chat_id) in _load().get("allowed_chat_ids", [])


def get_allowed() -> list[str]:
    return _load().get("allowed_chat_ids", [])


def get_pending() -> dict:
    return _load().get("pending", {})


def add_pending(chat_id: str, name: str) -> None:
    cfg = _load()
    cfg.setdefault("pending", {})[str(chat_id)] = {
        "name": name,
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    _save(cfg)


def allow_chat(chat_id: str) -> None:
    cfg = _load()
    cid = str(chat_id)
    cfg.setdefault("allowed_chat_ids", [])
    if cid not in cfg["allowed_chat_ids"]:
        cfg["allowed_chat_ids"].append(cid)
    cfg.get("pending", {}).pop(cid, None)
    _save(cfg)


def deny_chat(chat_id: str) -> None:
    """Remove from pending (bot stays in group but remains ignored)."""
    cfg = _load()
    cfg.get("pending", {}).pop(str(chat_id), None)
    _save(cfg)


def revoke_chat(chat_id: str) -> None:
    """Remove from allowed list."""
    cfg = _load()
    cid = str(chat_id)
    cfg["allowed_chat_ids"] = [c for c in cfg.get("allowed_chat_ids", []) if c != cid]
    cfg.get("pending", {}).pop(cid, None)
    _save(cfg)


# ─── Group migration (supergroup upgrade) ──────────────────────────────────────

def rehome_chat(old_chat_id: str, new_chat_id: str) -> bool:
    """
    Called when a group migrates to a supergroup (chat_id changes).
    Renames the data directory and updates all stored references.
    Returns True if anything was changed.
    """
    old, new = str(old_chat_id), str(new_chat_id)
    changed = False

    # 1. Rename data directory
    if rename_chat_dir(old, new):
        changed = True
        logger.info("Renamed data dir %s → %s", old, new)

    # 2. Update admin_config.json
    cfg = _load()
    allowed = cfg.get("allowed_chat_ids", [])
    if old in allowed:
        cfg["allowed_chat_ids"] = [new if c == old else c for c in allowed]
        changed = True
    pending = cfg.get("pending", {})
    if old in pending:
        pending[new] = pending.pop(old)
        changed = True
    if changed:
        _save(cfg)

    # 3. Update dm_store.json
    p = dm_store_path()
    if os.path.exists(p):
        try:
            with open(p) as f:
                store = json.load(f)
            updated = False
            for dm_id, groups in store.items():
                if old in groups:
                    store[dm_id] = [new if g == old else g for g in groups]
                    updated = True
            if updated:
                with open(p, "w") as f:
                    json.dump(store, f, indent=2)
                changed = True
        except Exception as e:
            logger.error("Failed to update dm_store during rehome: %s", e)

    return changed


# ─── Group name store ──────────────────────────────────────────────────────────

def set_group_name(chat_id: str, title: str) -> None:
    """Persist the human-readable name of a group chat."""
    cfg = _load()
    cfg.setdefault("group_names", {})[str(chat_id)] = title
    _save(cfg)


def get_group_name(chat_id: str) -> str | None:
    """Return the stored display name for a group, or None if not recorded."""
    return _load().get("group_names", {}).get(str(chat_id))


def resolve_group_by_name(name: str) -> str | None:
    """Return the chat_id for a group name (case-insensitive). None if not found or ambiguous."""
    names = _load().get("group_names", {})
    matches = [cid for cid, t in names.items() if t.lower() == name.lower()]
    return matches[0] if len(matches) == 1 else None
