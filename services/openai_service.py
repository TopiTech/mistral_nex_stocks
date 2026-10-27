"""
services/openai_service.py - OpenAI-compatible API LLM Provider (Beta).

Provides support for standard OpenAI REST API endpoints (/v1/chat/completions,
/v1/models, /v1/embeddings) for compatibility with OpenAI official, Ollama,
Groq, DeepSeek, OpenRouter, vLLM, LM Studio, etc.

Note:
  This is a BETA feature. The application is primarily optimized for Mistral AI API.
  Prompts and tool definitions are tuned for Mistral, though this compatibility
  layer ensures all AI features work reliably across compliant OpenAI endpoints.
"""

import copy
import hashlib
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from flask import current_app, g, has_app_context
from pydantic import BaseModel

from app_state import app_state
from constants import (
    MISTRAL_API_TIMEOUT_SEC,
    MISTRAL_MAX_TOKENS_CEIL,
)
from credential_manager import (
    get_openai_base_url,
    get_openai_embeddings_model,
    get_openai_model,
    is_preview_api_key,
)
from services.ai_tools import MISTRAL_FINANCIAL_TOOLS, execute_mistral_tool_call
from services.preview_responses import build_preview_chat_response, build_preview_stream_events
from utils.validators import extract_chat_content, extract_json_payload

logger = logging.getLogger(__name__)


def _openai_response_has_content(data: Any) -> bool:
    """Return True when an OpenAI-compatible response contains usable content.

    Mirrors ``services.ai_service._response_has_content`` (A-1): empty
    responses must NOT be cached, otherwise a retry of the same request would
    hit the cache and replay the same empty result until the TTL expires.
    Defined locally because importing ai_service here would be circular
    (ai_service dispatches to this module).
    """
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return False
    content = message.get("content")
    if isinstance(content, dict):
        return bool(content)
    return bool(content and str(content).strip())


def _public_openai_error_message(exc: BaseException, status_code: int = 0) -> str:
    """Return a clean, actionable user error message for OpenAI-compatible API calls."""
    if isinstance(exc, httpx.TimeoutException):
        return (
            "OpenAI互換APIがタイムアウトしました。サーバーの稼働状況を確認して再試行してください。"
        )
    if status_code == 401:
        return "OpenAI互換APIの認証に失敗しました。APIキーを確認してください。"
    if status_code == 403:
        return "OpenAI互換APIへのアクセスが拒否されました。権限とモデル設定を確認してください。"
    if status_code == 404:
        return "OpenAI互換APIのエンドポイントまたは指定されたモデルが見つかりません。Base URLとモデル名を確認してください。"
    if status_code == 429:
        return "OpenAI互換APIの利用上限またはレート制限に達しました。しばらく待ってから再試行してください。"
    if status_code >= 500:
        return f"OpenAI互換APIサーバーでエラーが発生しました (HTTP {status_code})。サーバーログを確認してください。"
    if isinstance(exc, (httpx.NetworkError, ConnectionError, OSError)):
        return "OpenAI互換APIサーバーに接続できませんでした。Base URL（ホスト名・ポート）を確認してください。"
    return "OpenAI互換APIの呼び出しに失敗しました。設定と接続を確認してください。"


def _is_openai_reasoning_model(model_name: str | None) -> bool:
    """Return True if model is an OpenAI or compatible reasoning model (e.g. o1, o3, DeepSeek-R1)."""
    if not model_name or not isinstance(model_name, str):
        return False
    name = model_name.strip().lower()
    return (
        name.startswith(("o1", "o3", "deepseek-reasoner"))
        or "reasoning" in name
        or "-r1" in name
        or name.endswith("-r1")
    )


def _create_httpx_client(**kwargs) -> httpx.Client:
    """Create an httpx.Client configured with OS trust store certs on Windows."""
    import ssl

    if "verify" not in kwargs:
        try:
            ctx = ssl.create_default_context()
            ctx.load_default_certs()
            kwargs["verify"] = ctx
        except Exception as exc:
            logger.debug("Failed configuring OS trust store certs: %s", exc)
    return httpx.Client(**kwargs)


def _get_openai_client(base_url: str, api_key: str | None = None) -> httpx.Client:
    """Retrieve persistent pooled httpx.Client from app_state or fallback to ephemeral."""
    try:
        return app_state.ai.get_or_create_openai_client(base_url, api_key)
    except Exception as exc:
        logger.debug("Failed getting pooled OpenAI client; using ephemeral client: %s", exc)
        return _create_httpx_client(timeout=MISTRAL_API_TIMEOUT_SEC, follow_redirects=True)


def _build_openai_headers(api_key: str | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "MistralNeXStocks/3.0 (OpenAI-Compat-Beta)",
        "HTTP-Referer": "http://localhost:5000",
        "X-Title": "MistralNeXStocks",
    }
    if api_key and api_key.strip() and not is_preview_api_key(api_key):
        clean_key = api_key.strip()
        if clean_key != "ollama-local":
            headers["Authorization"] = f"Bearer {clean_key}"
    return headers


