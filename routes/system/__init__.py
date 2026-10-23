"""システム系エンドポイントの関心事ごとの分割モジュール群。

公開 API (`api_system_bp` とハンドラ) は `routes.api_system` 経由で提供
される。テストで `patch(\"routes.api_system.X\")` される名前
(`save_api_credentials` / `get_custom_ai_prompt` /
`get_api_credential_state` / `clear_api_credentials` /
`_is_local_request` / `_require_admin_token_if_remote`) は呼び出し時に
`routes.api_system` 属性として解決する。
"""
