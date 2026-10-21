"""Market data integration helpers.

This module centralizes the conversion of raw market payloads into the
normalized row shapes used by the heatmap and screener APIs. Route handlers
should call these helpers instead of re-implementing field extraction and
fallback logic inline.
"""

from __future__ import annotations

import logging
from typing import Any

from app_state import app_state
from sectors import PREDEFINED_NAMES, PREDEFINED_SECTORS
from utils.market_utils import is_market_open
from utils.normalization import normalize_optional_number
from utils.stock_payload import get_stock_info_cached

logger = logging.getLogger(__name__)


def _default_fetch_stocks_batch(
    items: list[tuple[str, str, str]],
    snapshot_ts_ms: int | None = None,
    lightweight: bool = False,
    period: str = "3mo",
) -> list[Any]:
    """Proxy the batch fetch implementation from app_bg.

    Importing lazily keeps this module usable from tests without introducing a
    hard dependency cycle at import time.
    """
    from app_bg import fetch_stocks_batch as _fetch_stocks_batch

    return _fetch_stocks_batch(
        items,
        snapshot_ts_ms=snapshot_ts_ms,
        lightweight=lightweight,
        period=period,
    )


def fetch_stocks_batch(
    items: list[tuple[str, str, str]],
    snapshot_ts_ms: int | None = None,
    lightweight: bool = False,
    period: str = "3mo",
) -> list[Any]:
    """Public batch-fetch hook used by tests and default call sites."""
    return _default_fetch_stocks_batch(
        items,
        snapshot_ts_ms=snapshot_ts_ms,
        lightweight=lightweight,
        period=period,
    )


def _extract_change_pct(data_dict: dict[str, Any]) -> float:
    for key in (
        "change_percent",
        "regularMarketChangePercent",
        "priceChangePercent",
        "changePercent",
    ):
        val = normalize_optional_number(data_dict.get(key), allow_negative=True)
        if val is not None:
            return val
    return 0.0


def _extract_change_val(data_dict: dict[str, Any]) -> float:
    for key in ("change", "change_value", "regularMarketChange"):
        val = normalize_optional_number(data_dict.get(key), allow_negative=True)
        if val is not None:
            return val
    return 0.0


def _first_not_none(*values: Any) -> Any:
    """Return the first value that is not None, preserving valid 0.0 numbers."""
    for val in values:
        if val is not None:
            return val
    return None


def _build_market_row(
    symbol: str,
    market: str,
    source: dict[str, Any],
    fallback_name: str,
) -> dict[str, Any]:
    price_val = _first_not_none(
        normalize_optional_number(source.get("price")),
        normalize_optional_number(source.get("close")),
        normalize_optional_number(source.get("regularMarketPrice")),
        normalize_optional_number(source.get("currentPrice")),
    )
    price = price_val if price_val is not None else 0.0

    shares_outstanding = _first_not_none(
        normalize_optional_number(source.get("sharesOutstanding")),
        normalize_optional_number(source.get("shares_outstanding")),
    )
    shares_cap = (
        (shares_outstanding * price)
        if shares_outstanding is not None and shares_outstanding > 0 and price > 0
        else 0.0
    )
    mkt_cap_val = _first_not_none(
        normalize_optional_number(source.get("market_cap")),
        normalize_optional_number(source.get("marketCap")),
    )
    market_cap = mkt_cap_val if mkt_cap_val is not None else shares_cap

    vol_val = normalize_optional_number(source.get("volume"), allow_zero=True)
    volume = vol_val if vol_val is not None else 0.0

    high_val = _first_not_none(
        normalize_optional_number(source.get("high")),
        normalize_optional_number(source.get("regularMarketDayHigh")),
        normalize_optional_number(source.get("dayHigh")),
    )
    high = high_val if high_val is not None else price

    low_val = _first_not_none(
        normalize_optional_number(source.get("low")),
        normalize_optional_number(source.get("regularMarketDayLow")),
        normalize_optional_number(source.get("dayLow")),
    )
    low = low_val if low_val is not None else price

    sector = source.get("sector") or PREDEFINED_SECTORS.get(symbol, "Other")
    pe_ratio = _first_not_none(
        normalize_optional_number(source.get("pe_ratio"), allow_zero=True),
        normalize_optional_number(source.get("trailingPE"), allow_zero=True),
        normalize_optional_number(source.get("forwardPE"), allow_zero=True),
        normalize_optional_number(source.get("pe"), allow_zero=True),
    )
    return {
        "symbol": symbol,
        "name": source.get("name")
        or source.get("shortName")
        or source.get("longName")
        or PREDEFINED_NAMES.get(symbol, fallback_name),
        "market": market,
        "price": price,
        "change_percent": _extract_change_pct(source),
        "change_value": _extract_change_val(source),
        "market_cap": market_cap,
        "pe_ratio": pe_ratio,
        "volume": volume,
        "high": high,
        "low": low,
        "sector": sector,
        "sharesOutstanding": shares_outstanding,
    }


