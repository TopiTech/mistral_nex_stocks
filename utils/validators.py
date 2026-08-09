"""
Validation utilities for the application.
"""

# pylint: disable=cyclic-import

import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from constants import (
    PORTFOLIO_AVG_PRICE_MAX,
    PORTFOLIO_SHARES_MAX,
    PORTFOLIO_TOTAL_VALUE_MAX,
)
from utils.normalization import is_valid_symbol

logger = logging.getLogger(__name__)


class MnsValidationError(ValueError):
    """Custom validation exception for Mistral NeX Stocks."""


class NewsSummaryModel(BaseModel):
    """ニュース要約用の3セクション構造化モデル"""

    us: str = Field(description="US市場の要約文 (複数行)")
    jp: str = Field(description="日本市場の要約文 (複数行)")
    trends: str = Field(description="トレンド情報の要約文 (複数行)")


class StockAnalysis(BaseModel):
    """個別銘柄のAI分析結果用の構造化モデル (2026仕様)"""

    recommendation: str = Field(
        description="Investment recommendation",
        pattern="^(強い買い|買い|中立|売り|強い売り)$",
    )
    sentiment: str = Field(description="Market sentiment", pattern="^(強気|中立|弱気)$")
    target_price_3m: float = Field(description="3-month target price")
    upside_3m: str = Field(description="3-month upside percentage, e.g. '+10%'")
    confidence: str = Field(description="Analysis confidence level", pattern="^(高|中|低)$")
    analysis_summary: str = Field(description="100-character summary of analysis")
    key_catalysts: list[str] = Field(description="Key catalysts (up to 3 items)", max_length=3)
    risk_factors: list[str] = Field(description="Risk factors (up to 2 items)", max_length=2)
    technical_analysis: str = Field(description="Technical analysis summary (50 chars max)")
    fundamental_analysis: str = Field(description="Fundamental analysis summary (50 chars max)")
    latest_news_impact: str = Field(description="Impact of latest news (90 chars max)")


class SearchServiceStatusSchema(BaseModel):
    """Schema for validating search service health status."""

    tavily_ok: bool
    ddgs_ok: bool
    news_cache_keys: int


class DefaultSymbolsSchema(BaseModel):
    """Schema for default symbols passed to templates."""

    us: list[str]
    jp: list[str]
    idx: list[str]


class AppConfigSchema(BaseModel):
    """Schema for app config passed to templates."""

    has_mistral_api_key: bool
    has_langsearch_api_key: bool
    has_tavily_api_key: bool
    has_alphavantage_api_key: bool
    mistral_model: str
    is_ai_technical_lines_eligible: bool


class PortfolioInputSchema(BaseModel):
    """Schema for validating portfolio input parameters."""

    symbol: str
    market: str
    shares: float
    avg_price: float
    avg_fx_rate: float | None = None

    @field_validator("shares", "avg_price", "avg_fx_rate", mode="before")
    @classmethod
    def reject_boolean_numeric(cls, v: Any) -> Any:
        """Reject boolean values for numeric fields."""
        if isinstance(v, bool):
            raise MnsValidationError("bool_type_not_allowed")
        return v

    @model_validator(mode="after")
    def validate_bounds_and_total(self) -> "PortfolioInputSchema":
        """Validate logical bounds and calculate total value."""
        # shares validation
        if self.shares < 0:
            raise MnsValidationError("sharesは非負の数値である必要があります")
        if self.shares > PORTFOLIO_SHARES_MAX:
            raise MnsValidationError(f"sharesは{PORTFOLIO_SHARES_MAX:,}以下である必要があります")

        # avg_price validation
        if self.avg_price < 0:
            raise MnsValidationError("avg_priceは非負の数値である必要があります")
        if self.avg_price > PORTFOLIO_AVG_PRICE_MAX:
            raise MnsValidationError(
                f"avg_priceは{PORTFOLIO_AVG_PRICE_MAX:,}以下である必要があります"
            )

        # avg_fx_rate validation
        if self.avg_fx_rate is not None:
            if self.avg_fx_rate <= 0:
                raise MnsValidationError("avg_fx_rateは正の数値である必要があります")
            if self.avg_fx_rate > 1_000_000:
                raise MnsValidationError("avg_fx_rateは1,000,000以下である必要があります")

        # total value validation
        total = self.shares * self.avg_price
        if total > PORTFOLIO_TOTAL_VALUE_MAX:
            raise MnsValidationError(
                f"ポートフォリオ総額は{PORTFOLIO_TOTAL_VALUE_MAX:,}以下である必要があります"
            )

        return self


