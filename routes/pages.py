from routes._common import *  # noqa: F401,F403

from flask import send_from_directory

pages_bp = Blueprint("pages", __name__)


@pages_bp.route("/favicon.ico")
def favicon():
    """favicon.ico の直接参照を許可する"""
    root_favicon = Path(current_app.root_path) / "favicon.ico"
    if root_favicon.exists():
        return send_from_directory(current_app.root_path, "favicon.ico")
    return send_from_directory(current_app.static_folder, "favicon.ico")


@pages_bp.route("/")
@pages_bp.route("/setup")
def setup():
    """セットアップページを表示する"""
    return render_template(
        "setup.html",
        model_badge=get_model_badge(),
        default_symbols=get_default_symbols(),
        app_config=get_api_credential_state(),
    )


@pages_bp.route("/main")
def main_page():
    """メインページを表示する"""
    return render_template(
        "index.html",
        model_badge=get_model_badge(),
        default_symbols=get_default_symbols(),
        app_config=get_api_credential_state(),
    )


@pages_bp.route("/heatmap")
def heatmap_page():
    """ヒートマップページを表示する"""
    return render_template(
        "heatmap.html",
        model_badge=get_model_badge(),
        default_symbols=get_default_symbols(),
        app_config=get_api_credential_state(),
    )


@pages_bp.route("/settings")
def settings_page():
    """設定ページを表示する"""
    return render_template(
        "settings.html",
        model_badge=get_model_badge(),
        default_symbols=get_default_symbols(),
        app_config=get_api_credential_state(),
    )