def verify_openai_connection(
    base_url: str | None = None,
    api_key: str | None = None,
    timeout_sec: float = 12.0,
) -> dict[str, Any]:
    """Verify connectivity with OpenAI-compatible endpoint and query available models."""
    target_url = (base_url or get_openai_base_url()).strip().rstrip("/")
    models_endpoint = f"{target_url}/models"
    headers = _build_openai_headers(api_key)

    start_ts = time.time()
    try:
        with _create_httpx_client(timeout=timeout_sec, follow_redirects=True) as client:
            resp = client.get(models_endpoint, headers=headers)
            latency_ms = int((time.time() - start_ts) * 1000)

            if resp.status_code == 401:
                return {
                    "ok": False,
                    "valid": False,
                    "error": "認証エラー: APIキーが無効または未設定です (HTTP 401)",
                    "latency_ms": latency_ms,
                }
            if resp.status_code == 404:
                return {
                    "ok": False,
                    "valid": False,
                    "error": f"エンドポイントが見つかりません (HTTP 404: {models_endpoint})",
                    "latency_ms": latency_ms,
                }
            if resp.status_code >= 400:
                err_text = ""
                try:
                    body = resp.json()
                    err_text = (
                        body.get("error", {}).get("message", "") if isinstance(body, dict) else ""
                    )
                except Exception:
                    pass
                return {
                    "ok": False,
                    "valid": False,
                    "error": err_text or f"接続テスト失敗 (HTTP {resp.status_code})",
                    "latency_ms": latency_ms,
                }

            # OpenRouter provides public unauthenticated access to /models.
            # If the target is OpenRouter, verify the API key with /auth/key.
            if "openrouter.ai" in target_url:
                if not api_key or not api_key.strip():
                    return {
                        "ok": False,
                        "valid": False,
                        "error": "OpenRouterを利用するには有効なAPIキーが必要です (未入力)。",
                        "latency_ms": latency_ms,
                    }
                auth_resp = client.get(f"{target_url}/auth/key", headers=headers)
                if auth_resp.status_code == 401:
                    auth_err = "認証エラー: OpenRouterのAPIキーが無効です (HTTP 401)"
                    try:
                        auth_data = auth_resp.json()
                        msg = auth_data.get("error", {}).get("message")
                        if msg:
                            auth_err = f"OpenRouter認証エラー: {msg} (HTTP 401)"
                    except Exception:
                        pass
                    if not api_key.strip().startswith("sk-or-"):
                        auth_err += "。OpenRouterのキーは通常 'sk-or-v1-' で始まります。キーと接続先Base URLを確認してください。"
                    return {
                        "ok": False,
                        "valid": False,
                        "error": auth_err,
                        "latency_ms": latency_ms,
                    }
                if auth_resp.status_code >= 400:
                    return {
                        "ok": False,
                        "valid": False,
                        "error": f"OpenRouter認証確認失敗 (HTTP {auth_resp.status_code})",
                        "latency_ms": latency_ms,
                    }

            data = resp.json()
            raw_models = data.get("data", []) if isinstance(data, dict) else []
            model_ids: list[str] = []
            for item in raw_models:
                if isinstance(item, dict) and "id" in item:
                    model_ids.append(str(item["id"]))
                elif isinstance(item, str):
                    model_ids.append(item)

            model_ids = sorted(set(model_ids))
            configured_model = get_openai_model()
            has_configured = configured_model in model_ids if model_ids else True

            return {
                "ok": True,
                "valid": True,
                "provider": "openai_compatible",
                "tier_name": "OpenAI互換 (Beta)",
                "base_url": target_url,
                "model_count": len(model_ids),
                "models": model_ids[:100],  # cap for display
                "configured_model": configured_model,
                "configured_model_available": has_configured,
                "latency_ms": latency_ms,
                "message": f"接続成功 (応答: {latency_ms}ms, モデル数: {len(model_ids)}件)",
            }
    except Exception as exc:
        latency_ms = int((time.time() - start_ts) * 1000)
        logger.warning(
            "OpenAI verification failed url=%s error_type=%s: %s",
            models_endpoint,
            type(exc).__name__,
            exc,
        )
        return {
            "ok": False,
            "valid": False,
            "error": _public_openai_error_message(exc),
            "latency_ms": latency_ms,
        }


def _build_openai_cache_key(
    model: str,
    messages: list[Any],
    max_tokens: int,
    temperature: float | None,
    response_format: Any,
    tools: Any,
    tool_choice: Any = None,
    reasoning_effort: str | None = None,
    cache_key_override: str | None = None,
    base_url: str | None = None,
    credential_scope: str | None = None,
) -> str:
    serializable_msgs: list[Any] = []
    if messages:
        for m in messages:
            if hasattr(m, "model_dump"):
                serializable_msgs.append(m.model_dump())
            elif hasattr(m, "dict") and callable(m.dict):
                serializable_msgs.append(m.dict())
            else:
                serializable_msgs.append(m)

    serializable_tools: Any = None
    if tools:
        if isinstance(tools, list):
            serializable_tools = []
            for t in tools:
                if hasattr(t, "model_dump"):
                    serializable_tools.append(t.model_dump())
                elif hasattr(t, "dict") and callable(t.dict):
                    serializable_tools.append(t.dict())
                else:
                    serializable_tools.append(t)
        elif hasattr(tools, "model_dump"):
            serializable_tools = tools.model_dump()
        elif hasattr(tools, "dict") and callable(tools.dict):
            serializable_tools = tools.dict()
        else:
            serializable_tools = tools

    serializable_fmt = response_format
    if isinstance(response_format, type) and issubclass(response_format, BaseModel):
        try:
            serializable_fmt = f"{response_format.__module__}.{response_format.__qualname__}"
        except AttributeError:
            serializable_fmt = response_format.__name__
    elif response_format is not None:
        serializable_fmt = str(response_format)

    raw_key = {
        "provider": "openai_compatible",
        "base_url": base_url or get_openai_base_url(),
        "model": model,
        "messages": serializable_msgs,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "response_format": serializable_fmt,
        "tools": serializable_tools,
        "tool_choice": str(tool_choice) if tool_choice else None,
        "reasoning_effort": str(reasoning_effort) if reasoning_effort else None,
        "cache_key_override": cache_key_override,
        "credential_scope": credential_scope,
    }
    dumped = json.dumps(raw_key, sort_keys=True, ensure_ascii=False, default=str)
    return f"openai_{hashlib.sha256(dumped.encode('utf-8')).hexdigest()}"


