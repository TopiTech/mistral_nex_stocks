"""Shared constants and JS/HTML response parsers for Nikkei225JP scrapers."""

from __future__ import annotations

import re

NIKKEI225JP_BASE_URL = "https://nikkei225jp.com/"
NIKKEI225JP_ADR_URL = "https://nikkei225jp.com/adr/adr.php"
NIKKEI225JP_ADR_ALL_URL = "https://nikkei225jp.com/_data/_nfsDATA/adr/_adr_all.js"
NIKKEI225JP_INDEX_MID_URL = "https://nikkei225jp.com/_data/_nfsDATA/ajaxindex/ajax_TOP_mid.js"
NIKKEI225JP_INDEX_BTM_URL = "https://nikkei225jp.com/_data/_nfsDATA/ajaxindex/ajax_TOP_btm.js"
NIKKEI225JP_INDEX_NDY_URL = "https://nikkei225jp.com/_data/_nfsDATA/ajaxindex/ajax_NDY_min.js"

NIKKEI225JP_INDEX_MAP: dict[str, int] = {
    "^N225": 111,
    "N225": 111,
    "^DJI": 211,
    "DJI": 211,
    "^IXIC": 212,
    "NASDAQ": 212,
    "^GSPC": 213,
    "SP500": 213,
    "^NDX": 214,
    "NASDAQ100": 214,
    "USDJPY=X": 511,
    "USDJPY": 511,
    "JPY=X": 511,
    "EURJPY=X": 514,
    "EURJPY": 514,
    "EURUSD=X": 523,
    "EURUSD": 523,
    "^VIX": 621,
    "VIX": 621,
    "BTC-USD": 1001,
    "BTC": 1001,
}

_ADR_LINE_RE = re.compile(r'A0\[\w+\]="([^"]+)"')
_AJAX_INDEX_LINE_RE = re.compile(r'A\[(\d+)\]="([^"]+)"')
_NDY_MIN_RE = re.compile(r"var NDY(\d+)V=([\d.]+),NDY\1Z=([+-]?[\d.]+);")
_DIRECT_A0_RE = re.compile(r'var\s+A0\s*=\s*"([^"]+)"')


def parse_adr_all_js(text: str) -> dict[str, list[str]]:
    """Parse ``_adr_all.js`` ``A0[...]="..."`` rows keyed by ticker code."""
    cache: dict[str, list[str]] = {}
    for line in text.splitlines():
        if line.startswith("A0["):
            match = _ADR_LINE_RE.search(line)
            if match:
                parts = match.group(1).split("_")
                if len(parts) >= 21:
                    cache[parts[0]] = parts
    return cache


def parse_ajax_index_js(text: str, cache: dict[int, list[str]]) -> None:
    """Populate *cache* with ``A[<id>]="..."`` index entries from JS payload."""
    for line in text.splitlines():
        match = _AJAX_INDEX_LINE_RE.search(line)
        if match:
            code = int(match.group(1))
            parts = match.group(2).split("_")
            if len(parts) >= 3:
                cache[code] = parts


def parse_ndy_min_js(text: str, cache: dict[int, list[str]]) -> None:
    """Populate missing index entries in *cache* from ``ajax_NDY_min.js``."""
    for match in _NDY_MIN_RE.finditer(text):
        code = int(match.group(1))
        if code not in cache:
            cache[code] = [match.group(2), match.group(3), "0", "", ""]


def extract_direct_adr_parts(body_html: str) -> list[str] | None:
    """Extract underscored ``var A0 = "..."`` row fields from ``adr.php`` HTML."""
    match = _DIRECT_A0_RE.search(body_html)
    return match.group(1).split("_") if match else None
