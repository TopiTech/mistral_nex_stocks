import copy
from typing import Any
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from flask import g, has_app_context
from pydantic import BaseModel

from constants import RequestsTimeout, CurlRequestsTimeout
from mistral_compat import SDKError

from app_helpers import _short_text, _token_fingerprint
from app_state import app_state
from config_utils import get_model_name
from constants import (
    MISTRAL_API_TIMEOUT_SEC,
    MISTRAL_MIN_INTERVAL_SEC,
    ANALYSIS_MAX_TOKENS_FALLBACK,
    REPAIR_NEWS_MAX_TOKENS,
)
from utils.validators import extract_chat_content, extract_json_payload

logger = logging.getLogger(__name__)

MISTRAL_BASE_URL = "https://api.mistral.ai/v1"


def repair_analysis_json_with_llm(api_key, raw_content):
    """Asks the LLM to fix a malformed analysis JSON string."""
    if app_state.is_circuit_open("mistral"):
        logger.warning("Mistral circuit is open; skipping LLM analysis repair.")
        return {}, ""

    repair_prompt = (
        "次のテキストを指定スキーマのJSONオブジェクトに変換してください。"
        "必須キー: recommendation,sentiment,target_price_3m,upside_3m,confidence,"
        "analysis_summary,key_catalysts,risk_factors,technical_analysis,fundamental_analysis,latest_news_impact\n"
        "入力テキスト:\n"
        f"{raw_content}"
    )
    try:
        response = call_mistral_chat(
            api_key,
            [
                {
                    "role": "system",
                    "content": "あなたは厳密なJSONフォーマッターです。必ず有効なJSONオブジェクトのみを返してください。"
                    "マークダウンコードブロックや追加のテキストを含めず、JSONのみを出力してください。",
                },
                {"role": "user", "content": repair_prompt},
            ],
            max_tokens=ANALYSIS_MAX_TOKENS_FALLBACK,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "analysis_repair",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "recommendation": {"type": "string"},
                            "sentiment": {"type": "string"},
                            "target_price_3m": {"type": "number"},
                            "upside_3m": {"type": "number"},
                            "confidence": {"type": "string"},
                            "analysis_summary": {"type": "string"},
                            "key_catalysts": {"type": "array", "items": {"type": "string"}},
                            "risk_factors": {"type": "array", "items": {"type": "string"}},
                            "technical_analysis": {"type": "string"},
                            "fundamental_analysis": {"type": "string"},
                            "latest_news_impact": {"type": "string"},
                        },
                        "required": [
                            "recommendation",
                            "sentiment",
                            "target_price_3m",
                            "upside_3m",
                            "confidence",
                            "analysis_summary",
                            "key_catalysts",
                            "risk_factors",
                            "technical_analysis",
                            "fundamental_analysis",
                            "latest_news_impact",
                        ],
                    },
                },
            },
            cache_key_override="repair_analysis_json_v1",
            reasoning_effort="none",
        )

        if isinstance(response, dict) and "error" in response:
            logger.warning("LLM analysis repair API returned error: %s", response["error"])
            return {}, ""

        repaired_content = extract_chat_content(response)
        repaired_json_str = extract_json_payload(repaired_content)
        if not repaired_json_str:
            return {}, repaired_content
        return json.loads(repaired_json_str), repaired_content
    except Exception as exc:
        logger.error("Failed to repair analysis JSON with LLM: %s", exc)
        return {}, ""