def validate_portfolio_input(shares, avg_price, avg_fx_rate=None):
    """ポートフォリオ入力の厳格な検証"""
    errors = []
    try:
        PortfolioInputSchema(
            symbol="dummy",
            market="us",
            shares=shares,
            avg_price=avg_price,
            avg_fx_rate=avg_fx_rate,
        )
    except (MnsValidationError, ValueError, TypeError) as exc:
        if hasattr(exc, "errors"):
            for err in exc.errors():
                msg = err.get("msg")
                if "Value error, " in msg:
                    msg = msg.replace("Value error, ", "")
                if msg == "bool_type_not_allowed" or "Input should be a valid number" in msg:
                    loc = err.get("loc", [None])[0]
                    if loc == "avg_fx_rate":
                        errors.append("avg_fx_rateは正の数値である必要があります")
                    else:
                        errors.append(f"{loc}は非負の数値である必要があります")
                else:
                    errors.append(msg)
        else:
            errors.append(str(exc))
    return errors


def _normalize_content_list_for_history(content: list) -> list:
    """Normalize assistant content chunks for serializable history storage.

    Keeps both text and thinking chunks so reasoning-capable models can replay
    the full assistant message on subsequent turns. Non-serializable objects are
    converted to string values as a safe fallback.
    """
    normalized = []
    for chunk in content:
        if isinstance(chunk, dict):
            normalized.append(chunk)
            continue
        if isinstance(chunk, str):
            normalized.append({"type": "text", "text": chunk})
            continue
        if hasattr(chunk, "model_dump"):
            normalized.append(chunk.model_dump())
            continue
        normalized.append({"type": "text", "text": str(chunk)})
    return normalized


