"""Built-in sample AI output for the keyless *preview mode*.

Preview mode lets a user explore the whole UI before entering a Mistral API
key.  ``services.ai_service`` short-circuits to this module (no network, no
rate-limit slot, no usage accounting) whenever the credential layer hands out
the preview placeholder key.

The helpers return the same shapes as the real Mistral code paths:
  * ``build_preview_chat_response`` -> normalized ``chat.complete`` dict
  * ``build_preview_stream_events`` -> ``stream_mistral_chat`` event dicts
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

PREVIEW_MODEL_NAME = "preview"
PREVIEW_NOTICE = "【プレビューモード】これはサンプル出力です。APIキーまたはAgent IDを設定すると実際のAI結果が表示されます。"

_STREAM_CHUNK_CHARS = 24


def _last_user_text(messages: Any) -> str:
    """Best-effort extraction of the latest user message text."""
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
        if role != "user":
            continue
        content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [
                str(p.get("text", ""))
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            return " ".join(parts).strip()
    return ""


def _sample_for_model(model_cls: type[BaseModel]) -> dict[str, Any] | None:
    """Return hand-written sample data for the app's known response schemas."""
    name = model_cls.__name__
    if name == "StockAnalysis":
        return {
            "recommendation": "中立",
            "sentiment": "中立",
            "target_price_3m": 0.0,
            "upside_3m": "±0%",
            "confidence": "低",
            "analysis_summary": f"{PREVIEW_NOTICE}",
            "key_catalysts": ["サンプル: 決算発表", "サンプル: 新製品・サービス"],
            "risk_factors": ["サンプル: 市場全体の変動"],
            "technical_analysis": "サンプル: テクニカル分析はプレビューでは生成されません。",
            "fundamental_analysis": "サンプル: ファンダメンタル分析はプレビューでは生成されません。",
            "latest_news_impact": "サンプル: ニュース影響はプレビューでは生成されません。",
        }
    if name == "NewsSummaryModel":
        return {
            "us": f"{PREVIEW_NOTICE}\n・米国市場のニュース要約がここに表示されます。",
            "jp": f"{PREVIEW_NOTICE}\n・日本市場のニュース要約がここに表示されます。",
            "trends": f"{PREVIEW_NOTICE}\n・トレンド情報の要約がここに表示されます。",
        }
    if name == "TechnicalLinesResult":
        return {
            "summary": PREVIEW_NOTICE,
            "trend_bias": "Neutral",
            "lines": [],
        }
    return None


def _generic_sample(model_cls: type[BaseModel]) -> dict[str, Any] | None:
    """Fallback: build a minimal valid instance from the model's fields."""
    sample: dict[str, Any] = {}
    for fname, field in model_cls.model_fields.items():
        if not field.is_required():
            continue
        ann = field.annotation
        if ann is str:
            sample[fname] = PREVIEW_NOTICE
        elif ann is bool:
            sample[fname] = False
        elif ann is int:
            sample[fname] = 0
        elif ann is float:
            sample[fname] = 0.0
        elif getattr(ann, "__origin__", None) is list:
            sample[fname] = []
        elif getattr(ann, "__origin__", None) is dict or ann is dict:
            sample[fname] = {}
        else:
            return None
    return sample


def _build_structured_sample(response_format: Any) -> dict[str, Any] | None:
    if not (isinstance(response_format, type) and issubclass(response_format, BaseModel)):
        return None
    for builder in (_sample_for_model, _generic_sample):
        try:
            candidate = builder(response_format)
            if candidate is None:
                continue
            return response_format.model_validate(candidate).model_dump()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.debug(
                "Preview sample for %s rejected type=%s",
                getattr(response_format, "__name__", "?"),
                type(exc).__name__,
            )
    return None


def build_preview_text(messages: Any = None) -> str:
    """Plain-text sample reply for free-form chat / analysis calls."""
    question = _last_user_text(messages)
    if question:
        snippet = question.replace("\n", " ")[:60]
        return f"{PREVIEW_NOTICE}\n\n（質問: {snippet}{'…' if len(question) > 60 else ''}）"
    return PREVIEW_NOTICE


def build_preview_chat_response(messages: Any = None, response_format: Any = None) -> dict[str, Any]:
    """Return a normalized chat-completion dict containing sample output."""
    structured = _build_structured_sample(response_format)
    message: dict[str, Any] = {"role": "assistant"}
    if structured is not None:
        message["content"] = structured
        message["parsed"] = structured
    else:
        text = build_preview_text(messages)
        if response_format:
            # json_object / json_schema dict formats still expect JSON text.
            import json

            text = json.dumps({"message": text}, ensure_ascii=False)
        message["content"] = text
    return {
        "id": "preview",
        "object": "chat.completion",
        "model": PREVIEW_MODEL_NAME,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "preview": True,
    }


def build_preview_stream_events(messages: Any = None) -> Iterator[dict[str, Any]]:
    """Yield ``delta`` events followed by ``done`` in ``stream_mistral_chat`` format."""
    text = build_preview_text(messages)
    for i in range(0, len(text), _STREAM_CHUNK_CHARS):
        yield {"type": "delta", "text": text[i : i + _STREAM_CHUNK_CHARS]}
    yield {"type": "done", "text": text}
