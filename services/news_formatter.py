"""
news_formatter.py - News text parsing and formatting utilities.

Extracted from app_state.py to reduce module complexity.
All functionality is exposed as module-level functions.
The NewsFormatter class is kept for backward compatibility.
"""

import json
import re


def _coerce_news_section_text(raw):
    if not raw:
        return ""
    return _flatten(raw)


def _coerce_news_section_text_v2(raw):
    """Enhanced version of coerce for news section text with better truncation handling."""
    if not raw:
        return ""

    # If it's a list or dict, flatten it normally
    if isinstance(raw, (list, dict)):
        return _coerce_news_section_text(raw)

    # If it's a string, it might be a truncated JSON fragment or a raw list of lines
    text = str(raw).strip()

    # If it looks like a truncated sentence at the end (no punctuation), try to clean it
    if text and text[-1] not in '。！？!?."}]':
        # Search for the last complete sentence or line
        last_punc = max(text.rfind("。"), text.rfind("？"), text.rfind("！"), text.rfind("\n"))
        if last_punc != -1:
            text = text[: last_punc + 1].strip()

    return _coerce_news_section_text(text)


def _flatten(item, current_depth=0, max_depth=5):
    if current_depth > max_depth:
        return str(item).strip()
    if item is None:
        return ""
    if isinstance(item, (int, float, bool)):
        return str(item)
    if isinstance(item, str):
        txt = item.strip()
        if not txt:
            return ""
        txt = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", txt)
        txt = re.sub(r"\s*```$", "", txt).strip()
        # JSON文字列なら再帰的にフラット化
        try:
            parsed_inner = json.loads(txt)
            return _flatten(parsed_inner, current_depth + 1, max_depth)
        except (json.JSONDecodeError, ValueError):
            # 末尾カンマ等を考慮した extract_json_payload のロジックの一部を適用
            if txt.startswith("{") or txt.startswith("["):
                fixed_txt = re.sub(r",\s*([\]}])", r"\1", txt)
                try:
                    return _flatten(json.loads(fixed_txt), current_depth + 1, max_depth)
                except (json.JSONDecodeError, ValueError):
                    pass

        # JSON風の行から値だけを抽出
        values = []
        for line in txt.splitlines():
            s = line.strip().rstrip(",")
            if not s or s in {"{", "}", "[", "]"}:
                continue
            m = re.match(
                r'^"(?:topic|summary|details|market_impact|title|description|text)"\s*:\s*"?(.*?)"?$',
                s,
            )
            if m:
                val = m.group(1).strip().strip('"')
                if val:
                    values.append(val)
                continue
            # キー名がない場合でも引用符で囲まれていれば抽出
            if s.startswith('"') and s.endswith('"'):
                values.append(s.strip('"'))
            else:
                values.append(s)
        if values:
            # 重複を落として可読化
            uniq = []
            for v in values:
                if v and v not in uniq and not _is_noise_news_line(v):
                    uniq.append(v)
            return "\n".join(uniq)
        return txt

    if isinstance(item, list):
        lines = []
        for x in item:
            t = _flatten(x, current_depth + 1, max_depth)
            if t:
                lines.extend([seg.strip() for seg in str(t).splitlines() if seg.strip()])
        uniq = []
        for v in lines:
            if v not in uniq:
                uniq.append(v)
        return "\n".join(uniq)

    if isinstance(item, dict):
        topic = str(item.get("topic") or item.get("title") or "").strip()
        summary = str(
            item.get("summary") or item.get("details") or item.get("description") or ""
        ).strip()
        impact = item.get("market_impact")
        parts = []
        if topic:
            parts.append(topic)
        if summary:
            parts.append(summary)
        if isinstance(impact, dict):
            impact_lines = []
            for k, v in impact.items():
                kv = f"{str(k).strip()}: {str(v).strip()}".strip()
                if kv and not kv.endswith(":"):
                    impact_lines.append(kv)
            if impact_lines:
                parts.append(" | ".join(impact_lines))
        elif impact:
            parts.append(str(impact).strip())

        if parts:
            return " - ".join([p for p in parts if p])

        misc = []
        for k, v in item.items():
            kv = f"{str(k).strip()}: {str(v).strip()}".strip()
            if kv and not kv.endswith(":"):
                misc.append(kv)
        return " | ".join(misc)

    return str(item).strip()


def _is_noise_news_line(line):
    s = str(line or "").strip()
    if not s:
        return True
    lower = s.lower()
    if lower.startswith("source:") or lower.startswith("date:") or lower.startswith("url:"):
        return True
    if "<a " in lower or "<li" in lower or "<ol" in lower or "<ul" in lower:
        return True
    if re.search(r"<[^>]+>", s):
        return True
    if lower.startswith("http://") or lower.startswith("https://"):
        return True
    if "news.google.com/rss/articles" in lower:
        return True
    # 日本語テキストは文字数ベースで判定（スペース区切りが少ないため語数チェックは不正確）
    has_cjk = bool(re.search(r"[\u3040-\u9fff]", s))
    if has_cjk:
        # CJK文字を含む場合：文字数が10文字以下はノイズ扱い
        if len(s) <= 10:
            return True
    else:
        word_count = len(re.findall(r"\S+", s))
        if word_count <= 5 and not re.search(r"[。！？!?.]", s):
            return True
    return False


def _parse_lines(text):
    lines = []
    for line in str(text or "").splitlines():
        s = re.sub(r"^\s*(?:[-*•▪]|\d+[.)])\s*", "", line).strip()
        s = s.strip("\"'")
        if s and not _is_noise_news_line(s):
            lines.append(s)
    return lines


def normalize_mistral_news_lines(section_text, max_lines=12):
    """Normalize and deduplicate Mistral news output lines."""
    out = []
    seen = set()

    def push_unique(line):
        t = str(line or "").strip()
        if not t:
            return
        if _is_noise_news_line(t):
            return
        key = t.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(t)

    for line in _parse_lines(section_text):
        push_unique(line)
    return "\n".join(out[:max_lines])


# ---------------------------------------------------------------------------
# Backward-compatible class wrapper
# ---------------------------------------------------------------------------
class NewsFormatter:
    """Legacy wrapper — delegates to module-level functions.

    This class exists only for backward compatibility.
    New code should import and use the module-level functions directly.
    """

    _coerce_news_section_text = staticmethod(_coerce_news_section_text)
    _coerce_news_section_text_v2 = staticmethod(_coerce_news_section_text_v2)
    _flatten = staticmethod(_flatten)
    _is_noise_news_line = staticmethod(_is_noise_news_line)
    _parse_lines = staticmethod(_parse_lines)
    _normalize_mistral_news_lines = staticmethod(normalize_mistral_news_lines)