def extract_chat_content(response, preserve_for_history: bool = False):
    """
    Chat Completions レスポンス用（/v1/chat/completions）。
    Mistral APIが返すcontentフォーマット:
    - string: 'text content here'
    - list: [{'type': 'text', 'text': '...'}, ...]

    preserve_for_history=True の場合、reasoning対応モデルで返却される
    thinkingチャンクもJSON化して保持する。表示用途には従来どおり
    最終テキストのみを返す。
    """
    if not response:
        return "応答が空です"
    if isinstance(response, dict) and response.get("object") == "error":
        return response.get("message") or json.dumps(response, ensure_ascii=False)
    if isinstance(response, dict) and "error" in response:
        err = response["error"]
        if isinstance(err, dict):
            return err.get("message") or json.dumps(err, ensure_ascii=False)
        return str(err)

    try:
        # First, log the response structure for debugging
        logger.debug(
            "extract_chat_content: response type=%s, has_choices=%s",
            type(response).__name__,
            ("choices" in response if isinstance(response, dict) else hasattr(response, "choices")),
        )

        # Handle both dict and object responses
        if isinstance(response, dict):
            choices = response.get("choices")
        elif hasattr(response, "choices"):
            choices = response.choices
        else:
            choices = None

        if not choices:
            logger.warning(
                "extract_chat_content: no choices in response: %s",
                json.dumps(response, ensure_ascii=False)[:500],
            )
            return f"Unexpected response: {json.dumps(response, ensure_ascii=False)[:500]}"

        # Get message from choice
        first_choice = choices[0]
        if isinstance(first_choice, dict):
            message = first_choice.get("message", {})
        elif hasattr(first_choice, "message"):
            message = first_choice.message
        else:
            message = {}

        # Get content from message
        if isinstance(message, dict):
            content = message.get("content")
        elif hasattr(message, "content"):
            content = message.content
        else:
            content = None

        # 2026 Structured Outputs: content might be a BaseModel instance
        if isinstance(content, BaseModel):
            return content.model_dump_json()

        logger.debug(
            "extract_chat_content: content_type=%s, content_repr=%s",
            type(content).__name__,
            repr(content)[:200] if content else "None",
        )

        # Case 1: content is a string (most common)
        if isinstance(content, str):
            if content:
                return content.strip()
            logger.warning("extract_chat_content: empty string content")
            return "(空の応答が返されました)"

        # Case 2: content is a list of chunks
        if isinstance(content, list):
            texts = []
            logger.debug(
                "extract_chat_content: processing list with %d chunks",
                len(content),
            )
            for idx, chunk in enumerate(content):
                logger.debug(
                    "extract_chat_content: chunk[%d] type=%s, keys=%s",
                    idx,
                    type(chunk).__name__,
                    list(chunk.keys()) if isinstance(chunk, dict) else "N/A",
                )
                if isinstance(chunk, dict):
                    # First, try to extract directly from 'text' field (most common)
                    text_val = chunk.get("text")
                    if isinstance(text_val, str) and text_val:
                        texts.append(text_val)
                        continue

                    # Then check if it's a specifically typed chunk
                    chunk_type = chunk.get("type")
                    if chunk_type == "text":
                        # Already handled above, but in case structure is different
                        text_val = chunk.get("text") or chunk.get("value")
                        if isinstance(text_val, str) and text_val:
                            texts.append(text_val)
                    elif chunk_type == "citation":
                        # Skip citations for chat responses
                        pass
                    elif chunk_type == "reference":
                        # Skip references
                        pass
                    elif chunk_type == "thinking":
                        # 2026 specifications: Skip thinking/CoT process for final output
                        pass
                    else:
                        # For unknown types, log for debugging
                        logger.debug(
                            "extract_chat_content: skipping unknown chunk type: %s",
                            chunk_type,
                        )
                elif isinstance(chunk, str):
                    # Handle direct string chunks
                    if chunk:
                        texts.append(chunk)
                else:
                    # Handle Python objects (from Mistral SDK)
                    try:
                        # Try to get text attribute from object
                        if hasattr(chunk, "text"):
                            text_val = chunk.text
                            if isinstance(text_val, str) and text_val:
                                texts.append(text_val)
                                continue

                        # Try common attribute names
                        for attr in ["value", "content"]:
                            if hasattr(chunk, attr):
                                val = getattr(chunk, attr)
                                if isinstance(val, str) and val:
                                    texts.append(val)
                                    continue

                        # Last resort: log object type for debugging
                        logger.debug(
                            "extract_chat_content: unhandled object type: %s",
                            type(chunk).__name__,
                        )
                    except Exception as e:
                        logger.debug(
                            "extract_chat_content: error processing object: %s",
                            str(e),
                        )

            result = "".join(texts).strip()
            if result:
                return result

            logger.warning(
                "extract_chat_content: list chunks but no text extracted. content: %s",
                json.dumps(content, ensure_ascii=False)[:300],
            )
            return "(テキストの抽出に失敗しました)"

        # Case 3: content is a dict (shouldn't happen in normal chat, but handle it)
        if isinstance(content, dict):
            # type が 'json_object' で value が辞書の場合、value の中身を抽出する
            if content.get("type") == "json_object" and isinstance(content.get("value"), dict):
                content = content["value"]

            # Try to extract text field
            elif "text" in content and isinstance(content.get("text"), str):
                text = content["text"].strip()
                if text:
                    return text
            # Try json serialization as fallback
            try:
                return json.dumps(content, ensure_ascii=False)
            except (TypeError, ValueError):
                return str(content)

        # Case 4: content is None or missing
        if content is None:
            logger.warning(
                "extract_chat_content: content is None, message=%s",
                repr(message)[:200],
            )
            return "(応答が返されませんでした)"

        # Case 5: Unexpected type
        logger.warning(
            "extract_chat_content: unexpected content type: %s",
            type(content).__name__,
        )
        return f"(不予期の応答形式: {type(content).__name__})"

    except (ValueError, TypeError, KeyError) as exc:
        logger.exception("extract_chat_content: exception")
        return f"(応答解析に失敗しました: {exc})"


