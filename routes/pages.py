from pathlib import Path

from flask import Blueprint, current_app, render_template, send_from_directory

from credential_manager import get_api_credential_state, get_model_badge
from schemas.config import TemplateConfigSchema
from utils.stock_payload import get_default_symbols
from utils.validators import DefaultSymbolsSchema

pages_bp = Blueprint("pages", __name__)


@pages_bp.route("/favicon.ico")
def favicon():
    """favicon.ico の直接参照を許可する"""
    root_favicon = Path(current_app.root_path) / "favicon.ico"
    if root_favicon.exists():
        return send_from_directory(current_app.root_path, "favicon.ico")
    static_folder = current_app.static_folder or str(Path(current_app.root_path) / "static")
    return send_from_directory(static_folder, "favicon.ico")


@pages_bp.route("/.well-known/appspecific/com.chrome.devtools.json")
def chrome_devtools_discovery():
    """Chrome DevTools の自動検出プローブに空のJSON (200 OK) を返す"""
    return current_app.response_class("{}", mimetype="application/json")


def _get_safe_template_context() -> tuple[dict, dict]:
    try:
        safe_symbols = DefaultSymbolsSchema.model_validate(get_default_symbols()).model_dump()
    except Exception as exc:
        current_app.logger.warning("Failed to validate default symbols schema: %s", exc)
        safe_symbols = {"us": [], "jp": [], "idx": []}

    try:
        safe_config = TemplateConfigSchema.model_validate(get_api_credential_state()).model_dump()
    except Exception as exc:
        current_app.logger.warning("Failed to validate app config schema: %s", exc)
        # Historical builder fallback (pinned by
        # tests/test_code_review_goal_fixes_2026.py::test_routes_pages_safe_template_context_error_boundary).
        # Render paths must upgrade it via _template_config_for_render below.
        safe_config = {}

    return safe_symbols, safe_config


def _template_config_for_render(safe_config: dict) -> dict:
    """API-8: upgrade the degraded ``{}`` fallback to a typed contract.

    On schema failure the context builder returns ``{}`` (pinned contract);
    templates read ``app_config.*`` directly, so rendering that empty dict
    makes /setup and /settings render "no API key configured" wizards even
    when a key exists. This render-boundary helper supplies the minimum
    template contract plus an additive ``config_error`` flag — booleans and
    empty defaults only, no secrets can leak.
    """
    if safe_config:
        return safe_config
    fallback = TemplateConfigSchema(
        has_mistral_api_key=False,
        has_langsearch_api_key=False,
        has_tavily_api_key=False,
        has_alphavantage_api_key=False,
        mistral_model="",
        is_ai_technical_lines_eligible=False,
    ).model_dump()
    fallback["config_error"] = True
    return fallback


@pages_bp.route("/")
@pages_bp.route("/setup")
def setup():
    """セットアップページを表示する"""
    safe_symbols, safe_config = _get_safe_template_context()
    return render_template(
        "setup.html",
        model_badge=get_model_badge(),
        default_symbols=safe_symbols,
        app_config=_template_config_for_render(safe_config),
    )


@pages_bp.route("/main")
def main_page():
    """メインページを表示する"""
    safe_symbols, safe_config = _get_safe_template_context()
    return render_template(
        "index.html",
        model_badge=get_model_badge(),
        default_symbols=safe_symbols,
        app_config=_template_config_for_render(safe_config),
    )


@pages_bp.route("/heatmap")
def heatmap_page():
    """ヒートマップページを表示する"""
    safe_symbols, safe_config = _get_safe_template_context()
    return render_template(
        "heatmap.html",
        model_badge=get_model_badge(),
        default_symbols=safe_symbols,
        app_config=_template_config_for_render(safe_config),
    )


@pages_bp.route("/screener")
def screener_page():
    """簡易スクリーナーページを表示する"""
    safe_symbols, safe_config = _get_safe_template_context()
    return render_template(
        "screener.html",
        model_badge=get_model_badge(),
        default_symbols=safe_symbols,
        app_config=_template_config_for_render(safe_config),
    )


@pages_bp.route("/settings")
def settings_page():
    """設定ページを表示する"""
    safe_symbols, safe_config = _get_safe_template_context()
    return render_template(
        "settings.html",
        model_badge=get_model_badge(),
        default_symbols=safe_symbols,
        app_config=_template_config_for_render(safe_config),
    )


@pages_bp.route("/experimental/orbit")
def experimental_orbit_page():
    """実験的表示モード「Market Observatory」ページを表示する"""
    safe_symbols, safe_config = _get_safe_template_context()
    return render_template(
        "experimental_orbit.html",
        model_badge=get_model_badge(),
        default_symbols=safe_symbols,
        app_config=_template_config_for_render(safe_config),
    )
