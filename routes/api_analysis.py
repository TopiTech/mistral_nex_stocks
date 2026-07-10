import re
from datetime import datetime, timezone
from typing import Any

import requests
import threading
from flask import Blueprint, request, jsonify, current_app, g


from app_state import app_state
from services.news_service import news_service
from app_helpers import (
    normalize_market,
    get_cached,
    _is_local_request,
    _parse_json_request,
    error_response,
    normalize_symbol_for_market,
    get_cached_context_with_negative_cache,
    normalize_symbol,
    normalize_text,
    get_stock_info_cached,
)
from route_helpers import (
    extract_langsearch_api_key,
    extract_tavily_api_key,
    extract_api_key,
    rate_limit,
)
from services.search_service import (
    _determine_search_strategy,
    _get_market_trending_titles,
    collect_symbol_research_context,
)
from services.ai_service import (
    call_mistral_chat,
    repair_analysis_json_with_llm,
)
from app_bg import fetch_stock
from utils.validators import (
    StockAnalysis,
    safe_parse_analysis_result,
    extract_chat_content,
)
from utils.formatting import build_fallback_analysis_result
from error_codes import ErrorCode
from constants import (
    ANALYZE_RESEARCH_CONTEXT_MAX_CHARS,
    CACHE_DURATION_TRENDING,
    ANALYSIS_MAX_TOKENS,
    CHAT_MAX_TOKENS,
    CHAT_MAX_MSG_LENGTH,
    CHAT_HISTORY_MAX_KEYS,
    CHAT_HISTORY_MAX_MSGS,
    NEWS_PREPARE_WAIT_SEC,
)

from config_utils import get_custom_ai_prompt

api_analysis_bp = Blueprint("api_analysis", __name__)

ANALYSIS_DISCLAIMER = {
    "ja": (
        "本データは情報提供のみを目的としており、投資助言や推奨を構成するものではありません。"
        "投資判断はご自身の責任で行ってください。過去のパフォーマンスは将来の結果を保証するものではありません。"
    ),
    "en": (
        "This data is for informational purposes only and does not constitute investment advice "
        "or recommendations. Investment decisions should be made at your own risk. "
        "Past performance does not guarantee future results."
    ),
}


@api_analysis_bp.route("/api/trending")
@rate_limit(max_requests=30, window_seconds=60)
def get_trending():
    """トレンド情報を返すAPIエンドポイント"""
    market = normalize_market(request.args.get("market"), default="us") or "us"
    langsearch_api_key = extract_langsearch_api_key(request)
    tavily_api_key = extract_tavily_api_key(request)

    strategy = _determine_search_strategy(tavily_api_key, langsearch_api_key)

    def _fetch():
        try:
            return {
                "trending": _get_market_trending_titles(
                    market, strategy, langsearch_api_key, tavily_api_key
                )
            }
        except (RuntimeError, ValueError, KeyError, TypeError, OSError) as e:
            current_app.logger.error("Trending fetch error: %s", e)
            return {"trending": []}

    result = get_cached(
        f"trending_list_{market}_{strategy}",
        _fetch,
        duration=CACHE_DURATION_TRENDING,
        valid_func=lambda payload: bool(
            isinstance(payload, dict) and payload.get("trending")
        ),
    )
    return jsonify(result)


