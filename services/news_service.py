"""
News service helper logic.
Handles news item freshness policy and recent news filtering.
"""

from datetime import datetime, timezone
from utils.formatting import _parse_datetime_to_utc


def _market_news_freshness_policy(market="us"):
    """Returns (max_age_hours, allow_undated_limit) for news filtering."""
    if str(market).strip().lower() == "jp":
        return 24, 1
    return 48, 3


def _filter_recent_market_news_items(
    items, max_age_hours=48, allow_undated_limit=2, max_items=10
):
    """Filters news items based on age and limits results."""
    if not isinstance(items, list):
        return []

    now = datetime.now(timezone.utc)
    filtered = []
    undated_remaining = max(0, int(allow_undated_limit))
    max_items = max(1, int(max_items))

    for item in items:
        if len(filtered) >= max_items:
            break

        if not isinstance(item, dict):
            continue

        date_text = str(item.get("date") or "").strip()
        dt = _parse_datetime_to_utc(date_text)
        if dt is not None:
            age = now - dt
            if age.total_seconds() <= max_age_hours * 3600:
                filtered.append(item)
            continue

        if undated_remaining > 0:
            filtered.append(item)
            undated_remaining -= 1

    return filtered
