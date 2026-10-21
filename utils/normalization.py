import logging
import math
import re
import unicodedata

import pandas as pd

logger = logging.getLogger(__name__)

VALID_MARKETS = {"us", "jp", "idx"}
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9^][A-Z0-9._\-^=]{0,14}$")


def normalize_market(market, default="us"):
    """Validates and normalizes market identifier."""
    value = str(market or default).strip().lower()
    return value if value in VALID_MARKETS else None


def history_circuit_key(symbol: str, market: str | None = None) -> str:
    """Return the canonical circuit-breaker key for stock history.

    Every layer (route, async task, service, and provider) must derive the key
    through this function. When one layer used the bare symbol while another
    used ``market:symbol``, the two failure counters were independent and a
    failing upstream kept receiving requests instead of failing fast.
    """
    key_symbol = str(symbol or "").strip()
    if not key_symbol:
        return ""
    normalized_market = normalize_market(market, default="")
    if normalized_market is None:
        if key_symbol.endswith(".T"):
            normalized_market = "jp"
        elif key_symbol.startswith("^"):
            normalized_market = "idx"
        elif is_jp_stock_code(key_symbol):
            normalized_market = "jp"
        else:
            normalized_market = "us"
    return f"{normalized_market}:{key_symbol}"


def normalize_symbol(symbol):
    """Clean up stock symbol string and strip exchange prefixes if present."""
    if symbol is None:
        return ""
    if not isinstance(symbol, str):
        symbol = str(symbol)
    # NFKC-normalize BEFORE storing so the persisted key is exactly the form
    # that is_valid_symbol() validates (it NFKC-normalizes internally) and that
    # yfinance can resolve. Without this, fullwidth input such as ``ＡＡＰＬ`` or
    # ``７２０３`` passed is_valid_symbol() (which NFKC-normalizes before the
    # pattern check) but was stored unnormalized and could never be resolved by
    # the providers, leaving an unquotable watchlist entry.
    s = unicodedata.normalize("NFKC", symbol.strip()).upper()
    if ":" in s:
        from utils.tradingview_mapper import get_internal_symbol_from_tv_symbol

        return get_internal_symbol_from_tv_symbol(s)
    return s


def normalize_text(value, default=""):
    """テキスト値を正規化して返す。"""
    if value is None:
        return default
    return str(value).strip()


JP_ALPHANUM_CODE_RE = re.compile(r"^[0-9][A-Z0-9]{3}$")


def is_jp_stock_code(code: object) -> bool:
    """Return True if code is a 4-digit or 4-character alphanumeric Japanese securities code."""
    if not code or not isinstance(code, str):
        return False
    s = code.strip().upper()
    return bool(JP_ALPHANUM_CODE_RE.match(s))


def normalize_symbol_for_market(symbol, market):
    """Adjusts symbol formatting based on market rules (e.g., .T for JP)."""
    s = normalize_symbol(symbol)
    if market == "jp" and not s.endswith(".T") and is_jp_stock_code(s):
        return f"{s}.T"
    return s


def is_jp_market_symbol(symbol: object) -> bool:
    """Return True when *symbol* uses the JP-market ``*.T`` spelling.

    Mirrors the convention already enforced by ``/api/stocks/portfolio`` (which
    accepts any ``*.T`` JP holding and rejects ``*.T`` symbols in the US
    market). The bare numeric form is rewritten to ``*.T`` by
    ``normalize_symbol_for_market`` before this check runs, so callers see the
    persisted spelling. Symbol character validation stays in
    ``is_valid_symbol`` so synthetic/legacy ``*.T`` rows keep working.
    """
    if not isinstance(symbol, str):
        return False
    return symbol.strip().upper().endswith(".T")