@api_analysis_bp.route("/api/chat", methods=["POST"])
@rate_limit(max_requests=45, window_seconds=60)
def api_chat():
    """チャットAPIエンドポイント"""
    if not _is_local_request(request):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    api_key = extract_api_key(request)
    if not api_key:
        return error_response(ErrorCode.INVALID_API_KEY, status_code=401)

    data = _parse_json_request()
    if data is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )
    market = normalize_market(data.get("market"), default="us")
    symbol = normalize_symbol_for_market(data.get("symbol"), market)
    user_msg = (data.get("message") or "").strip()
    if len(user_msg) > CHAT_MAX_MSG_LENGTH:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": f"メッセージは{CHAT_MAX_MSG_LENGTH}文字以内で入力してください。"},
            status_code=400,
        )
    if not market:
        return error_response(ErrorCode.INVALID_MARKET)
    if not symbol or not user_msg:
        return error_response(
            ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["symbol", "message"]}
        )

    current_app.logger.info(
        "api_chat input id=%s market=%s symbol=%s msg_len=%d",
        getattr(g, "request_id", "-"),
        market,
        symbol,
        len(user_msg),
    )

    chat_key = f"{market}:{symbol}"

    # チャット履歴の管理
    with app_state.ai.chat_history_lock:
        if chat_key in app_state.ai.chat_history:
            app_state.ai.chat_history.move_to_end(chat_key)
        else:
            # symbolはユーザー入力のため、プロンプトに直接埋めず構造化データとして渡す
            safe_symbol = re.sub(r"[^\w\-.^=]", "", symbol)[:15]
            app_state.ai.chat_history[chat_key] = [
                {
                    "role": "system",
                    "content": "あなたは株式株式銘柄の専門家です。簡潔かつ投資家に有益な回答をしてください。",
                },
                {
                    "role": "user",
                    "content": f"[対象銘柄: {safe_symbol}] この銘柄について質問します。",
                },
                {
                    "role": "assistant",
                    "content": f"{safe_symbol}銘柄についてお答えします。",
                },
            ]

        if len(app_state.ai.chat_history) > CHAT_HISTORY_MAX_KEYS:
            app_state.ai.chat_history.popitem(last=False)

        app_state.ai.chat_history[chat_key].append({"role": "user", "content": user_msg})

        if len(app_state.ai.chat_history[chat_key]) > CHAT_HISTORY_MAX_MSGS:
            app_state.ai.chat_history[chat_key] = [
                app_state.ai.chat_history[chat_key][0]
            ] + app_state.ai.chat_history[chat_key][-(CHAT_HISTORY_MAX_MSGS - 1):]

        messages_snapshot = list(app_state.ai.chat_history[chat_key])

    # Mistral APIを呼び出し
    try:
        response = call_mistral_chat(
            api_key,
            messages_snapshot,
            max_tokens=CHAT_MAX_TOKENS,
            cache_key_override=f"chat_{market}_{symbol}",
        )

        # エラーチェック
        if isinstance(response, dict) and "error" in response:
            error_msg = response["error"].get("message", "Unknown error")
            current_app.logger.error(
                "api_chat mistral error id=%s: %s",
                getattr(g, "request_id", "-"),
                error_msg,
            )
            return jsonify({"reply": f"APIエラー: {error_msg}"}), 500

        # contentを安全に抽出（validators.extract_chat_content を単一の正規抽出器として使用）
        try:
            ai_content = extract_chat_content(response)

            current_app.logger.debug(
                "api_chat content extraction result id=%s: type=%s, len=%d",
                getattr(g, "request_id", "-"),
                type(response).__name__,
                len(ai_content) if ai_content else 0,
            )

            if not ai_content:
                current_app.logger.warning(
                    "api_chat empty content id=%s, retrying once...",
                    getattr(g, "request_id", "-"),
                )
                # トランジェントな空レスポンス対策としてキャッシュなしで1回リトライ
                retry_response = call_mistral_chat(
                    api_key,
                    messages_snapshot,
                    max_tokens=CHAT_MAX_TOKENS,
                    cache_key_override=f"chat_{market}_{symbol}",
                )
                ai_content = extract_chat_content(retry_response)

            if not ai_content:
                current_app.logger.error(
                    "api_chat retry also returned empty content id=%s",
                    getattr(g, "request_id", "-"),
                )
                ai_content = "(応答を生成できませんでした)"

        except (KeyError, IndexError, AttributeError) as e:
            current_app.logger.error(
                "api_chat content extraction error id=%s: %s",
                getattr(g, "request_id", "-"),
                str(e),
            )
            return jsonify({"reply": "応答の処理に失敗しました"}), 500

        # チャット履歴に応答を追加
        with app_state.ai.chat_history_lock:
            if chat_key in app_state.ai.chat_history:
                app_state.ai.chat_history[chat_key].append(
                    {"role": "assistant", "content": ai_content}
                )

        current_app.logger.info(
            "api_chat success id=%s content_len=%d",
            getattr(g, "request_id", "-"),
            len(ai_content),
        )

        return jsonify({"reply": ai_content, "disclaimer": ANALYSIS_DISCLAIMER})

    except (requests.ConnectionError, ConnectionError) as e:
        current_app.logger.error(
            "api_chat network error id=%s: %s",
            getattr(g, "request_id", "-"),
            str(e),
        )
        return jsonify({"reply": "AIサービスに接続できませんでした"}), 503
    except (ValueError, TypeError) as e:
        current_app.logger.error(
            "api_chat processing error id=%s: %s",
            getattr(g, "request_id", "-"),
            str(e),
        )
        return jsonify({"reply": f"入力データが不正です: {e}"}), 400
    except (RuntimeError, OSError) as e:
        current_app.logger.error(
            "api_chat system error id=%s: %s",
            getattr(g, "request_id", "-"),
            str(e),
            exc_info=True,
        )
        return jsonify({"reply": "チャット処理に失敗しました"}), 500