def repair_news_json_with_llm(api_key, raw_content):
    """Asks the LLM to fix a malformed news JSON string."""
    if app_state.is_circuit_open("mistral"):
        logger.warning("Mistral circuit is open; skipping LLM news repair.")
        return {"us": "", "jp": "", "trends": ""}, ""

    repair_prompt = (
        "次のテキストをニュース要約用のJSONオブジェクトに変換してください。"
        "必須キー: us,jp,trends\n"
        "各値は改行区切りの文字列。見出しの生引用/source/date/url/HTML/URL文字列は含めないこと。\n"
        "入力テキスト:\n"
        f"{raw_content}"
    )
    try:
        response = call_mistral_chat(
            api_key,
            [
                {
                    "role": "system",
                    "content": "あなたは厳密なJSONフォーマッターです。必ず有効なJSONオブジェクトのみを返してください。"
                    "マークダウンコードブロックや追加のテキストを含めず、JSONのみを出力してください。",
                },
                {"role": "user", "content": repair_prompt},
            ],
            max_tokens=REPAIR_NEWS_MAX_TOKENS,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "news_repair",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "us": {"type": "string"},
                            "jp": {"type": "string"},
                            "trends": {"type": "string"},
                        },
                        "required": ["us", "jp", "trends"],
                    },
                },
            },
            cache_key_override="repair_news_json_v1",
            reasoning_effort="none",
        )

        if isinstance(response, dict) and "error" in response:
            logger.warning("LLM news repair API returned error: %s", response["error"])
            return {"us": "", "jp": "", "trends": ""}, ""

        repaired_content = extract_chat_content(response)
        repaired_json_str = extract_json_payload(repaired_content)
        if not repaired_json_str:
            return {"us": "", "jp": "", "trends": ""}, repaired_content
        payload = json.loads(repaired_json_str)
        return {
            "us": str(payload.get("us") or ""),
            "jp": str(payload.get("jp") or ""),
            "trends": str(payload.get("trends") or ""),
        }, repaired_content
    except Exception as exc:
        logger.error("Failed to repair news JSON with LLM: %s", exc)
        return {"us": "", "jp": "", "trends": ""}, ""


def _get_mistral_model_name():
    """配置されたモデル名を取得し、最新モデル一覧に合わせて正規化する。"""
    from config_utils import MISTRAL_SUPPORTED_MODELS, MISTRAL_LEGACY_ALIASES
    configured_model = (get_model_name() or "").strip()

    if not configured_model:
        return "mistral-small-2603"

    if configured_model in MISTRAL_LEGACY_ALIASES:
        logger.info(
            "Configured Mistral model alias resolved: %s -> %s",
            configured_model,
            MISTRAL_LEGACY_ALIASES[configured_model],
        )
        return MISTRAL_LEGACY_ALIASES[configured_model]

    if configured_model in MISTRAL_SUPPORTED_MODELS:
        return configured_model

    logger.warning(
        "Unknown configured Mistral model: %s. Falling back to mistral-small-2603.",
        configured_model,
    )
    return "mistral-small-2603"


