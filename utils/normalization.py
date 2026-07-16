import logging
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


def normalize_symbol(symbol):
    """Clean up stock symbol string."""
    if symbol is None:
        return ""
    if not isinstance(symbol, str):
        symbol = str(symbol)
    return symbol.strip().upper()


def normalize_text(value, default=""):
    """テキスト値を正規化して返す。"""
    if value is None:
        return default
    return str(value).strip()


def normalize_symbol_for_market(symbol, market):
    """Adjusts symbol formatting based on market rules (e.g., .T for JP)."""
    s = normalize_symbol(symbol)
    if market == "jp" and s.isdigit():
        return f"{s}.T"
    return s


def is_valid_symbol(symbol):
    """強化されたシンボル検証（SQLインジェクションやパストラバーサル対策）"""
    if not symbol or len(symbol) > 15:
        return False
    symbol_str = str(symbol)
    dangerous_chars = ["/", "\\", "..", "\0", "%", "\x00", "\n", "\r"]
    if any(char in symbol_str for char in dangerous_chars):
        return False
    symbol_normalized = unicodedata.normalize("NFKC", symbol_str)
    if not SYMBOL_PATTERN.match(symbol_normalized):
        return False
    return True


def normalize_optional_number(value):
    """Noneや不正値を除外して数値に変換する"""
    try:
        if value is None:
            return None
        num = float(value)
        if pd.isna(num) or num <= 0:
            return None
        return num
    except (ValueError, TypeError):
        return None


def _fmt(v):
    """Round to 2 decimal places; return None for NaN/None."""
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _fmt_vol(v):
    """Convert to int volume; return None for NaN/None."""
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return int(float(v))
    except (TypeError, ValueError):
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

        frame = frame.dropna(subset=["Close"])
        return frame
    except (AttributeError, KeyError, TypeError, ValueError) as norm_exc:
        logger.error("normalize_history_frame error: %s", norm_exc, exc_info=True)
        return pd.DataFrame()