def build_heatmap_payload(
    market: str,
    symbols: list[str],
    *,
    fetch_batch_fn=None,
) -> dict[str, list[dict[str, Any]]]:
    """Build the normalized heatmap payload for the given market.

    The function intentionally keeps orchestration here so route handlers only
    request the result instead of reconstructing market-cap and sector rows.
    """
    batch_fetch = fetch_batch_fn or fetch_stocks_batch
    items = [(symbol, "", market) for symbol in symbols]
    fetched = batch_fetch(items, lightweight=True)
    results: list[dict[str, Any]] = []

    for item in fetched:
        if not item:
            continue

        price_val = _first_not_none(
            normalize_optional_number(item.get("price")),
            normalize_optional_number(item.get("close")),
        )
        price = price_val if price_val is not None else 0.0
        shares = _first_not_none(
            normalize_optional_number(item.get("sharesOutstanding")),
            normalize_optional_number(item.get("shares_outstanding")),
        )
        # Market cap must stay a capitalisation. ``price * volume`` is trading
        # value and differs by orders of magnitude, so a symbol with neither a
        # reported cap nor shares outstanding is omitted rather than sized
        # with a dimensionally different number.
        shares_cap = (shares * price) if (shares is not None and shares > 0 and price > 0) else 0.0
        mkt_cap_val = _first_not_none(
            normalize_optional_number(item.get("market_cap")),
            normalize_optional_number(item.get("marketCap")),
        )
        market_cap = mkt_cap_val if mkt_cap_val is not None else shares_cap
        if market_cap <= 0:
            continue

        sym = item.get("symbol")
        if not sym:
            continue
        name = (
            item.get("name")
            or item.get("shortName")
            or item.get("longName")
            or PREDEFINED_NAMES.get(sym, sym)
        )

        results.append(
            {
                "symbol": sym,
                "name": name,
                "price": price,
                "change_percent": _extract_change_pct(item),
                "market_cap": market_cap,
                "sector": item.get("sector") or PREDEFINED_SECTORS.get(sym, "Other"),
            }
        )

    results.sort(key=lambda row: row.get("market_cap", 0), reverse=True)
    return {"stocks": results}


def build_screener_enrichment(
    items: list[tuple[str, str, str]],
    full_fetch_symbol: str | None,
    *,
    fetch_batch_fn=None,
    get_info_fn=None,
) -> dict[str, dict[str, Any]]:
    """Build normalized screener rows for symbols that are not already registered."""
    batch_fetch = fetch_batch_fn or fetch_stocks_batch
    info_fetch = get_info_fn or get_stock_info_cached
    rows: dict[str, dict[str, Any]] = {}
    missing_items: list[tuple[str, str, str]] = []

    for sym, fallback_name, mkt in items:
        cache_key = f"payload_{sym}_{mkt}"
        cached_p = None
        try:
            cached_p = app_state.payload_disk_cache.get(cache_key)
        except Exception as exc:  # pragma: no cover - defensive fallback
            logger.debug("Failed reading payload_disk_cache for %s: %s", cache_key, exc)

        if cached_p and isinstance(cached_p, dict) and cached_p.get("symbol"):
            rows[sym] = _build_market_row(sym, mkt, cached_p, fallback_name)
        else:
            missing_items.append((sym, fallback_name, mkt))

    if missing_items:
        try:
            batch_results = batch_fetch(missing_items, lightweight=True, period="5d")
            if not isinstance(batch_results, list):
                batch_results = []
            by_symbol = {
                b_item["symbol"]: b_item
                for b_item in batch_results
                if isinstance(b_item, dict) and b_item.get("symbol")
            }

            for sym, fallback_name, mkt in missing_items:
                try:
                    b_item = by_symbol.get(sym)
                    if isinstance(b_item, dict) and b_item.get("symbol"):
                        rows[sym] = _build_market_row(sym, mkt, b_item, fallback_name)
                        continue

                    info = info_fetch(sym, cache_only=(sym != full_fetch_symbol)) or {}
                    rows[sym] = _build_market_row(
                        sym,
                        mkt,
                        {
                            **info,
                            "name": (
                                info.get("shortName")
                                or info.get("longName")
                                or info.get("displayName")
                                or PREDEFINED_NAMES.get(sym)
                                or fallback_name
                            ),
                        },
                        fallback_name,
                    )
                except Exception as sym_exc:
                    logger.debug("Failed enriching screener item for %s: %s", sym, sym_exc)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception(
                "Screener enrichment failed for %d missing symbols", len(missing_items)
            )

    return rows


def build_screener_base_rows(
    stocks_data: dict[str, list[dict[str, Any]]], market_filter: str
) -> list[dict[str, Any]]:
    """Convert active stock snapshot data into screener rows."""
    all_stocks: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()

    for mkt in ("us", "jp"):
        if market_filter != "all" and market_filter != mkt:
            continue
        for item in stocks_data.get(mkt, []):
            if not isinstance(item, dict) or not item.get("symbol"):
                continue
            sym = item["symbol"]
            if sym in seen_symbols:
                continue
            seen_symbols.add(sym)
            all_stocks.append(_build_market_row(sym, mkt, item, item.get("name") or sym))
    return all_stocks


def build_popular_symbol_items(
    market_filter: str,
    q: str,
    seen_symbols: set[str],
    pop_sources: list[tuple[str, list[str]]],
) -> list[tuple[str, str, str]]:
    """Collect popular symbols that should be enriched for the screener."""
    pop_unseen_items: list[tuple[str, str, str]] = []
    q_lower = q.strip().lower() if q else ""
    for mkt, pop_list in pop_sources:
        for sym in pop_list:
            if sym in seen_symbols:
                continue
            name = PREDEFINED_NAMES.get(sym, sym)
            sector = PREDEFINED_SECTORS.get(sym, "")
            if q_lower and (
                q_lower not in sym.lower()
                and q_lower not in name.lower()
                and q_lower not in sector.lower()
            ):
                continue
            seen_symbols.add(sym)
            pop_unseen_items.append((sym, name, mkt))

    return pop_unseen_items


def should_include_market(market_filter: str, market: str) -> bool:
    return market_filter == "all" or market_filter == market


def market_is_open_cached(market: str) -> bool:
    return is_market_open(market)