def _build_mistral_cache_key(
    model_name: str,
    msgs: list[object],
    token_limit: int,
    response_format_value,
    tools=None,
    tool_choice=None,
    reasoning_effort=None,
    cache_key_override=None,
) -> str:
    """キャッシュ用のユニークなキーを生成。"""

    # 2026仕様: msgs が Message オブジェクトのリストである可能性があるためシリアライズを調整
    serializable_msgs = []
    for m in msgs:
        if hasattr(m, "model_dump"):
            serializable_msgs.append(m.model_dump())
        else:
            serializable_msgs.append(m)

    # response_format_value が Pydantic クラスである場合
    serializable_fmt = response_format_value
    if isinstance(response_format_value, type) and issubclass(
        response_format_value, BaseModel
    ):
        # クラス名だけでなく完全修飾名を使い、異なるモジュールの同名クラスでも衝突を防止
        try:
            serializable_fmt = f"{response_format_value.__module__}.{response_format_value.__qualname__}"
        except AttributeError:
            serializable_fmt = response_format_value.__name__

    payload = json.dumps(
        {
            "model": model_name,
            "messages": serializable_msgs,
            "max_tokens": token_limit,
            "response_format": serializable_fmt,
            "tools": tools,
            "tool_choice": tool_choice,
            "reasoning_effort": reasoning_effort,
            "cache_key_override": cache_key_override,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    digest = hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()
    return f"mistral_chat_{digest}"



def _is_mistral_capacity_error(err_payload):
    """429や容量制限エラーかどうかを判定。"""
    err = (err_payload or {}).get("error", {})
    if not isinstance(err, dict):
        return False
    return (
        err.get("type") == "service_tier_capacity_exceeded"
        or str(err.get("code")) == "3505"
        or int(err.get("status_code") or 0) == 429
    )


def _extract_mistral_wait_seconds(response) -> float:
    """レスポンスヘッダから待機秒数を抽出。"""
    headers = getattr(response, "headers", {}) or {}
    waits = []

    def _parse_sec(value):
        text = str(value or "").strip()
        if not text:
            return 0.0
        try:
            return max(0.0, float(text))
        except (ValueError, TypeError):
            pass
        lower = text.lower()
        if lower.endswith("ms"):
            try:
                return max(0.0, float(lower[:-2].strip()) / 1000.0)
            except (ValueError, TypeError):
                return 0.0
        try:
            dt = parsedate_to_datetime(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, AttributeError):
            return 0.0

    waits.append(_parse_sec(headers.get("Retry-After")))
    waits.append(_parse_sec(headers.get("retry-after")))

    for key in ["X-RateLimit-Reset", "x-ratelimit-reset", "x-ratelimit-reset-requests"]:
        raw = headers.get(key)
        if raw:
            try:
                epoch = float(str(raw).strip())
                waits.append(max(0.0, epoch - time.time()))
            except (ValueError, TypeError):
                waits.append(_parse_sec(raw))

    return max((w for w in waits if w and w > 0.0), default=0.0)


def _get_mistral_client(api_key: str):
    """Mistral SDK クライアントを取得（キャッシュから、または新規作成）"""
    if not api_key:
        return None
    return app_state.ai.get_or_create_mistral_client(api_key)


def call_mistral_chat(
    api_key: str,
    messages: list[Any],
    max_tokens: int = 600,
    use_cache: bool = True,
    response_format=None,
    tools=None,
    tool_choice=None,
    cache_key_override=None,
    reasoning_effort=None,
):
    """Mistral公式SDKを使用した Chat Completions 呼び出し (SDK v2 chat.parse 対応版)"""
    model = _get_mistral_model_name()
    token_limit = max(64, min(max_tokens or 600, 2000))
    min_interval_sec = MISTRAL_MIN_INTERVAL_SEC

    cache_key = (
        _build_mistral_cache_key(
            model,
            messages,
            token_limit,
            response_format,
            tools,
            tool_choice,
            reasoning_effort,
            cache_key_override,
        )
        if use_cache
        else None
    )
    if use_cache:
        with app_state.ai.mistral_response_lock:
            cached = app_state.ai.mistral_response_cache.get(cache_key)
        if cached is not None:
            return copy.deepcopy(cached)

    client = _get_mistral_client(api_key)
    if client is None:
        return {"error": {"message": "Mistral API key is missing or invalid"}}

    # Reasoning effort (supported by latest models: large-2512, medium-2604).
    # Resolution order: explicit arg > global override env var > per-model default.
    effective_reasoning = reasoning_effort
    if effective_reasoning is None:
        env_default = os.environ.get("MNS_MISTRAL_REASONING_EFFORT", "").strip().lower()
        if env_default in ("low", "medium", "high", "none"):
            effective_reasoning = env_default
        elif env_default:
            logger.warning(
                "Invalid MNS_MISTRAL_REASONING_EFFORT=%r; expected low|medium|high|none. Falling back to per-model default.",
                env_default,
            )
    if effective_reasoning is None:
        if model in ("mistral-large-2512", "mistral-large-3", "mistral-large-latest"):
            effective_reasoning = "medium"
        elif model in ("mistral-medium-2604", "mistral-medium-3.5", "mistral-medium-3-5"):
            effective_reasoning = "high"
        else:
            effective_reasoning = "none"

    try:
        with app_state.ai.mistral_cooldown_lock:
            now_ts = time.time()
            wait_before = max(
                app_state.ai.mistral_next_allowed_ts - now_ts,
                (app_state.ai.mistral_last_call_ts + min_interval_sec) - now_ts,
                0.0,
            )
            app_state.ai.mistral_last_call_ts = now_ts + wait_before

        if wait_before > 0:
            app_state.execution.shutdown_event.wait(wait_before)

        with app_state.ai.mistral_call_semaphore:
            if app_state.is_circuit_open("mistral"):
                logger.warning("Mistral circuit is OPEN. Skipping API call.")
                return {
                    "error": {
                        "message": "AI service is temporarily unavailable (circuit open)",
                        "status_code": 503,
                    }
                }

            req_id = "-"
            try:
                if has_app_context():
                    req_id = getattr(g, "request_id", "-")
            except Exception:
                pass

            logger.info(
                "Mistral SDK call start id=%s model=%s reasoning=%s key=%s",
                req_id,
                model,
                effective_reasoning,
                _token_fingerprint(api_key),
            )

            kwargs = {
                "model": model,
                "messages": messages,
                "max_tokens": token_limit,
                "timeout_ms": int(MISTRAL_API_TIMEOUT_SEC * 1000),
                "reasoning_effort": effective_reasoning,
            }
            if tools:
                kwargs["tools"] = tools
            if tool_choice:
                kwargs["tool_choice"] = tool_choice

            # Structured Outputs: Pydanticモデルが渡された場合は chat.parse を使用
            if isinstance(response_format, type) and issubclass(
                response_format, BaseModel
            ):
                response = client.chat.parse(
                    **kwargs,
                    response_format=response_format,
                )
            else:
                if response_format:
                    kwargs["response_format"] = response_format
                response = client.chat.complete(**kwargs)

            # 成功報告
            app_state.report_circuit_result("mistral", success=True)
            app_state.ai.reset_mistral_streak()

            with app_state.ai.mistral_cooldown_lock:
                app_state.ai.mistral_last_call_ts = time.time()

            # レスポンスの辞書化
            if hasattr(response, "model_dump"):
                data = response.model_dump()
            else:
                data = {"choices": []}

            # chat.parse を使用した場合、content にパース済みオブジェクト(dict)を格納
            if isinstance(response_format, type) and issubclass(
                response_format, BaseModel
            ):
                try:
                    choice = response.choices[0]
                    # SDK v2: choice.message.parsed にパース済みモデルが入る
                    parsed_obj = getattr(choice.message, "parsed", None)
                    if parsed_obj:
                        data["choices"][0]["message"]["content"] = (
                            parsed_obj.model_dump()
                        )
                except (AttributeError, IndexError):
                    pass

            if use_cache and data.get("choices"):
                with app_state.ai.mistral_response_lock:
                    app_state.ai.mistral_response_cache[cache_key] = copy.deepcopy(data)
            return data

    except (SDKError, RequestsTimeout, CurlRequestsTimeout, ConnectionError, OSError) as exc:
        logger.warning("Mistral SDK call failed: %s", _short_text(str(exc), 240))
        status_code = getattr(exc, "status_code", 0)
        response_obj = getattr(exc, "response", None)
        retry_after_sec = _extract_mistral_wait_seconds(response_obj)

        # サーキットへの報告 (429はレート制限なので別途管理されるが、5xxやタイムアウトはサーキット対象)
        if (
            isinstance(exc, (RequestsTimeout, CurlRequestsTimeout, ConnectionError))
            or status_code >= 500
        ):
            app_state.report_circuit_result(
                "mistral", success=False, threshold=3, open_sec=60
            )

        if status_code == 429:
            backoff = app_state.ai.mark_mistral_429(retry_after_sec)
            logger.warning("Mistral 429 backoff applied: %.2fs", backoff)
        return {
            "error": {
                "message": str(exc),
                "status_code": status_code,
            }
        }
