"""
Rolling monthly API usage tracking + SerpApi live quota query.
Stats stored in data/api_usage.json; monthly counters reset on month rollover.
"""
import json
import logging
import os
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

logger = logging.getLogger(__name__)


def _path() -> str:
    from shared.paths import DATA_DIR
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, "api_usage.json")


def _load() -> dict:
    try:
        with open(_path()) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict) -> None:
    with open(_path(), "w") as f:
        json.dump(data, f, indent=2)


def _current_month() -> str:
    return datetime.now().strftime("%Y-%m")


def _maybe_reset(section: dict) -> dict:
    if section.get("month") != _current_month():
        return {
            "month": _current_month(),
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_calls": section.get("total_calls", 0),
        }
    return section


def record_anthropic(input_tokens: int, output_tokens: int) -> None:
    try:
        data = _load()
        s = _maybe_reset(data.get("anthropic", {}))
        s["calls"] = s.get("calls", 0) + 1
        s["input_tokens"] = s.get("input_tokens", 0) + input_tokens
        s["output_tokens"] = s.get("output_tokens", 0) + output_tokens
        s["total_calls"] = s.get("total_calls", 0) + 1
        data["anthropic"] = s
        _save(data)
    except Exception:
        logger.debug("api_usage: failed to record anthropic call", exc_info=True)


def record_mcp() -> None:
    try:
        data = _load()
        s = _maybe_reset(data.get("mcp", {}))
        s["calls"] = s.get("calls", 0) + 1
        s["total_calls"] = s.get("total_calls", 0) + 1
        data["mcp"] = s
        _save(data)
    except Exception:
        logger.debug("api_usage: failed to record mcp call", exc_info=True)


def query_serpapi() -> dict | None:
    key = os.getenv("SERPAPI_KEY")
    if not key:
        return None
    try:
        r = requests.get(
            "https://serpapi.com/account.json",
            params={"api_key": key},
            timeout=8,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        logger.debug("api_usage: serpapi account query failed", exc_info=True)
        return None


def format_usage_report() -> str:
    data = _load()
    month = _current_month()
    month_label = datetime.now().strftime("%B %Y")
    lines = ["📊 *API Usage Stats*"]

    # SerpApi — live
    sa = query_serpapi()
    if sa is None:
        lines.append("\n*SerpApi* — not configured")
    else:
        plan = sa.get("plan_name") or sa.get("plan") or "Unknown"
        per_month = sa.get("plan_searches_left", 0) + sa.get("this_month_usage", 0)
        used = sa.get("this_month_usage", 0)
        remaining = sa.get("plan_searches_left", 0)
        rate = sa.get("searches_per_hour_limit", 0)
        lines.append("\n*SerpApi* — live")
        lines.append(f"  Plan: {plan} · {per_month:,} searches/month")
        lines.append(f"  Used this month: {used:,} · Remaining: {remaining:,}")
        if rate:
            lines.append(f"  Rate limit: {rate:,}/hr")

    # Anthropic — local
    ant = data.get("anthropic", {})
    if ant.get("month") != month:
        ant = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_calls": ant.get("total_calls", 0)}
    lines.append(f"\n*Anthropic (Claude Haiku)* — local tracking, {month_label}")
    lines.append(f"  Calls this month: {ant.get('calls', 0):,} (total: {ant.get('total_calls', 0):,})")
    lines.append(f"  Input tokens: {ant.get('input_tokens', 0):,}")
    lines.append(f"  Output tokens: {ant.get('output_tokens', 0):,}")

    # MCP — local
    mcp = data.get("mcp", {})
    if mcp.get("month") != month:
        mcp = {"calls": 0, "total_calls": mcp.get("total_calls", 0)}
    lines.append(f"\n*MCP (mcp.bitz.dev / GasBuddy)* — local tracking, {month_label}")
    lines.append(f"  Calls this month: {mcp.get('calls', 0):,} (total: {mcp.get('total_calls', 0):,})")

    lines.append("\n_Anthropic + MCP counts tracked locally since first call._")
    lines.append("_SerpApi data is live from serpapi.com._")

    return "\n".join(lines)