def call_openai_chat(
    api_key: str,
    messages: list[Any],
    max_tokens: int = 600,
    use_cache: bool = True,
    response_format: Any = None,
    tools: Any = None,
    tool_choice: Any = None,
    cache_key_override: str | None = None,
    reasoning_effort: str | None = None,
    temperature: float | None = None,
    _model_override: str | None = None,
    _is_fallback: bool = False,
) -> dict[str, Any]:
    """Execute Chat Completion using OpenAI-compatible REST API (Beta)."""
    if is_preview_api_key(api_key):
        return build_preview_chat_response(messages, response_format)

    base_url = get_openai_base_url()
    model = _model_override if _model_override else get_openai_model()
    token_limit = min(max(16, max_tokens), MISTRAL_MAX_TOKENS_CEIL)

    # Response format handling
    requires_pydantic_parse = False
    pydantic_cls = None
    if isinstance(response_format, type) and issubclass(response_format, BaseModel):
        requires_pydantic_parse = True
        pydantic_cls = response_format
        token_limit = min(max(3000, token_limit), MISTRAL_MAX_TOKENS_CEIL)

    cred_scope = (
        hashlib.sha256(api_key.strip().encode("utf-8")).hexdigest()[:16]
        if api_key and api_key.strip()
        else None
    )

    cache_key = _build_openai_cache_key(
        model,
        messages,
        token_limit,
        temperature,
        response_format,
        tools,
        tool_choice=tool_choice,
        reasoning_effort=reasoning_effort,
        cache_key_override=cache_key_override,
        base_url=base_url,
        credential_scope=cred_scope,
    )
    if use_cache:
        with app_state.ai.mistral_response_lock:
            cached = app_state.ai.mistral_response_cache.get(cache_key)
        if cached is not None:
            return copy.deepcopy(cached)

    if app_state.market.is_circuit_open("openai_compatible"):
        logger.warning("OpenAI-compatible circuit is open. Skipping API call.")
        return {
            "error": {
                "message": "AIサービス（OpenAI互換）は一時的に利用できません（サーキットブレーカー発動中）",
                "status_code": 503,
                "provider": "openai_compatible",
            }
        }

    circuit_probe_claimed = False
    circuit_state = app_state.market.get_circuit_state("openai_compatible")
    if circuit_state.get("status") == "HALF_OPEN":
        if not app_state.market.try_claim_circuit_probe("openai_compatible"):
            logger.warning(
                "OpenAI-compatible circuit recovery probe already in progress. Skipping API call."
            )
            return {
                "error": {
                    "message": "AIサービス（OpenAI互換）は一時的に利用できません（サーキット回復プローブ実行中）",
                    "status_code": 503,
                    "provider": "openai_compatible",
                }
            }
        circuit_probe_claimed = True

    endpoint = f"{base_url}/chat/completions"
    headers = _build_openai_headers(api_key)

    effective_messages = [copy.deepcopy(m) if isinstance(m, dict) else m for m in messages]
    if requires_pydantic_parse and pydantic_cls is not None:
        schema_dict = pydantic_cls.model_json_schema()
        schema_instruction = (
            "\n\n【必須出力形式 (JSON)】\n"
            "必ず以下のJSONスキーマに厳密に準拠した有効な単一のJSONオブジェクト（json形式）のみを出力してください。\n"
            "マークダウンコードブロックや前置き・解説テキストは一切含めず、純粋なJSON文字列のみを出力してください。\n"
            "【重要：推奨・センチメント・確信度・目標価格キーの指定値】\n"
            '- recommendation: "強い買い" | "買い" | "中立" | "売り" | "強い売り"\n'
            '- sentiment: "強気" | "中立" | "弱気"\n'
            '- confidence: "高" | "中" | "低"\n'
            "- target_price_3m: 数値 (例: 150.0)\n"
            '- upside_3m: 文字列 (例: "+10%")\n'
            f"スキーマ定義:\n{json.dumps(schema_dict, ensure_ascii=False)}"
        )
        has_system = False
        new_msgs = []
        for m in effective_messages:
            if isinstance(m, dict) and m.get("role") == "system" and not has_system:
                new_m = dict(m)
                new_m["content"] = str(new_m.get("content", "")) + schema_instruction
                new_msgs.append(new_m)
                has_system = True
            else:
                new_msgs.append(m)
        if not has_system:
            new_msgs.insert(
                0,
                {
                    "role": "system",
                    "content": "あなたは厳密なJSON出力アシスタントです。" + schema_instruction,
                },
            )
        effective_messages = new_msgs

    # Newer reasoning architectures (o1, o3, etc.) require max_completion_tokens instead of max_tokens
    use_max_completion_tokens = _is_openai_reasoning_model(model) or bool(reasoning_effort)
    token_param_key = "max_completion_tokens" if use_max_completion_tokens else "max_tokens"

    payload: dict[str, Any] = {
        "model": model,
        "messages": effective_messages,
        token_param_key: token_limit,
    }
    if temperature is not None:
        payload["temperature"] = max(0.0, min(float(temperature), 2.0))
    if (
        reasoning_effort
        and isinstance(reasoning_effort, str)
        and reasoning_effort.lower() in ("low", "medium", "high")
    ):
        payload["reasoning_effort"] = reasoning_effort.lower()

    if requires_pydantic_parse and pydantic_cls is not None:
        payload["response_format"] = {"type": "json_object"}
    elif isinstance(response_format, dict):
        if response_format.get("type") == "json_schema":
            payload["response_format"] = {"type": "json_object"}
        else:
            payload["response_format"] = response_format

    if tools:
        payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice

    req_id = "-"
    try:
        if has_app_context():
            req_id = getattr(g, "request_id", "-")
    except Exception:
        pass

    logger.info(
        "OpenAI-compat call start id=%s model=%s base_url=%s",
        req_id,
        model,
        base_url,
    )

    try:
        with app_state.ai.openai_call_semaphore:
            client = _get_openai_client(base_url, api_key)
            resp = client.post(endpoint, json=payload, headers=headers)

            # Retry handler for common 400 / 422 parameter rejections across diverse OpenAI providers
            if resp.status_code in (400, 422):
                resp_text_lower = resp.text.lower()
                modified = False

                # 1. max_tokens / max_completion_tokens incompatibility
                if (
                    "max_tokens" in resp_text_lower or "max_completion_tokens" in resp_text_lower
                ) and (
                    "unsupported" in resp_text_lower
                    or "not supported" in resp_text_lower
                    or "deprecated" in resp_text_lower
                    or "extra" in resp_text_lower
                ):
                    if "max_tokens" in payload:
                        logger.info(
                            "OpenAI model %s rejected max_tokens. Retrying with max_completion_tokens.",
                            model,
                        )
                        payload["max_completion_tokens"] = payload.pop("max_tokens")
                        modified = True
                    elif "max_completion_tokens" in payload:
                        logger.info(
                            "OpenAI model %s rejected max_completion_tokens. Retrying with max_tokens.",
                            model,
                        )
                        payload["max_tokens"] = payload.pop("max_completion_tokens")
                        modified = True

                # 2. temperature rejected (e.g. o1/o3 only supports default temperature)
                if "temperature" in resp_text_lower and "temperature" in payload:
                    logger.info(
                        "OpenAI model %s rejected temperature (HTTP %d). Retrying without temperature.",
                        model,
                        resp.status_code,
                    )
                    payload.pop("temperature", None)
                    modified = True

                # 3. Tools rejected (common for non-tool reasoning/chat models)
                if tools and "tool" in resp_text_lower and "tools" in payload:
                    logger.info(
                        "OpenAI model %s rejected tools (HTTP %d). Retrying without tools.",
                        model,
                        resp.status_code,
                    )
                    payload.pop("tools", None)
                    payload.pop("tool_choice", None)
                    modified = True

                # 4. Reasoning effort rejected
                if "reasoning" in resp_text_lower and "reasoning_effort" in payload:
                    logger.info(
                        "OpenAI model %s rejected reasoning_effort (HTTP %d). Retrying without reasoning_effort.",
                        model,
                        resp.status_code,
                    )
                    payload.pop("reasoning_effort", None)
                    modified = True

                # 5. response_format rejected
                rf_reject_keywords = (
                    "response_format",
                    "json_schema",
                    "schema",
                    "format",
                    "extra",
                    "unrecognized",
                    "unexpected",
                    "not supported",
                )
                if (
                    any(kw in resp_text_lower for kw in rf_reject_keywords)
                    and "response_format" in payload
                ):
                    current_rf = payload.get("response_format")
                    if isinstance(current_rf, dict) and current_rf.get("type") == "json_schema":
                        logger.info(
                            "OpenAI model %s rejected json_schema (HTTP %d). Retrying with json_object.",
                            model,
                            resp.status_code,
                        )
                        payload["response_format"] = {"type": "json_object"}
                        modified = True
                    else:
                        logger.info(
                            "OpenAI model %s rejected response_format (HTTP %d). Retrying without response_format.",
                            model,
                            resp.status_code,
                        )
                        payload.pop("response_format", None)
                        modified = True

                if modified:
                    resp = client.post(endpoint, json=payload, headers=headers)

            if resp.status_code >= 400:
                err_detail = resp.text[:500]
                try:
                    err_json = resp.json()
                    if isinstance(err_json, dict) and "error" in err_json:
                        err_obj = err_json["error"]
                        err_detail = (
                            err_obj.get("message", err_detail)
                            if isinstance(err_obj, dict)
                            else str(err_obj)
                        )
                except Exception:
                    pass
                logger.error(
                    "OpenAI-compat error id=%s model=%s status=%d detail=%s",
                    req_id,
                    model,
                    resp.status_code,
                    err_detail,
                )
                if resp.status_code >= 500:
                    app_state.market.report_circuit_result("openai_compatible", success=False)
                    circuit_probe_claimed = False
                elif circuit_probe_claimed:
                    app_state.market.release_circuit_probe("openai_compatible")
                    circuit_probe_claimed = False
                return {
                    "error": {
                        "message": _public_openai_error_message(
                            Exception(err_detail), resp.status_code
                        ),
                        "status_code": resp.status_code,
                        "provider": "openai_compatible",
                    }
                }

            data = resp.json()
            if not isinstance(data, dict):
                return {
                    "error": {
                        "message": "Invalid JSON response from OpenAI provider",
                        "status_code": 502,
                    }
                }

            # Track successful circuit result & token usage
            app_state.market.report_circuit_result("openai_compatible", success=True)
            circuit_probe_claimed = False
            usage = data.get("usage")
            if isinstance(usage, dict):
                app_state.ai.record_mistral_usage(usage, model=model)
                app_state.ai.record_openai_usage(usage, model=model)

            # Pydantic structured output post-processing
            if requires_pydantic_parse and pydantic_cls is not None:
                try:
                    choices = data.get("choices") or []
                    if choices and isinstance(choices, list):
                        msg = choices[0].get("message") or {}
                        raw_content = (
                            msg.get("content")
                            or msg.get("reasoning")
                            or msg.get("reasoning_content")
                            or msg.get("thinking")
                            or ""
                        )
                        parsed_dict = None
                        json_str = None
                        try:
                            json_str = extract_json_payload(str(raw_content))
                        except Exception:
                            pass
                        if not json_str:
                            json_str = str(raw_content)

                        try:
                            parsed_model = pydantic_cls.model_validate_json(json_str)
                            parsed_dict = parsed_model.model_dump()
                        except Exception:
                            try:
                                raw_dict = json.loads(json_str)
                                if isinstance(raw_dict, dict):
                                    parsed_dict = raw_dict
                            except Exception:
                                pass

                        if parsed_dict is not None:
                            from utils.validators import _normalize_analysis_aliases

                            normalized_parsed = _normalize_analysis_aliases(parsed_dict)
                            data["choices"][0]["message"]["parsed"] = normalized_parsed
                            data["choices"][0]["message"]["content"] = normalized_parsed
                        elif raw_content and not msg.get("content"):
                            data["choices"][0]["message"]["content"] = raw_content
                except Exception as p_err:
                    logger.debug("Failed Pydantic parsing on OpenAI response: %s", p_err)
            else:
                choices = data.get("choices") or []
                if choices and isinstance(choices, list):
                    msg = choices[0].get("message") or {}
                    if not msg.get("content"):
                        reasoning_fallback = (
                            msg.get("reasoning")
                            or msg.get("reasoning_content")
                            or msg.get("thinking")
                        )
                        if reasoning_fallback:
                            data["choices"][0]["message"]["content"] = reasoning_fallback

            if use_cache and _openai_response_has_content(data):
                with app_state.ai.mistral_response_lock:
                    app_state.ai.mistral_response_cache[cache_key] = copy.deepcopy(data)

            return copy.deepcopy(data)

    except Exception as exc:
        raw_status = getattr(getattr(exc, "response", None), "status_code", None)
        resp_status = raw_status if isinstance(raw_status, int) else None
        # Circuit accounting mirrors the Mistral client: only provider-side
        # conditions may open the circuit — communication-class failures
        # (timeouts, connection resets, protocol errors, raw socket/OS errors)
        # and explicit 5xx responses. Application bugs (TypeError/KeyError/...
        # raised while building the payload or post-processing the response)
        # carry no HTTP response and defaulting them to 500 would count them
        # as provider failures: three in a row would open the circuit and
        # disable every OpenAI feature for the full window even though the
        # provider is healthy.
        if isinstance(exc, httpx.TimeoutException):
            status_code = 504
        elif isinstance(exc, (httpx.TransportError, ConnectionError, OSError)):
            status_code = 503
        elif resp_status is not None:
            status_code = resp_status
        else:
            status_code = 500
        is_provider_failure = isinstance(
            exc, (httpx.TransportError, ConnectionError, OSError)
        ) or (resp_status is not None and resp_status >= 500)
        if is_provider_failure:
            app_state.market.report_circuit_result("openai_compatible", success=False)
            circuit_probe_claimed = False
        elif circuit_probe_claimed:
            app_state.market.release_circuit_probe("openai_compatible")
            circuit_probe_claimed = False
        logger.error(
            "OpenAI-compat request failed model=%s url=%s error_type=%s status=%s: %s",
            model,
            endpoint,
            type(exc).__name__,
            status_code,
            exc,
        )
        return {
            "error": {
                "message": _public_openai_error_message(exc, status_code),
                "status_code": status_code,
                "provider": "openai_compatible",
            }
        }
    finally:
        if circuit_probe_claimed:
            app_state.market.release_circuit_probe("openai_compatible")
            circuit_probe_claimed = False


