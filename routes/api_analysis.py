import hashlib
import json
import re
from datetime import datetime, timezone
from concurrent.futures import wait

import requests
from flask import Blueprint, request, jsonify, current_app, g

from mistral_compat import SystemMessage, UserMessage  # type: ignore[attr-defined,no-redef]

from app_state import app_state, NewsSummaryModel, StockAnalysis
from services.news_formatter import NewsFormatter
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
    _extract_text_from_mistral_content,
    rate_limit,
)
from services.search_service import (
    _determine_search_strategy,
    _get_market_trending_titles,
    collect_market_news_context,
    collect_market_trending_titles,
    collect_symbol_research_context,
)
from services.ai_service import (
    call_mistral_chat,
    repair_analysis_json_with_llm,
)
from app_bg import fetch_stock
from utils.validators import (
    extract_chat_content,
    validate_analysis_result,
    normalize_analysis_result,
)
from utils.formatting import build_fallback_analysis_result
from error_codes import ErrorCode
from constants import (
    NEWS_CONTEXT_WAIT_TIMEOUT,
    ANALYZE_RESEARCH_CONTEXT_MAX_CHARS,
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
        duration=300,
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
    if len(user_msg) > 2000:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "メッセージは2000文字以内で入力してください。"},
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
    with app_state.chat_history_lock:
        if chat_key in app_state.chat_history:
            app_state.chat_history.move_to_end(chat_key)
        else:
            # symbolはユーザー入力のため、プロンプトに直接埋めず構造化データとして渡す
            safe_symbol = re.sub(r"[^\w\-.^=]", "", symbol)[:15]
            app_state.chat_history[chat_key] = [
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

        if len(app_state.chat_history) > 50:
            app_state.chat_history.popitem(last=False)

        app_state.chat_history[chat_key].append({"role": "user", "content": user_msg})

        if len(app_state.chat_history[chat_key]) > 11:
            app_state.chat_history[chat_key] = [
                app_state.chat_history[chat_key][0]
            ] + app_state.chat_history[chat_key][-10:]

        messages_snapshot = list(app_state.chat_history[chat_key])

    # Mistral APIを呼び出し
    try:
        response = call_mistral_chat(
            api_key,
            messages_snapshot,
            max_tokens=1500,
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

        # contentを安全に抽出
        try:
            def _extract_content_from_response(resp):
                """共通のcontent抽出ヘルパー"""
                if isinstance(resp, dict) and "choices" in resp:
                    choice = resp["choices"][0]
                    message = choice.get("message", {})
                    return message.get("content"), choice.get("finish_reason")
                if hasattr(resp, "choices"):
                    choice = resp.choices[0]
                    return choice.message.content, getattr(
                        choice, "finish_reason", None
                    )
                return None, None

            content, finish_reason = _extract_content_from_response(response)

            ai_content = _extract_text_from_mistral_content(content)

            current_app.logger.debug(
                "api_chat content extraction result id=%s: type=%s, len=%d, finish_reason=%s",
                getattr(g, "request_id", "-"),
                type(content).__name__,
                len(ai_content) if ai_content else 0,
                finish_reason,
            )

            if not ai_content:
                current_app.logger.warning(
                    "api_chat empty content id=%s type=%s finish_reason=%s, retrying once...",
                    getattr(g, "request_id", "-"),
                    type(content).__name__,
                    finish_reason,
                )
                # トランジェントな空レスポンス対策としてキャッシュなしで1回リトライ
                retry_response = call_mistral_chat(
                    api_key,
                    messages_snapshot,
                    max_tokens=1500,
                    use_cache=False,
                    cache_key_override=f"chat_{market}_{symbol}",
                )
                retry_content, _ = _extract_content_from_response(retry_response)
                ai_content = _extract_text_from_mistral_content(retry_content)

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
        with app_state.chat_history_lock:
            if chat_key in app_state.chat_history:
                app_state.chat_history[chat_key].append(
                    {"role": "assistant", "content": ai_content}
                )

        current_app.logger.info(
            "api_chat success id=%s content_len=%d",
            getattr(g, "request_id", "-"),
            len(ai_content),
        )

        return jsonify({"reply": ai_content, "disclaimer": ANALYSIS_DISCLAIMER})

    except (RuntimeError, ValueError, KeyError, TypeError, AttributeError, OSError) as e:
        current_app.logger.error(
            "api_chat exception id=%s: %s",
            getattr(g, "request_id", "-"),
            str(e),
            exc_info=True,
        )
        return jsonify({"reply": "チャット処理に失敗しました"}), 500


@api_analysis_bp.route("/api/news", methods=["POST"])
@rate_limit(max_requests=20, window_seconds=60)
def api_news():
    """ニュースAPIエンドポイント"""
    retrieve_status = {"us": "pending", "jp": "pending", "trends": "pending"}
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

    merged_trends = []
    trends_context = ""

    try:
        try:
            fut_us_ctx = app_state.news_executor.submit(
                get_cached_context_with_negative_cache,
                f"market_news_context_us_{strategy}",
                lambda: collect_market_news_context(
                    "us", langsearch_api_key=langsearch_api_key, tavily_api_key=tavily_api_key
                ),
                300,
                90,
                False,
            )
            fut_jp_ctx = app_state.news_executor.submit(
                get_cached_context_with_negative_cache,
                f"market_news_context_jp_{strategy}",
                lambda: collect_market_news_context(
                    "jp", langsearch_api_key=langsearch_api_key, tavily_api_key=tavily_api_key
                ),
                300,
                90,
                False,
            )
            fut_us_trends = app_state.news_executor.submit(
                collect_market_trending_titles,
                "us",
                8,
                langsearch_api_key,
                tavily_api_key,
            )
            fut_jp_trends = app_state.news_executor.submit(
                collect_market_trending_titles,
                "jp",
                8,
                langsearch_api_key,
                tavily_api_key,
            )

            done, not_done = wait(
                [fut_us_ctx, fut_jp_ctx, fut_us_trends, fut_jp_trends],
                timeout=NEWS_CONTEXT_WAIT_TIMEOUT,
            )
            for fut in not_done:
                fut.cancel()
            if not_done:
                pending_targets = []
                future_targets_for_log = {
                    fut_us_ctx: "us_context",
                    fut_jp_ctx: "jp_context",
                    fut_us_trends: "us_trends",
                    fut_jp_trends: "jp_trends",
                }
                for pending_future in not_done:
                    pending_targets.append(
                        future_targets_for_log.get(pending_future, "unknown")
                    )
                current_app.logger.warning(
                    "News context gather timeout: completed=%d pending=%d timeout=%ss pending_targets=%s",
                    len(done),
                    len(not_done),
                    NEWS_CONTEXT_WAIT_TIMEOUT,
                    pending_targets,
                )

            # 収集完了後に各結果を取得 & 取得状況を追跡
            us_context = ""
            jp_context = ""
            us_trends = []
            jp_trends = []
            retrieve_status = {
                "us": "pending",
                "jp": "pending",
                "trends": "pending",
            }

            future_targets = {
                fut_us_ctx: "us_context",
                fut_jp_ctx: "jp_context",
                fut_us_trends: "us_trends",
                fut_jp_trends: "jp_trends",
            }
            for fut in done:
                target = future_targets.get(fut)
                try:
                    result = fut.result()
                    if target == "us_context":
                        us_context = result or ""
                        retrieve_status["us"] = "success" if us_context else "empty"
                    elif target == "jp_context":
                        jp_context = result or ""
                        retrieve_status["jp"] = "success" if jp_context else "empty"
                    elif target == "us_trends":
                        us_trends = result if isinstance(result, list) else []
                        retrieve_status["trends"] = (
                            "success" if (us_trends or jp_trends) else "empty"
                        )
                    elif target == "jp_trends":
                        jp_trends = result if isinstance(result, list) else []
                        retrieve_status["trends"] = (
                            "success" if (us_trends or jp_trends) else "empty"
                        )
                except (RuntimeError, ValueError, KeyError, AttributeError) as fut_exc:
                    current_app.logger.warning(
                        "Future result retrieval error (%s): %s", target, fut_exc
                    )
                    if target == "us_context":
                        retrieve_status["us"] = "error"
                    elif target == "jp_context":
                        retrieve_status["jp"] = "error"
                    elif target in ("us_trends", "jp_trends"):
                        retrieve_status["trends"] = "error"
                    continue

            # 完了しなかったタスクはタイムアウト状態
            for fut in not_done:
                target = future_targets.get(fut)
                if target == "us_context":
                    retrieve_status["us"] = "timeout"
                elif target == "jp_context":
                    retrieve_status["jp"] = "timeout"
                elif target in ("us_trends", "jp_trends"):
                    retrieve_status["trends"] = "timeout"

            seen_trends = set()
            merged_trends = []
            for title in list(us_trends) + list(jp_trends):
                t = str(title or "").strip()
                key = t.lower()
                if not t or key in seen_trends:
                    continue
                seen_trends.add(key)
                merged_trends.append(t)
                if len(merged_trends) >= 12:
                    break
            trends_context = "\n".join(f"- {title}" for title in merged_trends)

        except (RuntimeError, ValueError, KeyError) as ctx_err:
            current_app.logger.warning("News context gather error: %s", ctx_err)
            us_context = ""
            jp_context = ""
            merged_trends = []
            trends_context = ""
            retrieve_status = {
                "us": "error",
                "jp": "error",
                "trends": "error",
            }

        instructions = (
            "あなたは金融市場の専門アナリストです。\n"
            "提供される情報を元に、簡潔だが具体性のある要約を提供してください。\n"
            "必ずJSONのみを返してください。\n"
            "各セクション（us, jp, trends）は完全に独立していて、他のセクションの情報を混ぜないでください。\n"
            "各行は1つの事実を述べ、出来事・背景・市場への影響の少なくとも2要素を含めてください。\n"
            "見出しの単語列挙ではなく、分析文として書いてください。"
        )

        combined_prompt = (
            "以下の3つの情報をそれぞれ独立して要約し、混ぜないでください。\n\n"
            "【US市場情報】\n"
            f"{us_context or 'データなし'}\n\n"
            "【日本市場情報】\n"
            f"{jp_context or 'データなし'}\n\n"
            "【トレンド情報】\n"
            f"{trends_context or 'データなし'}\n\n"
            "返すJSONは次の形式でなければなりません。原則はJSONオブジェクトのみを返してください。\n"
            "必要な場合のみ ```json ... ``` で囲っても構いません。前置き説明は禁止です。\n"
            "重要: us/jp/trends の値は必ず文字列にしてください。配列やオブジェクトは禁止です。\n"
            "文字列内で 6-8 行を改行区切りで書いてください（1行=1話題）。\n"
            "各行は30〜80文字程度で、何が起きたか / なぜ重要か / 市場への影響 をできるだけ含めてください。\n"
            "重要: 出力は『分析要約』のみ。ニュース見出しの生引用、source/date/url、HTMLタグ、URL文字列は絶対に含めないでください。\n"
            '{"us":"1行目\\n2行目","jp":"1行目\\n2行目","trends":"1行目\\n2行目"}'
        )

        # ニュース要約は通常5分キャッシュするが、force=true では LLM 出力キャッシュを無視して最新化する。
        # コンテキスト側のみをキャッシュし、LLM出力は再利用しない。
        llm_hash_src = f"{us_context}|{jp_context}|{trends_context}".encode(
            "utf-8", errors="ignore"
        )
        llm_hash = hashlib.sha256(llm_hash_src).hexdigest()
        current_app.logger.info(
            "News bundle refresh id=%s context_hash=%s",
            getattr(g, "request_id", "-"),
            llm_hash[:12],
        )
        current_app.logger.info(
            "News prompt prepared id=%s us_chars=%s jp_chars=%s trends_titles=%s",
            getattr(g, "request_id", "-"),
            len(us_context or ""),
            len(jp_context or ""),
            len(merged_trends or []),
        )

        def _generate_news_bundle():
            combined_res = call_mistral_chat(
                api_key,
                [
                    SystemMessage(content=instructions),
                    UserMessage(content=combined_prompt),
                ],
                1500,
                use_cache=False,
                response_format=NewsSummaryModel,
                cache_key_override="news_summary_v3_structured",
                reasoning_effort="none",
            )

            try:
                if isinstance(combined_res, dict) and "choices" in combined_res:
                    choice = combined_res["choices"][0]
                    message_data = (
                        choice.get("message", {}) if isinstance(choice, dict) else {}
                    )
                    # Prefer 'parsed' (promoted dict from Pydantic parse), fall back to 'content'
                    payload = message_data.get("parsed") or message_data.get("content")
                    # Normalise: convert JSON strings to dict
                    if isinstance(payload, str):
                        try:
                            payload = json.loads(payload)
                        except json.JSONDecodeError:
                            current_app.logger.warning(
                                "News bundle: failed to JSON-decode content string. Falling back."
                            )
                            payload = {}
                    # Handle Pydantic model objects that slipped through
                    if hasattr(payload, "model_dump"):
                        payload = payload.model_dump()
                    elif hasattr(payload, "__dict__") and not isinstance(payload, dict):
                        payload = vars(payload)
                    if isinstance(payload, dict):
                        return {
                            "us": str(payload.get("us") or ""),
                            "jp": str(payload.get("jp") or ""),
                            "trends": str(payload.get("trends") or ""),
                        }
                return {"us": "解析中...", "jp": "解析中...", "trends": "解析中..."}
            except (json.JSONDecodeError, ValueError, TypeError, AttributeError, KeyError) as parse_err:
                current_app.logger.warning(
                    "News bundle structured parse failed: %s", parse_err
                )
                return {"us": "解析エラー", "jp": "解析エラー", "trends": "解析エラー"}

        def _is_valid_news_bundle(bundle):
            if not isinstance(bundle, dict):
                return False
            for k in ("us", "jp", "trends"):
                v = bundle.get(k)
                if not v or "解析中..." in v or "解析エラー" in v:
                    return False
            return True

        if force_refresh:
            current_app.logger.info(
                "News bundle force refresh requested id=%s context_hash=%s",
                getattr(g, "request_id", "-"),
                llm_hash[:12],
            )
            news_bundle = _generate_news_bundle()
        else:
            news_bundle = get_cached(
                f"news_bundle_llm_{llm_hash}",
                _generate_news_bundle,
                duration=300,
                valid_func=_is_valid_news_bundle,
            )

        if not isinstance(news_bundle, dict):
            news_bundle = {"us": "", "jp": "", "trends": ""}

        us_text = NewsFormatter._normalize_mistral_news_lines(
            news_bundle.get("us") or ""
        )
        jp_text = NewsFormatter._normalize_mistral_news_lines(
            news_bundle.get("jp") or ""
        )
        trends_text = NewsFormatter._normalize_mistral_news_lines(
            news_bundle.get("trends") or ""
        )

        # トレンドバッジの同期用
        raw_trending = []
        if merged_trends:
            raw_trending = merged_trends[:]

        now_iso = datetime.now(timezone.utc).isoformat()

        return jsonify(
            {
                "us": {
                    "content": us_text,
                    "timestamp": now_iso,
                    "status": retrieve_status.get("us", "unknown"),
                },
                "jp": {
                    "content": jp_text,
                    "timestamp": now_iso,
                    "status": retrieve_status.get("jp", "unknown"),
                },
                "trends": {
                    "content": trends_text,
                    "timestamp": now_iso,
                    "status": retrieve_status.get("trends", "unknown"),
                },
                "trending_raw": raw_trending,
                "retrieve_status": retrieve_status,
                "disclaimer": ANALYSIS_DISCLAIMER,
            }
        )
    except (requests.RequestException, ValueError, KeyError, RuntimeError) as exc:
        current_app.logger.error("News API error: %s", exc)
        return error_response(ErrorCode.INTERNAL_SERVER_ERROR, status_code=500)


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
                max_tokens=2500,
                use_cache=False,
                response_format=StockAnalysis,
                cache_key_override="analyze_system_v2_pydantic",
                reasoning_effort="none",
            )
        except (RuntimeError, ConnectionError, OSError) as api_err:
            current_app.logger.error("Analyze-v2 API call failed: %s", api_err)
            return jsonify(
                build_fallback_analysis_result("AI解析APIエラー: API呼び出しに失敗しました")
            ), 200

        # Extract structured result
        result = None
        if isinstance(response, dict) and response.get("choices"):
            msg = response["choices"][0].get("message", {})
            # call_mistral_chat promotes 'parsed' to 'content' for Pydantic models
            result = msg.get("content")

            if not isinstance(result, dict):
                # Fallback to extraction if not automatically parsed
                content = extract_chat_content(response)
                if content:
                    try:
                        repaired_result, _ = repair_analysis_json_with_llm(
                            api_key, content
                        )
                        result = repaired_result
                    except (json.JSONDecodeError, ValueError, TypeError, RuntimeError) as e:
                        current_app.logger.warning(
                            "Analyze-v2 extraction-repair failed: %s", e
                        )

        if not result:
            current_app.logger.error(
                "Analyze-v2 failed to produce result after all attempts"
            )
            return jsonify(
                build_fallback_analysis_result("AI解析の生成に失敗しました")
            ), 200

        # Success! Validate and normalize
        valid, reason = validate_analysis_result(result)
        if not valid:
            current_app.logger.info(
                "Analyze-v2 result validation failed (%s); attempting final repair",
                reason,
            )
            try:
                # result が既にある場合はそれを文字列化して修復にかける
                repaired_result, _ = repair_analysis_json_with_llm(
                    api_key, json.dumps(result)
                )
                result = repaired_result
            except (json.JSONDecodeError, ValueError, TypeError, RuntimeError) as e:
                current_app.logger.warning(
                    "Analyze-v2 final validation-repair failed: %s", e
                )

        if not result:
            return jsonify(
                build_fallback_analysis_result("AI解析の検証に失敗しました")
            ), 200

        result = normalize_analysis_result(result)
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
        with app_state.chat_history_lock:
            if chat_key in app_state.chat_history:
                app_state.chat_history.move_to_end(chat_key)
            else:
                app_state.chat_history[chat_key] = [
                    {
                        "role": "system",
                        "content": f"あなたは{symbol}銘柄の専門家です。簡潔かつ投資家に有益な回答をしてください。",
                    }
                ]

            if len(app_state.chat_history) > 50:
                app_state.chat_history.popitem(last=False)

            app_state.chat_history[chat_key].append(
                {
                    "role": "assistant",
                    "content": f"分析サマリー（v2）: {result.get('analysis_summary')}",
                }
            )

            if len(app_state.chat_history[chat_key]) > 11:
                app_state.chat_history[chat_key] = [
                    app_state.chat_history[chat_key][0]
                ] + app_state.chat_history[chat_key][-10:]
        return jsonify(result)
    except (RuntimeError, ValueError, KeyError, TypeError, AttributeError, OSError) as e:
        current_app.logger.error("Analyze-v2 unexpected error: %s", e)
        return error_response(ErrorCode.INTERNAL_SERVER_ERROR, status_code=500)