def extract_json_payload(content, required_fields=None):
    """
    AIの出力から最初の JSON オブジェクトを抽出・自動修復。
    1. Direct dict/list check
    2. Markdown fence (```json ... ```) から抽出試行
    3. 構造化スタック追跡（{ および [）による領域特定および文法修復
    4. 途切れ（Truncation）時の自動補完・末尾ゴミ切り落とし（Progressive Truncation Salvage）
    5. コントロール文字・改行・末尾カンマ・引用符の修復試行
    6. トークン/正規表現による必須フィールド救済 (Fallback)
    """
    if isinstance(content, (dict, list)):
        return json.dumps(content, ensure_ascii=False)
    text = (content or "").strip()
    if not text:
        raise ValueError("空の応答です")

    def _clean_json_str(s: str) -> str:
        s = s.strip()
        # 末尾カンマの削除 (..., } -> ... }, ..., ] -> ... ])
        s = re.sub(r",\s*([\]}])", r"\1", s)
        # 制御文字コードの除去
        s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
        return s

    def _try_json_parse(s: str):
        if not s:
            return None, s
        s = _clean_json_str(s)
        # 1. Standard json.loads
        try:
            obj = json.loads(s)
            return obj, s
        except (json.JSONDecodeError, TypeError):
            pass

        # 2. strict=False (allows raw newlines inside string literals)
        try:
            obj = json.loads(s, strict=False)
            return obj, json.dumps(obj, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            pass

        # 3. Single quote keys fix
        try:
            sq_fixed = re.sub(r"'([a-zA-Z0-9_]+)'\s*:", r'"\1":', s)
            obj = json.loads(sq_fixed, strict=False)
            return obj, json.dumps(obj, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            pass

        # 4. Escape unescaped newlines inside strings
        try:
            def _escape_newlines(m):
                return m.group(0).replace("\n", "\\n").replace("\r", "\\r")

            escaped = re.sub(r'"([^"\\]|\\.)*"', _escape_newlines, s, flags=re.DOTALL)
            obj = json.loads(escaped, strict=False)
            return obj, json.dumps(obj, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            pass

        return None, s

    # Stage 0: Direct full text parse
    obj, fixed_s = _try_json_parse(text)
    if obj is not None:
        return fixed_s

    # Stage 1: Markdown fence
    match_fence = re.search(r"```(?:json)?\s*(.*?)\s*(?:```|$)", text, re.DOTALL)
    if match_fence:
        candidate = match_fence.group(1).strip()
        obj, fixed_s = _try_json_parse(candidate)
        if obj is not None:
            return fixed_s
        text = candidate

    # Truncate text only for progressive salvage stages if unusually large to avoid CPU exhaustion
    if len(text) > 100000:
        text = text[:100000]

    # Stage 2 & 3: Stack-based balance search & salvage
    first_idx = -1
    for idx, ch in enumerate(text):
        if ch in ("{", "["):
            first_idx = idx
            break

    if first_idx != -1:
        sub_text = text[first_idx:]
        obj, fixed_s = _try_json_parse(sub_text)
        if obj is not None:
            return fixed_s

        # Deep stack tracking
        stack = []
        in_str = False
        escape = False

        for i, ch in enumerate(sub_text):
            if escape:
                escape = False
                continue
            if ch == "\\" and in_str:
                escape = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if not in_str:
                if ch in ("{", "["):
                    stack.append("}" if ch == "{" else "]")
                elif ch in ("}", "]") and stack and stack[-1] == ch:
                    stack.pop()
                    if not stack:
                        candidate = sub_text[: i + 1]
                        obj_c, fixed_c = _try_json_parse(candidate)
                        if obj_c is not None:
                            return fixed_c

        # Truncation & malformed boundary salvage
        working = sub_text.rstrip()

        # Step A: Direct closure of open string and stack delimiters
        if in_str:
            clean_working = working.removesuffix("\\")
            simple_salvage = clean_working + '"' + "".join(reversed(stack))
        else:
            simple_salvage = working + "".join(reversed(stack))

        obj_simple, fixed_simple = _try_json_parse(simple_salvage)
        if obj_simple is not None:
            logger.info("JSON salvaged by direct stack closure")
            return fixed_simple

        # Step B: Progressive cutback from the end to salvage complete elements
        if in_str:
            working = working.removesuffix("\\")
            working += '"'

        for attempt_len in range(len(working) - 1, 0, -1):
            chunk = working[:attempt_len].rstrip()
            chunk = re.sub(r',?\s*"?[a-zA-Z0-9_]*"?\s*:?\s*$', "", chunk).rstrip()
            if not chunk:
                break

            rem_stack = []
            st_in_str = False
            st_escape = False
            for ch in chunk:
                if st_escape:
                    st_escape = False
                    continue
                if ch == "\\" and st_in_str:
                    st_escape = True
                    continue
                if ch == '"':
                    st_in_str = not st_in_str
                    continue
                if not st_in_str:
                    if ch in ("{", "["):
                        rem_stack.append("}" if ch == "{" else "]")
                    elif ch in ("}", "]") and rem_stack and rem_stack[-1] == ch:
                        rem_stack.pop()

            salvaged = chunk
            if st_in_str:
                salvaged += '"'
            salvaged += "".join(reversed(rem_stack))

            obj_s, fixed_s = _try_json_parse(salvaged)
            if obj_s is not None:
                logger.info(
                    "JSON salvaged by progressive stack closure (removed %d trailing chars)",
                    len(working) - len(chunk),
                )
                return fixed_s

    # Stage 4: Token/Regex extraction fallback
    fields_to_check = list(required_fields or ["recommendation", "sentiment", "target_price_3m"])
    matched_fields = [f for f in fields_to_check if f'"{f}"' in text or f"'{f}'" in text]
    if matched_fields:
        try:
            recovered = {}
            for f in set(fields_to_check + ["summary", "trend_bias", "analysis_summary", "lines"]):
                m = re.search(rf'"{f}"\s*:\s*"([^"]*)"', text)
                if m:
                    recovered[f] = m.group(1)[:2000]
                else:
                    m_arr = re.search(rf'"{f}"\s*:\s*(\[[^\]]*\]|\{{[^\}}]*\}})', text)
                    if m_arr:
                        try:
                            recovered[f] = json.loads(m_arr.group(1), strict=False)
                        except (json.JSONDecodeError, ValueError, TypeError) as exc:
                            logger.debug(
                                "Failed to parse field '%s' JSON during salvage: %s", f, exc
                            )
            if recovered:
                logger.info("JSON salvaged by partial token/regex field extraction")
                return json.dumps(recovered, ensure_ascii=False)
        except (ValueError, TypeError, re.error, json.JSONDecodeError) as exc:
            logger.debug("Failed to salvage JSON manually: %s", exc)

    snippet = text.replace("\n", " ").replace("\r", " ").strip()
    if len(snippet) > 200:
        snippet = snippet[:200] + "..."
    raise ValueError(
        f"JSONブロックの抽出に失敗しました (構文エラーの可能性あり)。入力先頭: {snippet}"
    )


def normalize_analysis_result(result: Any) -> dict[str, Any]:
    """Ensures all expected keys are present in the AI analysis result dictionary."""
    normalized = dict(result or {})
    defaults = {
        "recommendation": "中立",
        "sentiment": "中立",
        "target_price_3m": 0,
        "upside_3m": "0%",
        "confidence": "低",
        "analysis_summary": "データが不足しているため保守的に中立判定",
        "key_catalysts": [],
        "risk_factors": [],
        "technical_analysis": "データ不足",
        "fundamental_analysis": "データ不足",
        "latest_news_impact": "データ不足",
    }
    for key, value in defaults.items():
        if key not in normalized or normalized[key] in (None, ""):
            normalized[key] = value

    if not isinstance(normalized.get("key_catalysts"), list):
        normalized["key_catalysts"] = [str(normalized.get("key_catalysts"))]
    if not isinstance(normalized.get("risk_factors"), list):
        normalized["risk_factors"] = [str(normalized.get("risk_factors"))]

    # Ensure the fallback flag is always present so consumers can unambiguously
    # distinguish a real analysis from a generated fallback (the fallback path
    # sets fallback_used=True in build_fallback_analysis_result).
    normalized["fallback_used"] = bool(normalized.get("fallback_used", False))

    return normalized


def validate_analysis_result(result):
    """Lightweight validation for AI analysis results.

    Accepts partial results (LLM function-calling may return a subset). Returns
    (True, "") when the result is considered usable; otherwise (False, reason).
    """
    if not isinstance(result, dict):
        return False, "result is not an object"

    # Require at least one core field to consider the result usable.
    core_fields = ("analysis_summary", "recommendation", "sentiment", "target_price_3m")
    if not any(k in result for k in core_fields):
        return False, "missing core analysis fields"

    # If specific fields are present, sanity-check their types (non-blocking)
    if "target_price_3m" in result:
        tpm = result.get("target_price_3m")
        if not isinstance(tpm, (int, float)):
            try:
                float(str(tpm))
            except (ValueError, TypeError):
                return False, "target_price_3m must be numeric"

    if "key_catalysts" in result and not isinstance(result.get("key_catalysts"), list):
        return False, "key_catalysts must be an array"
    if "risk_factors" in result and not isinstance(result.get("risk_factors"), list):
        return False, "risk_factors must be an array"

    return True, ""


def safe_parse_analysis_result(
    response: Any, api_key: str, repair_func: Any = None
) -> dict[str, Any]:
    """Safely extracts, repairs, validates, and normalizes AI stock analysis results."""
    if repair_func is None:
        from services.ai_service import repair_analysis_json_with_llm

        repair_func = repair_analysis_json_with_llm
    from utils.formatting import build_fallback_analysis_result

    result = None
    if isinstance(response, dict) and response.get("choices"):
        msg = response["choices"][0].get("message", {})
        result = msg.get("content")

        if not isinstance(result, dict):
            # Fallback to extraction from string content
            content = extract_chat_content(response)
            if content:
                try:
                    repaired_result, _ = repair_func(api_key, content)
                    result = repaired_result
                except Exception as e:
                    logger.warning("safe_parse_analysis_result extraction-repair failed: %s", e)

    if not result:
        logger.error("safe_parse_analysis_result failed to extract result")
        return build_fallback_analysis_result("AI解析の生成に失敗しました")

    valid, reason = validate_analysis_result(result)
    if not valid:
        logger.info(
            "safe_parse_analysis_result validation failed (%s); attempting final repair", reason
        )
        try:
            repaired_result, _ = repair_func(api_key, json.dumps(result))
            result = repaired_result
        except Exception as e:
            logger.warning("safe_parse_analysis_result final validation-repair failed: %s", e)

    if not result:
        return build_fallback_analysis_result("AI解析の検証に失敗しました")

    return normalize_analysis_result(result)


class AiPortfolioItemSchema(BaseModel):
    """Schema for individual stock holding in AI Portfolio."""

    model_config = ConfigDict(allow_inf_nan=False, str_strip_whitespace=True)

    symbol: str = Field(min_length=1, max_length=15)
    market: Literal["us", "jp"] = "us"
    weight_pct: float = Field(gt=0, le=100)
    target_price: float | None = Field(default=None, gt=0, le=PORTFOLIO_AVG_PRICE_MAX)
    rationale: str = Field(min_length=1, max_length=500)
    risk_level: Literal["low", "mid", "high"] = "mid"

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        symbol = value.strip().upper()
        if not is_valid_symbol(symbol):
            raise ValueError("invalid stock symbol")
        return symbol

    @field_validator("market", "risk_level", mode="before")
    @classmethod
    def normalize_choice(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("weight_pct", "target_price", mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("boolean is not a valid number")  # noqa: TRY004
        return value


class AiPortfolioResponseSchema(BaseModel):
    """Schema for complete AI Portfolio response."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1, max_length=500)
    risk_level: str = Field(default="中リスク", min_length=1, max_length=500)
    expected_return: str = Field(default="8-12%", min_length=1, max_length=500)
    commentary: str = Field(default="", max_length=500)
    items: list[AiPortfolioItemSchema] = Field(min_length=1, max_length=20)