def _close_stream_context(stream_cm: Any, stream: Any = None) -> None:
    """Safely close an httpx streaming context manager and response on every exit path."""
    if stream is not None:
        try:
            close_fn = getattr(stream, "close", None)
            if callable(close_fn):
                close_fn()
        except Exception as exc:
            logger.debug("Failed closing stream response: %s", exc)
    if stream_cm is not None:
        try:
            exit_fn = getattr(stream_cm, "__exit__", None)
            if callable(exit_fn):
                exit_fn(None, None, None)
        except Exception as exc:
            logger.debug("Failed exiting stream context: %s", exc)


def stream_openai_chat(
    api_key: str,
    messages: list[Any],
    max_tokens: int = 600,
    temperature: float | None = 0.7,
    reasoning_effort: str | None | bool = None,
    include_thinking: bool | None = None,
    _model_override: str | None = None,
    _is_fallback: bool = False,
):
    """Stream an OpenAI-compatible chat completion yielding SSE event dicts.

    When include_thinking=False (explicitly set by user-facing chat), reasoning/thinking
    deltas (DeepSeek-R1 / OpenRouter / o1) are separated and not leaked into the
    final text response.
    When include_thinking=True, yields separate {"type": "thinking", "text": ...} events.
    When include_thinking=None (default), maintains backward compatibility by emitting
    reasoning deltas as text if content is absent.
    """
    if is_preview_api_key(api_key):
        yield from build_preview_stream_events(messages)
        return

    if app_state.market.is_circuit_open("openai_compatible"):
        logger.warning("OpenAI-compatible circuit is open. Skipping stream call.")
        yield {
            "type": "error",
            "message": "AIサービス（OpenAI互換）は一時的に利用できません（サーキットブレーカー発動中）",
            "status_code": 503,
        }
        return

    circuit_probe_claimed = False
    circuit_state = app_state.market.get_circuit_state("openai_compatible")
    if circuit_state.get("status") == "HALF_OPEN":
        if not app_state.market.try_claim_circuit_probe("openai_compatible"):
            logger.warning(
                "OpenAI-compatible circuit recovery probe already in progress. Skipping stream call."
            )
            yield {
                "type": "error",
                "message": "AIサービス（OpenAI互換）は一時的に利用できません（サーキット回復プローブ実行中）",
                "status_code": 503,
            }
            return
        circuit_probe_claimed = True

    base_url = get_openai_base_url()
    model = _model_override if _model_override else get_openai_model()
    token_limit = min(max(16, max_tokens), MISTRAL_MAX_TOKENS_CEIL)
    endpoint = f"{base_url}/chat/completions"
    headers = _build_openai_headers(api_key)

    use_max_completion_tokens = _is_openai_reasoning_model(model) or bool(reasoning_effort)
    token_param_key = "max_completion_tokens" if use_max_completion_tokens else "max_tokens"

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        token_param_key: token_limit,
        "stream": True,
    }
    if temperature is not None:
        payload["temperature"] = max(0.0, min(float(temperature), 2.0))
    if (
        reasoning_effort
        and isinstance(reasoning_effort, str)
        and reasoning_effort.lower() in ("low", "medium", "high")
    ):
        payload["reasoning_effort"] = reasoning_effort.lower()

    full_parts: list[str] = []
    logger.info("OpenAI stream start model=%s base_url=%s", model, base_url)

    try:
        with app_state.ai.openai_stream_semaphore:
            client = _get_openai_client(base_url, api_key)

            def _open_stream(p: dict[str, Any]):
                return client.stream("POST", endpoint, json=p, headers=headers)

            stream_cm = None
            stream = None
            try:
                stream_cm = _open_stream(payload)
                stream = stream_cm.__enter__()

                # Retry handler for common 400 parameter rejections (e.g. reasoning_effort / temperature / max_tokens)
                if stream.status_code in (400, 422):
                    stream.read()
                    err_detail = stream.text[:500]
                    err_detail_lower = err_detail.lower()
                    modified = False

                    if "temperature" in err_detail_lower and "temperature" in payload:
                        logger.info(
                            "OpenAI stream rejected temperature (HTTP %d). Retrying without temperature.",
                            stream.status_code,
                        )
                        payload.pop("temperature", None)
                        modified = True

                    if "reasoning" in err_detail_lower and "reasoning_effort" in payload:
                        logger.info(
                            "OpenAI stream rejected reasoning_effort (HTTP %d). Retrying without reasoning_effort.",
                            stream.status_code,
                        )
                        payload.pop("reasoning_effort", None)
                        modified = True

                    if (
                        "max_tokens" in err_detail_lower
                        or "max_completion_tokens" in err_detail_lower
                    ) and (
                        "unsupported" in err_detail_lower
                        or "not supported" in err_detail_lower
                        or "deprecated" in err_detail_lower
                    ):
                        if "max_tokens" in payload:
                            payload["max_completion_tokens"] = payload.pop("max_tokens")
                            modified = True
                        elif "max_completion_tokens" in payload:
                            payload["max_tokens"] = payload.pop("max_completion_tokens")
                            modified = True

                    if modified:
                        _close_stream_context(stream_cm, stream)
                        stream_cm = None
                        stream = None
                        stream_cm = _open_stream(payload)
                        stream = stream_cm.__enter__()

                if stream.status_code >= 400:
                    stream.read()
                    err_detail = stream.text[:500]
                    try:
                        err_json = json.loads(err_detail)
                        if isinstance(err_json, dict) and "error" in err_json:
                            err_obj = err_json["error"]
                            err_detail = (
                                err_obj.get("message", err_detail)
                                if isinstance(err_obj, dict)
                                else str(err_obj)
                            )
                    except Exception:
                        pass
                    logger.warning(
                        "OpenAI stream HTTP error status=%d detail=%s",
                        stream.status_code,
                        err_detail,
                    )
                    if stream.status_code >= 500:
                        app_state.market.report_circuit_result("openai_compatible", success=False)
                        circuit_probe_claimed = False
                    elif circuit_probe_claimed:
                        app_state.market.release_circuit_probe("openai_compatible")
                        circuit_probe_claimed = False
                    err_msg = _public_openai_error_message(
                        Exception(err_detail), stream.status_code
                    )
                    yield {
                        "type": "error",
                        "message": err_msg,
                        "status_code": stream.status_code,
                        "detail": err_detail,
                    }
                    return

                for line in stream.iter_lines():
                    if not line:
                        continue
                    line_str = (
                        line.decode("utf-8", errors="replace")
                        if isinstance(line, (bytes, bytearray))
                        else str(line)
                    ).strip()
                    if line_str.startswith("data:"):
                        data_part = line_str.removeprefix("data:").strip()
                        if data_part == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_part)
                            choices = chunk.get("choices") or []
                            if choices and isinstance(choices, list):
                                delta = choices[0].get("delta") or {}
                                delta_text = delta.get("content")
                                # Capture reasoning deltas separately
                                r_val = (
                                    delta.get("reasoning_content")
                                    or delta.get("reasoning")
                                    or delta.get("thinking")
                                )

                                if delta_text and isinstance(delta_text, str):
                                    full_parts.append(delta_text)
                                    yield {"type": "delta", "text": delta_text}
                                elif r_val:
                                    r_text = (
                                        r_val
                                        if isinstance(r_val, str)
                                        else "".join(str(x) for x in r_val)
                                    )
                                    if r_text:
                                        if include_thinking is True:
                                            yield {"type": "thinking", "text": r_text}
                                        elif include_thinking is None:
                                            full_parts.append(r_text)
                                            yield {"type": "delta", "text": r_text}

                        except Exception as c_err:
                            logger.debug("Failed parsing SSE chunk: %s", c_err)
                            continue

                app_state.market.report_circuit_result("openai_compatible", success=True)
                circuit_probe_claimed = False
            finally:
                _close_stream_context(stream_cm, stream)

        yield {"type": "done", "text": "".join(full_parts)}

    except Exception as exc:
        raw_status = getattr(getattr(exc, "response", None), "status_code", None)
        resp_status = raw_status if isinstance(raw_status, int) else None
        # Same rule as call_openai_chat: only communication-class failures and
        # explicit provider 5xx responses count against the circuit breaker.
        # Application bugs without an HTTP response must not open it.
        if isinstance(exc, httpx.TimeoutException):
            status_code = 504
        elif isinstance(exc, (httpx.TransportError, ConnectionError, OSError)):
            status_code = 503
        elif resp_status is not None:
            status_code = resp_status
        else:
            status_code = 500
        is_provider_failure = isinstance(
            exc, (httpx.TransportError, ConnectionError, OSError)
        ) or (resp_status is not None and resp_status >= 500)
        if is_provider_failure:
            app_state.market.report_circuit_result("openai_compatible", success=False)
            circuit_probe_claimed = False
        elif circuit_probe_claimed:
            app_state.market.release_circuit_probe("openai_compatible")
            circuit_probe_claimed = False
        logger.error(
            "OpenAI stream exception error_type=%s status=%d: %s",
            type(exc).__name__,
            status_code,
            exc,
        )
        yield {
            "type": "error",
            "message": _public_openai_error_message(exc, status_code),
            "status_code": status_code,
        }
    finally:
        if circuit_probe_claimed:
            app_state.market.release_circuit_probe("openai_compatible")
            circuit_probe_claimed = False


