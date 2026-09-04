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


def _build_market_row(
    symbol: str,
    market: str,
    source: dict[str, Any],
    fallback_name: str,
) -> dict[str, Any]:
    price = (
        normalize_optional_number(source.get("price"))
        or normalize_optional_number(source.get("close"))
        or normalize_optional_number(source.get("regularMarketPrice"))
        or normalize_optional_number(source.get("currentPrice"))
        or 0.0
    )
    shares_outstanding = normalize_optional_number(source.get("sharesOutstanding"))
    market_cap = (
        normalize_optional_number(source.get("market_cap"))
        or normalize_optional_number(source.get("marketCap"))
        or ((shares_outstanding * price) if shares_outstanding and price > 0 else 0.0)
    )
    volume = normalize_optional_number(source.get("volume")) or 0.0
    high = (
        normalize_optional_number(source.get("high"))
        or normalize_optional_number(source.get("regularMarketDayHigh"))
        or normalize_optional_number(source.get("dayHigh"))
        or price
    )
    low = (
        normalize_optional_number(source.get("low"))
        or normalize_optional_number(source.get("regularMarketDayLow"))
        or normalize_optional_number(source.get("dayLow"))
        or price
    )
    sector = source.get("sector") or PREDEFINED_SECTORS.get(symbol, "Other")
    pe_ratio = (
        normalize_optional_number(source.get("pe_ratio"))
        or normalize_optional_number(source.get("trailingPE"))
        or normalize_optional_number(source.get("pe"))
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

        price = (
            normalize_optional_number(item.get("price"))
            or normalize_optional_number(item.get("close"))
            or 0.0
        )
        volume = normalize_optional_number(item.get("volume")) or 0.0
        fallback_size = price * volume if volume > 0 else 0.0
        shares = normalize_optional_number(item.get("sharesOutstanding"))
        shares_cap = (shares * price) if (shares is not None) else fallback_size
        market_cap = (
            normalize_optional_number(item.get("market_cap"))
            or normalize_optional_number(item.get("marketCap"))
            or shares_cap
            or fallback_size
        )
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
