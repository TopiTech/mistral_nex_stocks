"""Rendered-markup contract regressions for every page route.

Pins the template / route / page-JS boundaries verified in the 2026-11-08
markup pass: CSP nonces, CSRF metadata, unique IDs, resolvable ARIA and
label references, balanced containers, static assets, and Jinja StrictUndefined
rendering.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

PAGES = [
    "/",
    "/setup",
    "/main",
    "/heatmap",
    "/screener",
    "/settings",
    "/experimental/orbit",
]

VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
OPTIONAL = {"p", "li", "td", "th", "tr", "option", "thead", "tbody"}
STRICT = {
    "div",
    "section",
    "main",
    "aside",
    "header",
    "footer",
    "nav",
    "form",
    "button",
    "label",
    "table",
    "ul",
    "ol",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "a",
    "textarea",
    "select",
    "span",
}


class Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.nonces: list[str] = []
        self.for_refs: list[str] = []
        self.aria_refs: list[tuple[str, str]] = []
        self.skip_targets: list[str] = []
        self.stack: list[tuple[str, int]] = []
        self.balance_errors: list[str] = []
        self.duplicate_attrs: list[tuple[int, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = [name for name, _ in attrs]
        for name in {n for n in names if names.count(n) > 1}:
            self.duplicate_attrs.append((self.getpos()[0], tag, name))
        d = dict(attrs)
        if d.get("id"):
            self.ids.append(d["id"] or "")
        if "nonce" in d:
            self.nonces.append(d.get("nonce") or "")
        if tag == "label" and d.get("for"):
            self.for_refs.append(d["for"] or "")
        for key in ("aria-controls", "aria-labelledby", "aria-describedby"):
            if d.get(key):
                for value in (d[key] or "").split():
                    self.aria_refs.append((key, value))
        if tag == "a" and (d.get("href") or "").startswith("#"):
            self.skip_targets.append((d.get("href") or "")[1:])
        if tag in VOID or tag in OPTIONAL:
            return
        if tag in STRICT:
            self.stack.append((tag, self.getpos()[0]))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID or tag in OPTIONAL:
            return
        if tag not in STRICT:
            return
        if not self.stack:
            self.balance_errors.append(f"line {self.getpos()[0]}: </{tag}> without open")
            return
        open_tag, line = self.stack.pop()
        if open_tag != tag:
            self.balance_errors.append(
                f"line {self.getpos()[0]}: </{tag}> closes <{open_tag}> from line {line}"
            )


def _nonce_from_csp(header: str) -> str:
    m = re.search(r"'nonce-([^']+)'", header)
    return m.group(1) if m else ""


@pytest.mark.parametrize("path", PAGES)
def test_rendered_markup_contract(client, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200, path
    body = response.get_data(as_text=True)
    collector = Collector()
    collector.feed(body)

    # 1. CSP nonce must be the same value the header authorises.
    csp = response.headers.get("Content-Security-Policy", "")
    header_nonce = _nonce_from_csp(csp)
    assert header_nonce, (path, csp)
    assert collector.nonces, path
    assert set(collector.nonces) == {header_nonce}, (
        path,
        sorted(set(collector.nonces))[:3],
        header_nonce,
    )

    # 2. CSRF meta must carry a usable token.
    csrf = re.search(r'name="csrf-token" content="([^"]*)"', body)
    assert csrf and len(csrf.group(1)) >= 20, (path, csrf.group(1) if csrf else None)

    # 3. IDs must be unique.
    duplicates = sorted({i for i in collector.ids if collector.ids.count(i) > 1})
    assert not duplicates, (path, duplicates)

    # 4. Internal references must resolve.
    ids = set(collector.ids)
    broken_for = sorted({v for v in collector.for_refs if v not in ids})
    broken_aria = sorted({f"{k}={v}" for k, v in collector.aria_refs if v not in ids})
    broken_skip = sorted({v for v in collector.skip_targets if v not in ids and v})
    assert not broken_for, (path, broken_for)
    assert not broken_aria, (path, broken_aria)
    assert not broken_skip, (path, broken_skip)

    # 5. Strict container tags must be balanced and attributes unique.
    assert not collector.balance_errors, (path, collector.balance_errors)
    assert not collector.stack, (path, collector.stack[-5:])
    assert not collector.duplicate_attrs, (path, collector.duplicate_attrs)

    # 6. Every referenced local static asset must exist.
    assets = sorted(
        {
            m.split("?", 1)[0]
            for m in re.findall(r'(?:src|href)="(/static/[^"]+)"', body)
        }
    )
    for asset in assets:
        asset_response = client.get(asset)
        try:
            assert asset_response.status_code == 200, (path, asset)
        finally:
            asset_response.close()


def test_heatmap_3d_failure_message_has_visible_container(client) -> None:
    """The 3D failure message must land in the on-screen toggle group.

    heatmap.js appends its failure box to #view-mode-toggle-container and only
    falls back to document.body when that container is absent.  The fallback
    puts the message after the footer, so a missing ID hides the feedback the
    user needs when WebGL/Three.js cannot start.
    """
    body = client.get("/heatmap").get_data(as_text=True)
    assert body.count('id="view-mode-toggle-container"') == 1
    assert re.search(
        r'<div\b[^>]*\bid="view-mode-toggle-container"[^>]*\bclass="view-toggle"',
        body,
    )


@pytest.mark.parametrize("path", PAGES)
def test_rendered_with_strict_undefined(client, path: str) -> None:
    from jinja2 import StrictUndefined

    from app import app

    original = app.jinja_env.undefined
    app.jinja_env.undefined = StrictUndefined
    try:
        response = client.get(path)
    finally:
        app.jinja_env.undefined = original
    assert response.status_code == 200, (path, response.get_data(as_text=True)[:2000])