def _gen_token() -> str:
    import secrets

    return secrets.token_hex(4)


def call_openai_chat_with_tools(
    api_key: str,
    messages: list[Any],
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 1000,
    max_tool_iterations: int = 5,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    response_format: Any = None,
    cache_key_override: str | None = None,
) -> dict[str, Any]:
    """Execute OpenAI Chat Completion with autonomous tool calling agent loop."""
    import concurrent.futures
    import math

    _proxy: Any = current_app
    flask_app: Any = _proxy._get_current_object() if has_app_context() else None

    max_tool_calls_per_turn = 8
    max_tool_argument_chars = 16_384
    max_tool_call_id_chars = 128
    max_tool_result_chars = 32_000

    def _tool_loop_error(message: str) -> dict[str, Any]:
        # Mirrors services/ai_service.py: a malformed provider payload must
        # surface as a graceful 502-shaped error, not an AttributeError.
        return {"error": {"message": message, "status_code": 502}}

    def _json_safe_tool_value(value: Any) -> Any:
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if isinstance(value, dict):
            return {str(k): _json_safe_tool_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_json_safe_tool_value(v) for v in value]
        if value is None or isinstance(value, (str, int, bool)):
            return value
        try:
            json.dumps(value, ensure_ascii=False, allow_nan=False)
            return value
        except (TypeError, ValueError):
            return str(value)[:2_000]

    def _serialize_tool_result(value: Any) -> str:
        try:
            serialized = json.dumps(
                _json_safe_tool_value(value), ensure_ascii=False, allow_nan=False
            )
        except (TypeError, ValueError, RecursionError):
            serialized = json.dumps(
                {"error": "ツール結果のシリアライズに失敗しました"}, ensure_ascii=False
            )
        if len(serialized) > max_tool_result_chars:
            return json.dumps(
                {"error": "ツール結果が大きすぎるため省略しました"}, ensure_ascii=False
            )
        return serialized

    active_tools = tools if tools is not None else MISTRAL_FINANCIAL_TOOLS
    current_messages = list(messages) if isinstance(messages, list) else []

    max_tool_iterations = max(1, min(int(max_tool_iterations), 8))

    for iteration in range(max_tool_iterations):
        use_cache = iteration == 0 and not active_tools
        response = call_openai_chat(
            api_key,
            current_messages,
            max_tokens=max_tokens,
            use_cache=use_cache,
            response_format=response_format if iteration == max_tool_iterations - 1 else None,
            tools=active_tools if iteration < max_tool_iterations - 1 else None,
            cache_key_override=f"{cache_key_override}_iter{iteration}"
            if cache_key_override
            else None,
            reasoning_effort=reasoning_effort,
            temperature=temperature,
        )

        if "error" in response:
            return response

        choices = response.get("choices") or []
        if not choices or not isinstance(choices, list):
            return response

        # Mirror the Mistral path guard (services/ai_service.py): a broken
        # proxy can return non-dict choices, which would otherwise raise an
        # unhandled AttributeError on ``.get`` below.
        if not isinstance(choices[0], dict):
            return _tool_loop_error("AIサービスから不正な応答を受信しました")
        msg = choices[0].get("message") or {}
        if not isinstance(msg, dict):
            return _tool_loop_error("AIサービスから不正なメッセージを受信しました")
        tool_calls = msg.get("tool_calls")
        if not tool_calls or not isinstance(tool_calls, list):
            # Model gave final direct answer
            return response

        logger.info(
            "OpenAI agent loop turn %d: model requested %d tool calls",
            iteration + 1,
            len(tool_calls),
        )

        # Truncate BEFORE building the assistant message: the assistant turn
        # must reference exactly the tool_call_ids that get executed below,
        # otherwise providers reject the next request with HTTP 400
        # ("An assistant message with 'tool_calls' must be followed by tool
        # messages ...").
        if len(tool_calls) > max_tool_calls_per_turn:
            logger.warning(
                "OpenAI agent loop truncated excessive tool calls: "
                "turn=%d count=%d limit=%d",
                iteration + 1,
                len(tool_calls),
                max_tool_calls_per_turn,
            )
            tool_calls = tool_calls[:max_tool_calls_per_turn]

        # Drop malformed entries up front so the assistant message never
        # references a tool_call_id that has no matching tool result.
        valid_tcs = [tc for tc in tool_calls if isinstance(tc, dict)]
        if not valid_tcs:
            return _tool_loop_error("AIサービスから不正なツール呼び出しを受信しました")

        # Normalize assistant message for OpenAI/Ollama compatibility:
        # Some compatible servers reject 'content: None' while others accept it.
        # Ensure clean dict structure with role and tool_calls preserved.
        assistant_turn_msg = {
            "role": "assistant",
            "content": msg.get("content") or "",
            "tool_calls": valid_tcs,
        }
        current_messages.append(assistant_turn_msg)

        def _exec_single_tool(tc: dict[str, Any], iter_idx: int = iteration) -> dict[str, Any]:
            def _execute() -> dict[str, Any]:
                tc_id = tc.get("id") or f"call_{iter_idx}_{_gen_token()}"
                if len(tc_id) > max_tool_call_id_chars:
                    tc_id = tc_id[:max_tool_call_id_chars]
                fn = tc.get("function") or {}
                fn_name = str(fn.get("name") or "unknown_tool")[:80]
                fn_args = fn.get("arguments") or "{}"
                if isinstance(fn_args, str) and len(fn_args) > max_tool_argument_chars:
                    fn_args = "{}"

                tool_output = execute_mistral_tool_call(fn_name, fn_args)
                return {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": fn_name,
                    "content": _serialize_tool_result(tool_output),
                }

            if flask_app is not None:
                with flask_app.app_context():
                    return _execute()
            return _execute()

        if len(valid_tcs) > 1:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(len(valid_tcs), 4)
            ) as executor:
                tool_messages = list(executor.map(_exec_single_tool, valid_tcs))
            current_messages.extend(tool_messages)
        else:
            for tc in valid_tcs:
                current_messages.append(_exec_single_tool(tc))

    # If loop finishes without direct answer, call one last synthesis
    return call_openai_chat(
        api_key,
        current_messages,
        max_tokens=max_tokens,
        use_cache=False,
        response_format=response_format,
        reasoning_effort=reasoning_effort,
        temperature=temperature,
    )


