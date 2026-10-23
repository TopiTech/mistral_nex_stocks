"""設定ストアの関心事ごとの分割モジュール群。

公開 API は `config_store` 経由で提供される。各モジュールは共有状態
(パス定数・ロック・キャッシュ・破損フラグ) およびテストでモックされる
振る舞い (`load_config` / `save_config` / `_is_windows` 等) を
`config_store` モジュール属性として参照する。これはテスト隔離
(`tests/test_support/patches.py` が `config_store` 属性を再束縛する) と
`patch(\"config_store.X\")` 系モックを保つためである。
"""
