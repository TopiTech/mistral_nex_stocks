"""Flask アプリケーションの関心事ごとの分割モジュール群。

公開 API は `app` 経由で提供される。共有状態
(`_app_bootstrap_done` / `_app_bootstrap_lock`) は `app` モジュールが
所有し、各モジュールは `import app` + 呼び出し時属性解決で参照する。
これはテスト隔離 (`app._app_bootstrap_done` の再束縛) と
`patch(\"app.X\")` 系モックを保つためである。

ロガー名は `app` に統一する (`logging.getLogger(\"app\")`)。`app.logger`
(`/app.logger.warning` の patch 対象) と同一オブジェクトになる。
"""