def _sanitize_prompt_text(value: Any, max_len: int = 120) -> str:
    """Strip control/XML metacharacters from client-influenced values before
    they are interpolated into an LLM prompt (mirrors services.ai_service._sanitize_prompt_text)."""
    text = str(value if value is not None else "")
    text = "".join(c for c in text if ord(c) >= 0x20 or c in ("\t", "\n", "\r"))
    text = text.replace("<", " ").replace(">", " ")
    return text.strip()[:max_len]


def analyze_chart_image_with_openai(
    api_key: str,
    image_data: str,
    symbol: str = "",
    market: str = "",
    custom_prompt: str = "",
    model: str | None = None,
) -> dict[str, Any]:
    """Analyze a chart image using an OpenAI-compatible Vision API (multimodal)."""
    target_model = model or get_openai_model()

    clean_img = image_data.strip()
    if clean_img.startswith(("http://", "https://", "data:image/")):
        image_url = clean_img
    else:
        clean_b64 = "".join(clean_img.split())
        image_url = f"data:image/png;base64,{clean_b64}"

    safe_sym = _sanitize_prompt_text(symbol, 16) if symbol else "対象銘柄"
    user_text = (
        f"{safe_sym} の株価チャート画像を視覚的に分析してください。\n"
        "【分析タスク】\n"
        "1. チャート上の主要なトレンド（上昇・下降・保ち合い）\n"
        "2. 視覚的に確認できるサポートライン・レジスタンスラインの水準\n"
        "3. チャートパターン（ダブルボトム/トップ、三尊天井、逆三尊、三角保ち合い等）の検出\n"
        "4. 移動平均線やオシレーター等のテクニカル指標の配置と示唆\n"
        "5. 短期・中期の見通しと注目すべき価格ブレイクアウトポイント\n"
    )
    if custom_prompt:
        user_text += f"\n【ユーザーからの追加指示】\n{_sanitize_prompt_text(custom_prompt, 500)}\n"

    messages = [
        {
            "role": "system",
            "content": (
                "あなたは高度なテクニカル・チャート分析の専門家です。提供されたチャート画像（ローソク足、"
                "テクニカル指標等）を視覚的に精密に読み取り、客観的で実践的な分析レポートを作成してください。"
            ),
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        },
    ]

    response = call_openai_chat(
        api_key,
        messages=messages,
        max_tokens=2048,
        temperature=0.2,
        _model_override=target_model,
        use_cache=False,
    )
    if "error" in response:
        err_msg = response["error"].get("message", "")
        # Detect vision unsupported errors from provider
        if "image" in err_msg.lower() or "multimodal" in err_msg.lower() or "400" in str(response):
            return {
                "error": (
                    f"選択されたモデル ({target_model}) は画像認識（Vision）に対応していません。"
                    "gpt-4o等のマルチモーダルモデルを設定するか、Mistral（Pixtral）に切り替えてください。"
                )
            }
        return response

    content = extract_chat_content(response)
    return {
        "symbol": symbol,
        "market": market,
        "model": target_model,
        "analysis": content,
        "analyzed_at": datetime.now(UTC).isoformat(),
        "provider": "openai_compatible",
    }


