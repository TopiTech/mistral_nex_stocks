"""Regression coverage for fullscreen-chart modal lifecycle behavior."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_fullscreen_chart_acquires_one_scroll_lock_per_open() -> None:
    """Closing a fullscreen chart must fully restore page scrolling.

    ``closeFsChartModal()`` releases one modal scroll lock, so the open path
    may acquire exactly one. Two acquisitions leave ``body`` permanently
    scroll-locked after the modal closes.
    """
    source = (ROOT / "static" / "js" / "ui.js").read_text(encoding="utf-8")
    start = source.index("function openFullscreenChart(")
    end = source.index("\nfunction closeFsChartModal()", start)
    open_function = source[start:end]

    assert open_function.count("lockBodyScroll();") == 1
