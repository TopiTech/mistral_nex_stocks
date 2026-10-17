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
from flask import g, has_app_context
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


def _public_openai_error_message(exc: BaseException, status_code: int = 0) -> str:
    """Return a clean, actionable user error message for OpenAI-compatible API calls."""
    if isinstance(exc, httpx.TimeoutException):
        return "OpenAI互換APIがタイムアウトしました。サーバーの稼働状況を確認して再試行してください。"
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


def _build_openai_headers(api_key: str | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "MistralNeXStocks/3.0 (OpenAI-Compat-Beta)",
        "HTTP-Referer": "http://localhost:5000",
        "X-Title": "MistralNeXStocks",
    }
    if api_key and api_key.strip() and not is_preview_api_key(api_key):
        headers["Authorization"] = f"Bearer {api_key.strip()}"
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
                    err_text = body.get("error", {}).get("message", "") if isinstance(body, dict) else ""
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
) -> str:
    raw_key = {
        "provider": "openai_compatible",
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "response_format": str(response_format),
        "tools": bool(tools),
        "tool_choice": str(tool_choice) if tool_choice else None,
        "reasoning_effort": str(reasoning_effort) if reasoning_effort else None,
        "cache_key_override": cache_key_override,
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
    )
    if use_cache:
        with app_state.ai.mistral_response_lock:
            cached = app_state.ai.mistral_response_cache.get(cache_key)
        if cached is not None:
            return copy.deepcopy(cached)

    endpoint = f"{base_url}/chat/completions"
    headers = _build_openai_headers(api_key)

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": token_limit,
    }
    if temperature is not None:
        payload["temperature"] = max(0.0, min(float(temperature), 2.0))
    if reasoning_effort and isinstance(reasoning_effort, str):
        payload["reasoning_effort"] = reasoning_effort

    # Response format handling
    requires_pydantic_parse = False
    pydantic_cls = None
    if isinstance(response_format, type) and issubclass(response_format, BaseModel):
        requires_pydantic_parse = True
        pydantic_cls = response_format
        payload["response_format"] = {"type": "json_object"}
    elif isinstance(response_format, dict):
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
        with _create_httpx_client(timeout=MISTRAL_API_TIMEOUT_SEC, follow_redirects=True) as client:
            resp = client.post(endpoint, json=payload, headers=headers)

            # If tools were passed but model rejected with 400 (common for non-tool models)
            if resp.status_code == 400 and tools and "tool" in resp.text.lower():
                logger.info("OpenAI model %s rejected tools (HTTP 400). Retrying without tools.", model)
                payload.pop("tools", None)
                payload.pop("tool_choice", None)
                resp = client.post(endpoint, json=payload, headers=headers)

            if resp.status_code >= 400:
                err_detail = resp.text[:500]
                try:
                    err_json = resp.json()
                    if isinstance(err_json, dict) and "error" in err_json:
                        err_obj = err_json["error"]
                        err_detail = err_obj.get("message", err_detail) if isinstance(err_obj, dict) else str(err_obj)
                except Exception:
                    pass
                logger.warning(
                    "OpenAI-compat error id=%s status=%d detail=%s",
                    req_id,
                    resp.status_code,
                    err_detail,
                )
                return {
                    "error": {
                        "message": _public_openai_error_message(Exception(err_detail), resp.status_code),
                        "status_code": resp.status_code,
                        "provider": "openai_compatible",
                    }
                }

            data = resp.json()
            if not isinstance(data, dict):
                return {"error": {"message": "Invalid JSON response from OpenAI provider", "status_code": 502}}

            # Track token usage
            usage = data.get("usage")
            if isinstance(usage, dict):
                app_state.ai.record_mistral_usage(usage, model=model)

            # Pydantic structured output post-processing
            if requires_pydantic_parse and pydantic_cls is not None:
                try:
                    choices = data.get("choices") or []
                    if choices and isinstance(choices, list):
                        msg = choices[0].get("message") or {}
                        raw_content = msg.get("content") or ""
                        json_str = extract_json_payload(str(raw_content)) or str(raw_content)
                        try:
                            parsed_model = pydantic_cls.model_validate_json(json_str)
                            parsed_dict = parsed_model.model_dump()
                            data["choices"][0]["message"]["parsed"] = parsed_dict
                            data["choices"][0]["message"]["content"] = parsed_dict
                        except Exception:
                            try:
                                raw_dict = json.loads(json_str)
                                if isinstance(raw_dict, dict):
                                    data["choices"][0]["message"]["parsed"] = raw_dict
                                    data["choices"][0]["message"]["content"] = raw_dict
                            except Exception:
                                pass
                except Exception as p_err:
                    logger.debug("Failed Pydantic parsing on OpenAI response: %s", p_err)

            if use_cache and data.get("choices"):
                with app_state.ai.mistral_response_lock:
                    app_state.ai.mistral_response_cache[cache_key] = copy.deepcopy(data)

            return copy.deepcopy(data)

    except Exception as exc:
        logger.error("OpenAI-compat request failed error_type=%s: %s", type(exc).__name__, exc)
        return {
            "error": {
                "message": _public_openai_error_message(exc),
                "status_code": 500,
                "provider": "openai_compatible",
            }
        }