def get_openai_embeddings_batch(
    texts: list[str],
    api_key: str | None = None,
    model: str | None = None,
    batch_size: int = 32,
) -> list[list[float] | None]:
    """Compute semantic embeddings using OpenAI-compatible /v1/embeddings API."""
    if not texts:
        return []

    if app_state.market.is_circuit_open("openai_compatible"):
        logger.warning("OpenAI-compatible circuit is open. Skipping embeddings call.")
        return [None] * len(texts)

    target_model = model or get_openai_embeddings_model()
    base_url = get_openai_base_url()
    endpoint = f"{base_url}/embeddings"
    headers = _build_openai_headers(api_key)

    results: list[list[float] | None] = [None] * len(texts)

    # Filter non-empty texts while tracking original indices to prevent HTTP 400
    valid_items: list[tuple[int, str]] = [
        (idx, t.strip()[:4000])
        for idx, t in enumerate(texts)
        if isinstance(t, str) and t.strip()
    ]

    if not valid_items:
        return results

    effective_batch_size = max(1, batch_size)
    for i in range(0, len(valid_items), effective_batch_size):
        chunk = valid_items[i : i + effective_batch_size]
        chunk_texts = [text for _, text in chunk]
        payload = {
            "model": target_model,
            "input": chunk_texts,
        }
        try:
            client = _get_openai_client(base_url, api_key)
            resp = client.post(endpoint, json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                raw_data = data.get("data", []) if isinstance(data, dict) else []
                for fallback_idx, item in enumerate(raw_data):
                    if not isinstance(item, dict):
                        continue
                    pos = item.get("index")
                    if not isinstance(pos, int) or pos < 0 or pos >= len(chunk):
                        pos = fallback_idx
                    if 0 <= pos < len(chunk):
                        orig_idx = chunk[pos][0]
                        emb = item.get("embedding")
                        if isinstance(emb, list) and orig_idx < len(results):
                            results[orig_idx] = emb
            else:
                logger.debug(
                    "OpenAI embeddings returned HTTP %d: %s (graceful fallback)",
                    resp.status_code,
                    resp.text[:200],
                )
        except Exception as exc:
            logger.debug("OpenAI embeddings call failed (fallback to None): %s", exc)

    return results