# Module-level tracking for in-flight news fetches to prevent duplicate execution
news_fetch_lock = threading.Lock()
news_fetch_inflight: dict[str, dict[str, Any]] = {}


@api_analysis_bp.route("/api/news", methods=["POST"])
@rate_limit(max_requests=20, window_seconds=60)
def api_news():
    """ニュースAPIエンドポイント

    重い収集・LLM要約はバックグラウンドexecutorへオフロードする。
    リクエストスレッドは短い上限(NEWS_PREPARE_WAIT_SEC)で完了を待ち、
    それを超える場合のみ fetching:True を返してクライアントにポーリングさせる。
    これによりワーカー枯渇(ローカルDoS)を防ぐ。
    """
    if not _is_local_request(request):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    api_key = extract_api_key(request)
    langsearch_api_key = extract_langsearch_api_key(request)
    tavily_api_key = extract_tavily_api_key(request)
    if not api_key:
        return error_response(ErrorCode.INVALID_API_KEY, status_code=401)

    strategy = _determine_search_strategy(tavily_api_key, langsearch_api_key)
    force_refresh = (request.args.get("force") or "").strip().lower() == "true"

    current_app.logger.info(
        "api_news start id=%s langsearch=%s tavily=%s strategy=%s force_refresh=%s",
        getattr(g, "request_id", "-"),
        bool(langsearch_api_key),
        bool(tavily_api_key),
        strategy,
        force_refresh,
    )

    inflight_key = f"news_{strategy}"

    with news_fetch_lock:
        if inflight_key in news_fetch_inflight:
            result_holder = news_fetch_inflight[inflight_key]
            already_fetching = True
        else:
            result_holder = {
                "result": None,
                "error": None,
                "done": threading.Event(),
            }
            news_fetch_inflight[inflight_key] = result_holder
            already_fetching = False

    if not already_fetching:
        def _run_news_job() -> None:
            try:
                result_holder["result"] = news_service.get_synchronized_market_news(
                    api_key=api_key,
                    langsearch_api_key=langsearch_api_key,
                    tavily_api_key=tavily_api_key,
                    force_refresh=force_refresh,
                )
            except (requests.RequestException, ValueError, KeyError, RuntimeError) as exc:
                result_holder["error"] = exc
            finally:
                with news_fetch_lock:
                    news_fetch_inflight.pop(inflight_key, None)
                result_holder["done"].set()

        try:
            app_state.execution.news_executor.submit(_run_news_job)
        except (RuntimeError, AttributeError, ValueError) as exc:
            current_app.logger.error("Failed to schedule news job: %s", exc)
            with news_fetch_lock:
                news_fetch_inflight.pop(inflight_key, None)
            return error_response(ErrorCode.INTERNAL_SERVER_ERROR, status_code=500)

    finished = result_holder["done"].wait(timeout=NEWS_PREPARE_WAIT_SEC)
    if not finished:
        # バックグラウンドで継続生成中。クライアントは fetchInitialStocks / タイマで再取得。
        return jsonify({"fetching": True})

    if result_holder["error"] is not None:
        current_app.logger.error("News API error: %s", result_holder["error"])
        return error_response(ErrorCode.INTERNAL_SERVER_ERROR, status_code=500)

    result = result_holder["result"]
    if not isinstance(result, dict):
        return error_response(ErrorCode.INTERNAL_SERVER_ERROR, status_code=500)
    result["disclaimer"] = ANALYSIS_DISCLAIMER
    return jsonify(result)