def market_symbol_mismatch(symbol: object, market: object) -> str | None:
    """Return a reason when *symbol* cannot belong to *market*, else ``None``.

    A watchlist entry carries one ``market`` and that market decides how the
    payload is valued: ``build_stock_payload`` forces ``JPY`` for every ``jp``
    entry and portfolio updates reject non ``*.T`` JP holdings. Accepting a US
    ticker under the ``jp`` market therefore creates an entry whose price is
    displayed/valued in the wrong currency and whose holdings can never be set,
    so the write boundary must reject the pair instead of persisting it.

    ``idx`` intentionally accepts any symbol because indices/ETFs are quoted in
    mixed currencies and are resolved from provider metadata.
    """
    if not isinstance(symbol, str) or not symbol.strip():
        return None
    s = symbol.strip().upper()
    if market == "jp" and not is_jp_market_symbol(s):
        return "market/symbol mismatch: JP market requires JP symbol (e.g., 7203.T)"
    if market == "us" and s.endswith(".T"):
        return "market/symbol mismatch: US market cannot accept JP symbol (e.g., 7203.T)"
    return None


def is_valid_symbol(symbol):
    """強化されたシンボル検証（SQLインジェクションやパストラバーサル対策）"""
    # Coerce to str first: this validator's contract is "return bool", so a
    # non-string value (e.g. an int from a raw JSON body) must be rejected
    # with False, never raise TypeError.
    if symbol is None:
        return False
    symbol_str = str(symbol)
    if not symbol_str or len(symbol_str) > 15:
        return False
    dangerous_chars = ["/", "\\", "..", "\0", "%", "\x00", "\n", "\r"]
    if any(char in symbol_str for char in dangerous_chars):
        return False
    symbol_normalized = unicodedata.normalize("NFKC", symbol_str).upper()
    return bool(SYMBOL_PATTERN.match(symbol_normalized))


def normalize_optional_number(value, allow_negative=False):
    """Noneや不正値を除外して数値に変換する"""
    try:
        if value is None or isinstance(value, bool) or type(value).__name__ in ("bool_", "bool"):
            return None
        num = float(value)
        if pd.isna(num) or not math.isfinite(num):
            return None
        if not allow_negative and num <= 0:
            return None
        return num
    except (ValueError, TypeError, OverflowError):
        return None


def _fmt(v):
    """Round to 2 decimal places; return None for NaN/Inf/None/bool.

    Rejects both NaN and Inf so a single non-finite value from the data source
    can never break ``json.dumps(..., allow_nan=False)`` in the SSE stream.
    """
    try:
        if (
            v is None
            or isinstance(v, bool)
            or type(v).__name__ in ("bool_", "bool")
            or (isinstance(v, float) and pd.isna(v))
        ):
            return None
        num = float(v)
        if not math.isfinite(num):
            return None
        return round(num, 2)
    except (TypeError, ValueError, OverflowError):
        return None


def _fmt_vol(v):
    """Convert to int volume; return None for NaN/Inf/None/bool."""
    try:
        if (
            v is None
            or isinstance(v, bool)
            or type(v).__name__ in ("bool_", "bool")
            or (isinstance(v, float) and pd.isna(v))
        ):
            return None
        num = float(v)
        if not math.isfinite(num):
            return None
        return int(num)
    except (TypeError, ValueError, OverflowError):
        return None


def normalize_history_frame(hist, inplace=False):
    """
    データフレームを正規化：インデックスを DatetimeIndex に変換、Close 列をチェック
    入力検証：非 DataFrame/None 入力に対応
    """
    if hist is None or getattr(hist, "empty", True):
        return pd.DataFrame()

    if not isinstance(hist, pd.DataFrame):
        logger.warning(
            "normalize_history_frame: non-DataFrame input: type=%s",
            type(hist).__name__,
        )
        return pd.DataFrame()

    try:
        frame = hist if inplace else hist.copy()
        if not isinstance(frame.index, pd.DatetimeIndex):
            try:
                frame.index = pd.to_datetime(frame.index)
            except (ValueError, TypeError) as exc:
                logger.warning("Failed to convert history index to DatetimeIndex: %s", exc)
                return pd.DataFrame()

        if "Close" not in frame.columns:
            logger.warning("normalize_history_frame: 'Close' column not found in DataFrame")
            return pd.DataFrame()

        # A NaT index reaches ``Timestamp.timestamp()`` in both the chart and
        # history serializers and can abort an otherwise usable response.
        # Treat it like a missing close value at the normalization boundary.
        frame = frame[frame.index.notna()]
        frame = frame.dropna(subset=["Close"])
        return frame
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        logger.exception("normalize_history_frame error")
        return pd.DataFrame()
