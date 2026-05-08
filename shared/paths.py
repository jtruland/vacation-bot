import os

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'data'))


def chat_dir(chat_id: str) -> str:
    """Return (and create) the per-chat data directory."""
    safe = str(chat_id).replace("-", "neg")
    path = os.path.join(DATA_DIR, safe)
    os.makedirs(path, exist_ok=True)
    return path