@api_analysis_bp.route("/api/analyze-v2", methods=["POST"])
@rate_limit(max_requests=20, window_seconds=60)
def api_analyze_v2():
    """
    Phase 1 Pilot: Mistral Function Calling variant

    This endpoint demonstrates Function Calling integration with Mistral API.
    Benefits over v1:
    - Tool definitions ensure structured output from Mistral
    - No dependency on JSON regex extraction
    - Parallel tool calling support (future)
    - More robust error handling
    """
    if not _is_local_request(request):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    api_key = extract_api_key(request)
    langsearch_api_key = extract_langsearch_api_key(request)
    tavily_api_key = extract_tavily_api_key(request)
    if not api_key:
        return error_response(ErrorCode.INVALID_API_KEY, status_code=401)

    data = _parse_json_request()
    if data is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )
    raw_symbol = data.get("symbol")
    fallback_name = normalize_symbol(raw_symbol)
    market = normalize_market(data.get("market"), default="us")
    symbol = normalize_symbol_for_market(raw_symbol, market)
    name = normalize_text(data.get("name"), default=(symbol or fallback_name))
    price = data.get("price")
    chart_data = data.get("chart_data", []) or []

    if not market:
        return error_response(ErrorCode.INVALID_MARKET)
    if not symbol:
        return error_response(
            ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["symbol"]}
        )

    current_app.logger.info(
        "api_analyze_v2 input id=%s market=%s symbol=%s has_price=%s chart_points=%d langsearch=%s tavily=%s",
        getattr(g, "request_id", "-"),
        market,
        symbol,
        price is not None,
        len(chart_data or []),
        bool(langsearch_api_key),
        bool(tavily_api_key),
    )

    try:
        # Fetch missing data
        if not chart_data or price is None:
            fetched = fetch_stock(symbol, name, market)
            if fetched:
                chart_data = chart_data or fetched.get("chart_data", [])
                if price is None:
                    price = fetched.get("price")

        # Gather research context
        research_context = get_cached_context_with_negative_cache(
            f"research_context_{symbol}_{market}_fc",
            lambda: collect_symbol_research_context(
                symbol, name, market, langsearch_api_key=langsearch_api_key, tavily_api_key=tavily_api_key
            ),
            600,
            120,
            True,
        )
        if len(research_context) > ANALYZE_RESEARCH_CONTEXT_MAX_CHARS:
            research_context = research_context[:ANALYZE_RESEARCH_CONTEXT_MAX_CHARS]

        info = get_stock_info_cached(symbol)
        sector = info.get("sector") or data.get("sector") or ""
        industry = info.get("industry") or data.get("industry") or ""
        market_cap = (
            info.get("marketCap")
            if info.get("marketCap") is not None
            else data.get("market_cap")
        )
        pe_ratio = (
            info.get("trailingPE")
            if info.get("trailingPE") is not None
            else data.get("pe_ratio")
        )
        price_trend = " → ".join([str(d.get("price")) for d in chart_data[-6:]])

        # Define analysis tools for Structured Outputs (json_schema)
        # Note: We use the StockAnalysis Pydantic model directly via client.chat.parse

        # System and user prompts
        system_prompt = (
            "あなたは株式分析の専門家です。提供された情報を元に、"
            "厳密な分析結果を構造化データとして返してください。"
            "数値データは入力された通貨単位を維持し、断定できない情報は保守的に扱ってください。"
        )

        user_prompt = (
            f"以下の銘柄を分析してください。\n"
            f"【銘柄情報】\n"
            f"- シンボル: {symbol}\n"
            f"- 企業名: {name}\n"
            f"- 現在価格: {price}\n"
            f"- 業種: {industry or 'N/A'}\n"
            f"- セクター: {sector or 'N/A'}\n"
            f"- 時価総額: {market_cap or 'N/A'}\n"
            f"- PER: {pe_ratio or 'N/A'}\n"
            f"- 直近価格推移: {price_trend}\n"
            f"【外部調査コンテキスト】\n{research_context}\n"
        )
        custom_prompt = get_custom_ai_prompt()
        if custom_prompt:
            user_prompt += f"\n【ユーザーからの追加指示】\n{custom_prompt}\n"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # Call Mistral with Structured Output (json_schema / Strict Mode)
        try:
            response = call_mistral_chat(
                api_key,
                messages=messages,
                max_tokens=ANALYSIS_MAX_TOKENS,

                response_format=StockAnalysis,

                reasoning_effort="none",
            )
        except (requests.ConnectionError, ConnectionError, OSError) as api_err:
            current_app.logger.error("Analyze-v2 API call failed: %s", api_err)
            return jsonify(
                build_fallback_analysis_result("AI解析APIエラー: API呼び出しに失敗しました")
            ), 200

        # Extract, validate, and normalize result using safe_parse_analysis_result helper
        # Pass local repair_analysis_json_with_llm to pick up unit test mocking
        result = safe_parse_analysis_result(
            response, api_key, repair_func=repair_analysis_json_with_llm
        )

        result["search_used"] = bool(research_context.strip())
        result["analyzed_at"] = datetime.now(timezone.utc).isoformat()
        result["version"] = "v2-structured-pydantic-2026"
        result["tool_used"] = True
        result["disclaimer"] = ANALYSIS_DISCLAIMER

        current_app.logger.info(
            "Analyze-v2 success id=%s symbol=%s recommendation=%s sentiment=%s",
            getattr(g, "request_id", "-"),
            symbol,
            result.get("recommendation"),
            result.get("sentiment"),
        )

        # Store in chat history (LRU/limitロジックをv1と統一)
        chat_key = f"{market}:{symbol}"
        with app_state.ai.chat_history_lock:
            if chat_key in app_state.ai.chat_history:
                app_state.ai.chat_history.move_to_end(chat_key)
            else:
                app_state.ai.chat_history[chat_key] = [
                    {
                        "role": "system",
                        "content": f"あなたは{symbol}銘柄の専門家です。簡潔かつ投資家に有益な回答をしてください。",
                    }
                ]

            if len(app_state.ai.chat_history) > CHAT_HISTORY_MAX_KEYS:
                app_state.ai.chat_history.popitem(last=False)

            app_state.ai.chat_history[chat_key].append(
                {
                    "role": "assistant",
                    "content": f"分析サマリー（v2）: {result.get('analysis_summary')}",
                }
            )

            if len(app_state.ai.chat_history[chat_key]) > CHAT_HISTORY_MAX_MSGS:
                app_state.ai.chat_history[chat_key] = [
                    app_state.ai.chat_history[chat_key][0]
                ] + app_state.ai.chat_history[chat_key][-(CHAT_HISTORY_MAX_MSGS - 1):]
        return jsonify(result)
    except (requests.ConnectionError, ConnectionError, OSError) as e:
        current_app.logger.error("Analyze-v2 network error: %s", e)
        return error_response(ErrorCode.INTERNAL_SERVER_ERROR, status_code=500)
    except (ValueError, TypeError) as e:
        current_app.logger.error("Analyze-v2 data processing error: %s", e)
        return error_response(ErrorCode.INVALID_INPUT, status_code=400)