def stream_openai_chat(
    api_key: str,
    messages: list[Any],
    max_tokens: int = 600,
    temperature: float | None = 0.7,
    reasoning_effort: str | None | bool = None,
    _model_override: str | None = None,
    _is_fallback: bool = False,
):
    """Stream an OpenAI-compatible chat completion yielding SSE event dicts."""
    if is_preview_api_key(api_key):
        yield from build_preview_stream_events(messages)
        return

    base_url = get_openai_base_url()
    model = _model_override if _model_override else get_openai_model()
    token_limit = min(max(16, max_tokens), MISTRAL_MAX_TOKENS_CEIL)
    endpoint = f"{base_url}/chat/completions"
    headers = _build_openai_headers(api_key)

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": token_limit,
        "stream": True,
    }
    if temperature is not None:
        payload["temperature"] = max(0.0, min(float(temperature), 2.0))
    if reasoning_effort and isinstance(reasoning_effort, str):
        payload["reasoning_effort"] = reasoning_effort

    full_parts: list[str] = []
    logger.info("OpenAI stream start model=%s base_url=%s", model, base_url)

    try:
        with (
            _create_httpx_client(timeout=MISTRAL_API_TIMEOUT_SEC) as client,
            client.stream("POST", endpoint, json=payload, headers=headers) as stream,
        ):
            if stream.status_code >= 400:
                stream.read()
                err_detail = stream.text[:500]
                try:
                    err_json = json.loads(err_detail)
                    if isinstance(err_json, dict) and "error" in err_json:
                        err_obj = err_json["error"]
                        err_detail = err_obj.get("message", err_detail) if isinstance(err_obj, dict) else str(err_obj)
                except Exception:
                    pass
                logger.warning(
                    "OpenAI stream HTTP error status=%d detail=%s",
                    stream.status_code,
                    err_detail,
                )
                err_msg = _public_openai_error_message(Exception(err_detail), stream.status_code)
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
                line_str = line.strip()
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
                            # Also capture reasoning deltas if present (e.g. DeepSeek-R1 / o1)
                            if not delta_text and "reasoning_content" in delta:
                                delta_text = delta.get("reasoning_content")
                            if delta_text and isinstance(delta_text, str):
                                full_parts.append(delta_text)
                                yield {"type": "delta", "text": delta_text}
                    except Exception as c_err:
                        logger.debug("Failed parsing SSE chunk: %s", c_err)
                        continue

        yield {"type": "done", "text": "".join(full_parts)}

    except Exception as exc:
        logger.error("OpenAI stream exception error_type=%s: %s", type(exc).__name__, exc)
        yield {
            "type": "error",
            "message": _public_openai_error_message(exc),
            "status_code": 500,
        }


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
    active_tools = tools if tools is not None else MISTRAL_FINANCIAL_TOOLS
    current_messages = list(messages) if isinstance(messages, list) else []

    max_tool_iterations = max(1, min(int(max_tool_iterations), 8))
    max_tool_calls_per_turn = 8

    for iteration in range(max_tool_iterations):
        use_cache = iteration == 0 and not active_tools
        response = call_openai_chat(
            api_key,
            current_messages,
            max_tokens=max_tokens,
            use_cache=use_cache,
            response_format=response_format if iteration == max_tool_iterations - 1 else None,
            tools=active_tools if iteration < max_tool_iterations - 1 else None,
            cache_key_override=f"{cache_key_override}_iter{iteration}" if cache_key_override else None,
            reasoning_effort=reasoning_effort,
            temperature=temperature,
        )

        if "error" in response:
            return response

        choices = response.get("choices") or []
        if not choices or not isinstance(choices, list):
            return response

        msg = choices[0].get("message") or {}
        tool_calls = msg.get("tool_calls")
        if not tool_calls or not isinstance(tool_calls, list):
            # Model gave final direct answer
            return response

        logger.info(
            "OpenAI agent loop turn %d: model requested %d tool calls",
            iteration + 1,
            len(tool_calls),
        )

        # Append assistant message with tool calls
        current_messages.append(msg)

        if len(tool_calls) > max_tool_calls_per_turn:
            tool_calls = tool_calls[:max_tool_calls_per_turn]

        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            tc_id = tc.get("id") or f"call_{iteration}_{_gen_token()}"
            fn = tc.get("function") or {}
            fn_name = fn.get("name") or ""
            fn_args = fn.get("arguments") or "{}"

            # Execute financial tool
            result = execute_mistral_tool_call(fn_name, fn_args)
            try:
                serialized = json.dumps(result, ensure_ascii=False)
            except Exception:
                serialized = json.dumps({"error": "Failed to serialize tool result"}, ensure_ascii=False)

            current_messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": serialized,
            })

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

    safe_sym = symbol.strip()[:16] if symbol else "対象銘柄"
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
        user_text += f"\n【ユーザーからの追加指示】\n{custom_prompt.strip()[:500]}\n"

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

    target_model = model or get_openai_embeddings_model()
    base_url = get_openai_base_url()
    endpoint = f"{base_url}/embeddings"
    headers = _build_openai_headers(api_key)

    results: list[list[float] | None] = [None] * len(texts)

    for i in range(0, len(texts), batch_size):
        chunk = [t.strip()[:4000] for t in texts[i : i + batch_size]]
        payload = {
            "model": target_model,
            "input": chunk,
        }
        try:
            with _create_httpx_client(timeout=15.0) as client:
                resp = client.post(endpoint, json=payload, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    raw_data = data.get("data", []) if isinstance(data, dict) else []
                    for idx, item in enumerate(raw_data):
                        emb = item.get("embedding") if isinstance(item, dict) else None
                        if isinstance(emb, list) and (i + idx) < len(results):
                            results[i + idx] = emb
                else:
                    logger.debug(
                        "OpenAI embeddings returned HTTP %d: %s (graceful fallback)",
                        resp.status_code,
                        resp.text[:200],
                    )
        except Exception as exc:
            logger.debug("OpenAI embeddings call failed (fallback to None): %s", exc)

    return results
