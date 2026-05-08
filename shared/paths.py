import os

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'data'))


def _safe(chat_id: str) -> str:
    return str(chat_id).replace("-", "neg")


def chat_dir(chat_id: str) -> str:
    """Return (and create) the per-chat data directory."""
    path = os.path.join(DATA_DIR, _safe(chat_id))
    os.makedirs(path, exist_ok=True)
    return path


def admin_config_path() -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, "admin_config.json")


def dm_store_path() -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, "dm_store.json")


def list_chat_ids() -> list[str]:
    """Return all chat IDs that have a data directory."""
    if not os.path.isdir(DATA_DIR):
        return []
    result = []
    for name in os.listdir(DATA_DIR):
        if os.path.isdir(os.path.join(DATA_DIR, name)):
            chat_id = '-' + name[3:] if name.startswith('neg') else name
            result.append(chat_id)
    return result


def rename_chat_dir(old_chat_id: str, new_chat_id: str) -> bool:
    """Rename the data directory when a group migrates to a supergroup."""
    old_path = os.path.join(DATA_DIR, _safe(old_chat_id))
    new_path = os.path.join(DATA_DIR, _safe(new_chat_id))
    if os.path.isdir(old_path) and not os.path.exists(new_path):
        os.rename(old_path, new_path)
        return True
    return False
