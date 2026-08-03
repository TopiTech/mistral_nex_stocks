from pathlib import Path

from flask import Blueprint, current_app, render_template, send_from_directory

from credential_manager import get_api_credential_state, get_model_badge
from utils.stock_payload import get_default_symbols
from utils.validators import AppConfigSchema, DefaultSymbolsSchema

pages_bp = Blueprint("pages", __name__)


@pages_bp.route("/favicon.ico")
def favicon():
    """favicon.ico の直接参照を許可する"""
    root_favicon = Path(current_app.root_path) / "favicon.ico"
    if root_favicon.exists():
        return send_from_directory(current_app.root_path, "favicon.ico")
    static_folder = current_app.static_folder or str(Path(current_app.root_path) / "static")
    return send_from_directory(static_folder, "favicon.ico")


@pages_bp.route("/")
@pages_bp.route("/setup")
def setup():
    """セットアップページを表示する"""
    # Validate variables before injecting them into templates (L-3)
    safe_symbols = DefaultSymbolsSchema.model_validate(get_default_symbols()).model_dump()
    safe_config = AppConfigSchema.model_validate(get_api_credential_state()).model_dump()
    return render_template(
        "setup.html",
        model_badge=get_model_badge(),
        default_symbols=safe_symbols,
        app_config=safe_config,
    )


@pages_bp.route("/main")
def main_page():
    """メインページを表示する"""
    safe_symbols = DefaultSymbolsSchema.model_validate(get_default_symbols()).model_dump()
    safe_config = AppConfigSchema.model_validate(get_api_credential_state()).model_dump()
    return render_template(
        "index.html",
        model_badge=get_model_badge(),
        default_symbols=safe_symbols,
        app_config=safe_config,
    )


@pages_bp.route("/heatmap")
def heatmap_page():
    """ヒートマップページを表示する"""
    safe_symbols = DefaultSymbolsSchema.model_validate(get_default_symbols()).model_dump()
    safe_config = AppConfigSchema.model_validate(get_api_credential_state()).model_dump()
    return render_template(
        "heatmap.html",
        model_badge=get_model_badge(),
        default_symbols=safe_symbols,
        app_config=safe_config,
    )


@pages_bp.route("/screener")
def screener_page():
    """簡易スクリーナーページを表示する"""
    safe_symbols = DefaultSymbolsSchema.model_validate(get_default_symbols()).model_dump()
    safe_config = AppConfigSchema.model_validate(get_api_credential_state()).model_dump()
    return render_template(
        "screener.html",
        model_badge=get_model_badge(),
        default_symbols=safe_symbols,
        app_config=safe_config,
    )


@pages_bp.route("/settings")
def settings_page():
    """設定ページを表示する"""
    safe_symbols = DefaultSymbolsSchema.model_validate(get_default_symbols()).model_dump()
    safe_config = AppConfigSchema.model_validate(get_api_credential_state()).model_dump()
    return render_template(
        "settings.html",
        model_badge=get_model_badge(),
        default_symbols=safe_symbols,
        app_config=safe_config,
    )
