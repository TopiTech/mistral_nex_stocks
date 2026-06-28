from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from utils.validators import normalize_analysis_result


def _parse_datetime_to_utc(value):
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    # Unix timestamp
    if text.isdigit():
        try:
            return datetime.fromtimestamp(int(text), timezone.utc)
        except (ValueError, OverflowError):
            pass

    # RFC 2822 / RFC 1123 and other common formats
    try:
        parsed = parsedate_to_datetime(text)
        if parsed is not None:
            return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        pass

    # Basic UTC timestamp format without separators
    try:
        return datetime.strptime(text, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        pass

    # ISO 8601 variants
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def build_fallback_analysis_result(reason: str = "") -> dict[str, Any]:
    """Builds a neutral fallback result when AI analysis fails."""
    base = normalize_analysis_result({})
    if reason:
        base["analysis_summary"] = f"構造化出力に失敗したため保守的判定: {reason[:80]}"
    base["fallback_used"] = True
    return base
