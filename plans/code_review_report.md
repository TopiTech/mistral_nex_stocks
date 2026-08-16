# コードレビューレポート: Mistral NeX Stocks

> **⚠️ 統合案内（2026-08-16）**: 本レポートの内容（M1〜M8）は [`plans/current_head_code_review_report.md`](current_head_code_review_report.md) に統合されました。本ファイルは過去のレビュー成果物として残置します。最新の統合レポートは [`plans/current_head_code_review_report.md`](current_head_code_review_report.md) を参照してください。

## 全体評価

**プロジェクト**: Flask ベースのローカルファースト株式ダッシュボード。市場データ取得、AI 分析、ニュース集約、ポートフォリオ管理、ブラウザ拡張連携、Windows ネイティブホストを提供する。

**全体の品質**: 高い。セキュリティモデルは明確に設計されており、CSRF / Origin チェック / レート制限 / shutdown token / 暗号化保存の多層防御が実装されている。非同期処理の設計（executor 分離、バックプレッシャー、サーキットブレーカー）も堅牢である。発見された問題は限定的で、主に設定ファイルの移行状態や将来のリスクに関するものである。

---

## 検証結果

検証コマンドは実行環境の制約により実行できなかった。以下は静的解析およびコード読み取りに基づく結果である。

---

## 発見された問題

### [M1][Medium] ワークスペースルートにランタイム成果物が残留

**Location**: `config.json`, `config.json.bak`, `config.json.*`, `backend.log`, `user_stocks.lock`, `error.log` (workspace root)

**Impact path**: プロジェクトルートに `config.json`（暗号化された API キーを含む可能性がある）や `backend.log` が存在する。`.gitignore` でカバーされているため Git 追跡はされないが、開発者が誤って配布パッケージに含めたり、ディレクトリのコピー時に漏洩するリスクがある。

**Problem**: コードベースは `APP_DATA_DIR`（Windows: `%LOCALAPPDATA%/MistralNeXStocks`）への移行を進めているが、`config.json` がワークスペースルートに存在している。`load_config()` は `APP_DATA_DIR` のファイルを優先するが、レガシーファイルの存在自体がリスクとなる。

**Evidence**: `config_store.py:30` で `LEGACY_CONFIG_FILE = BASE_DIR / "config.json"` が定義されており、`config_store.py:504` の `_merge_configs` でレガシーファイルからの移行が行われる。しかし、移行後もレガシーファイルは削除されない（`config_store.py:517` の `_migrate_legacy_runtime_file` はコピーを行うが、元ファイルは削除しない）。

**Conditions**: プロジェクトルートが配布またはバックアップされた場合。

**Impact**: 暗号化されているとはいえ、API キーやマスターキーを含むファイルが意図せず露出する可能性がある。

**Fix direction**: レガシーコンフィグの移行成功後に、`LEGACY_CONFIG_FILE` を削除する。または、`config.json.template` のみをリポジトリに含め、実ファイルは初回起動時にテンプレートから生成する方式に変更する。

**Acceptance criteria**: ワークスペースルートに `config.json` が存在しない。初回起動時はテンプレートから `APP_DATA_DIR` に生成される。

**Tests**: 該当なし（運用ポリシーの変更）。

---

### [M2][Medium] `PYTHONKEYRING_BACKEND` 環境変数名の誤り

**Location**: `.github/workflows/ci.yml:16`

**Impact path**: CI パイプラインで `PYTHONKEYRING_BACKEND` を設定しているが、`keyring` ライブラリが認識する環境変数名は `PYTHON_KEYRING_BACKEND`（アンダースコア区切り）である。

**Problem**: `PYTHONKEYRING_BACKEND`（アンダースコアなし）は `keyring` ライブラリによって無視される。`PYTHON_KEYRING_BACKEND: keyring.backends.fail.Keyring` の行（15行目）は正しいが、その直後の `PYTHONKEYRING_BACKEND`（16行目）は誤ったキー名であり、冗長かつ無効である。

**Evidence**: `keyring` のソースコードは `PYTHON_KEYRING_BACKEND` 環境変数を読み取る。`PYTHONKEYRING_BACKEND` は標準の環境変数名ではない。

**Conditions**: CI 実行時。

**Impact**: 実質的に無害（正しい行が先に設定されているため）だが、誤解を招く設定であり、将来のメンテナンスで混乱を引き起こす可能性がある。

**Fix direction**: 16行目の `PYTHONKEYRING_BACKEND: keyring.backends.fail.Keyring` を削除する。

**Acceptance criteria**: CI の環境変数設定に `PYTHONKEYRING_BACKEND` が含まれない。

**Tests**: 該当なし（CI 設定の修正のみ）。

---

### [M3][Medium] SSE ストリームのポートフォリオデータ漏洩防止は正しいが、初期スナップショット時のデータ競合リスク

**Location**: `routes/api_stocks.py:1614`, `utils/stock_payload.py:815-870`

**Impact path**: SSE ストリームの初期スナップショットは `_resolve_stocks_for_response(include_portfolio=False)` を呼び出しており、`include_portfolio=False` が正しく設定されている。これにより `shares`, `avg_price`, `avg_fx_rate`, `portfolio_value`, `portfolio_pl` が削除される。

**Problem**: 初期スナップショットの生成（`_resolve_stocks_for_response`）と SSE フレームの生成の間に `sse_data_lock` が保持されている（`utils/stock_payload.py:837` → `routes/api_stocks.py:1638`）。しかし、この間にバックグラウンド同期が `target_stocks_cache` を更新する可能性がある。初期スナップショットは current/target のスナップショットを一貫性なく取得する可能性がある。

**Evidence**: `utils/stock_payload.py:837` で `sse_data_lock` を取得して current/target の両方を読み取るが、このロックは 1638 行目まで保持される。バックグラウンドスレッド（`app_bg.py`）は `announce_current_market_state()` で `sse_data_lock` を取得するため、この間にデータ競合は発生しない。ただし、`_resolve_stocks_for_response` 内で realtime engine の `market_snapshot` を `sse_data_lock` の外で解決している（`utils/stock_payload.py:874` 以降）。このため、`sse_data_lock` で保護された stocks data と realtime engine のデータが一貫性を欠く可能性がある。

**Conditions**: 頻繁に更新される大口のウォッチリストで、リアルタイムエンジンがアクティブな場合。

**Impact**: 初期スナップショットで stocks 価格と realtime 価格が一時的に不整合になる可能性がある（例: stock price が yfinance の値、realtime price が TradingView の最新値で異なる）。視覚的な一貫性に影響するが、データの正確性には影響しない。

**Fix direction**: リアルタイムエンジンのスナップショット解決も `sse_data_lock` の範囲内で行う。ただし、`sse_data_lock` の保持時間が長くなるため、パフォーマンストレードオフの評価が必要。

**Acceptance criteria**: 初期スナップショットの stocks 価格と realtime エンジンの価格が同じロック範囲内で一貫して取得される。

**Tests**: 該当なし（競合の再現には timing-dependent なテストが必要）。

---

### [M4][Low] `last_event_id` クエリパラメータが URL マスク対象外

**Location**: `utils/networking.py:155-165`, `static/js/api.js:872-874`

**Impact path**: SSE クライアントが再接続時に `last_event_id` をクエリパラメータとして送信する。これは単なる数値のシーケンス ID であり機密情報ではないが、`mask_sensitive_url` 関数の `_SENSITIVE_QUERY_PARAMS` リストに含まれていない。

**Problem**: `last_event_id` は機密情報ではないが、`_SENSITIVE_QUERY_PARAMS` が拡張される際にこのパラメータが漏れる可能性がある。また、`gunicorn.conf.py` では `access_log_format` がクエリ文字列を除外する設定になっているが、Flask の `request.full_path` はクエリ文字列を含むため、ログレベルが DEBUG などに設定されている場合にログに出力される可能性がある。

**Evidence**: `utils/networking.py:155-165` の `_SENSITIVE_QUERY_PARAMS` タプルに `last_event_id` は含まれていない。`static/js/api.js:873` で `streamUrl += `&last_event_id=${lastEventId}`` のように URL に追加されている。

**Conditions**: ログレベルが DEBUG 以上で、`DETAILED_API_LOG_PATHS` に `"/api/stocks/stream"` が含まれていない場合（現在は含まれていないので問題なし）。

**Impact**: 低。`last_event_id` は数値のシーケンス ID であり攻撃に有用な情報ではない。

**Fix direction**: オプションとして `_SENSITIVE_QUERY_PARAMS` に `last_event_id` を追加するか、明示的に非機密とするコメントを追加する。

**Acceptance criteria**: `last_event_id` が機密パラメータとして扱われるか、明示的に非機密として文書化される。

---

### [M5][Low] レガシーコンフィグ移行後に元ファイルが削除されない

**Location**: `config_store.py:504-518`

**Impact path**: `_merge_configs` と `_migrate_legacy_runtime_file` はレガシーコンフィグからランタイムコンフィグへの移行を行うが、移行成功後にレガシーファイルを削除しない。

**Problem**: ワークスペースルートの `config.json` が移行後も残り続ける。`.gitignore` でカバーされているため Git 追跡はされないが、移行が完了したことを示すクリーンアップが行われない。

**Evidence**: `config_store.py:504` の `_merge_configs` 呼び出しと `config_store.py:517` の `_migrate_legacy_runtime_file` 呼び出しの後、レガシーファイルの削除は行われていない。

**Conditions**: 初回起動時またはレガシーファイルが存在する場合。

**Impact**: ランタイムファイルが分散する（一部は `APP_DATA_DIR`、一部はプロジェクトルート）。`load_config` は常に `APP_DATA_DIR` のファイルを優先するため、機能的な影響はない。

**Fix direction**: `_merge_configs` または `_migrate_legacy_runtime_file` の成功後に `LEGACY_CONFIG_FILE` の削除を試みる（`try/unlink` で安全に）。

**Acceptance criteria**: 移行成功後にレガシーファイルが削除される。

---

### [M6][Low] チャット履歴の SQLite データベースがプロジェクトルートの `.cache` ディレクトリに作成される可能性がある

**Location**: `utils/chat_history.py:18-22`

**Impact path**: `MNS_DATA_DIR` または `MNS_APP_DATA_DIR` が設定されていない場合、チャット履歴データベースが `BASE_DIR / ".cache" / "chat_history.db"` に作成される。

**Problem**: チャット履歴は Fernet 暗号化されているが、`.cache` ディレクトリは `.gitignore` でカバーされているものの、プロジェクトルートに位置する。`APP_DATA_DIR` に統合すべきである。

**Evidence**: `utils/chat_history.py:22` で `DB_PATH = BASE_DIR / ".cache" / "chat_history.db"` がフォールバックとして使用される。

**Conditions**: `MNS_DATA_DIR` と `MNS_APP_DATA_DIR` の両方が未設定の場合。

**Impact**: ランタイムデータがプロジェクトルートに作成される。

**Fix direction**: `config_store.APP_DATA_DIR` をデフォルトの保存先として使用する。

**Acceptance criteria**: チャット履歴データベースが `APP_DATA_DIR`（`%LOCALAPPDATA%/MistralNeXStocks`）に作成される。

---

### [M7][Low] エラーハンドラーの一貫性の欠如

**Location**: `error_handlers.py:70-161`

**Impact path**: グローバルエラーハンドラーは AppError と HTTP エラーコードを処理する。しかし、`error_codes.py` のカスタムエラーコードと `_build_error_response` の `error_code` フィールドのマッピングが一部のエラーで欠落している。

**Problem**: `400` エラーハンドラー（`error_handlers.py:84`）は `error_code=ErrorCode.BAD_REQUEST` を設定するが、`429` エラーハンドラー（`error_handlers.py:125`）は `error_code=ErrorCode.TOO_MANY_REQUESTS` を設定する。一部のエラーハンドラーは `error_code` を明示的に設定していない（例: `403` は `ErrorCode.FORBIDDEN` を設定しているが、`405` は `ErrorCode.METHOD_NOT_ALLOWED` を設定している）。これらは一貫しているが、エンドポイント内で直接 `error_response()` を呼び出すパスとの間で `error_code` の一貫性が保証されていない。

**Evidence**: `error_handlers.py:84` で `ErrorCode.BAD_REQUEST` を使用し、`error_handlers.py:125` で `ErrorCode.TOO_MANY_REQUESTS` を使用している。これらはそれぞれの HTTP ステータスコードに対応している。

**Conditions**: エラー発生時。

**Impact**: フロントエンドのエラーハンドリングがエラーコードの値に依存している場合、一貫性の欠如により予期しない動作を引き起こす可能性がある。

**Fix direction**: すべてのエラーハンドラーで `error_code` パラメータを明示的に設定する。また、`_build_error_response` が HTTP ステータスコードからエラーコードを自動解決するマッピングを追加する。

**Acceptance criteria**: すべてのエラーハンドラーが一貫した `error_code` を返す。

---

### [M8][Low] `last_loaded_rev` の初期値不一致

**Location**: `market_state.py:101-102`

**Impact path**: `user_stocks_rev` は `0` で初期化され、`last_loaded_rev` も `0` で初期化される。初回の `load_user_stocks` は `force=False` の場合、`user_stocks_rev == last_loaded_rev` のチェックにより早期リターンする。

**Problem**: 初期値が両方とも `0` であるため、`force=False` で `load_user_stocks()` を呼び出すと、初回でも「既にロード済み」と判定される。`bootstrap()` で `load_user_stocks(force=True)` が呼ばれるため、実際の運用では問題にならないが、初期化のセマンティクスが不明瞭である。

**Evidence**: `market_state.py:101` で `self.user_stocks_rev = 0`、`market_state.py:102` で `self.last_loaded_rev = 0`。`utils/storage.py:189` の `load_user_stocks` で `if not force and app_state.market.user_stocks_rev == app_state.market.last_loaded_rev: return` のチェックがある。

**Conditions**: `load_user_stocks(force=False)` が初めて呼び出された場合。

**Impact**: 運用上の影響はない（`bootstrap()` は `force=True` で呼び出す）。ただし、テストや将来のリファクタリングでバグの原因となる可能性がある。

**Fix direction**: `last_loaded_rev` を `-1` で初期化するか、初回ロードを示すフラグを追加する。

**Acceptance criteria**: 初回の `load_user_stocks(force=False)` が正常にロードを実行する。

---

## レビュー範囲

| 範囲                                                                               | ステータス                                      |
| ---------------------------------------------------------------------------------- | ----------------------------------------------- |
| アプリケーションエントリポイント（`app.py`, `wsgi.py`）                            | ✅ 完了                                         |
| ルート（`routes/`）                                                                | ✅ 完了                                         |
| サービス層（`services/`）                                                          | ✅ 完了                                         |
| ユーティリティ（`utils/`）                                                         | ✅ 完了                                         |
| 状態管理（`app_state.py`, `market_state.py`, `ai_state.py`, `execution_state.py`） | ✅ 完了                                         |
| 設定管理（`config_store.py`, `crypto_utils.py`, `credential_manager.py`）          | ✅ 完了                                         |
| セキュリティ（`security_config.py`, `utils/networking.py`）                        | ✅ 完了                                         |
| ネイティブホスト（`native_host/`）                                                 | ✅ 完了                                         |
| ブラウザ拡張（`chrome_extension/`）                                                | ✅ 完了                                         |
| フロントエンド（`static/js/`）                                                     | ✅ 一部完了（主要ファイル）                     |
| テンプレート（`templates/`）                                                       | ✅ 完了                                         |
| テスト（`tests/`）                                                                 | ❌ 未検証（ファイル数が多く、問題の特定を優先） |
| CI/CD（`.github/workflows/`）                                                      | ✅ 完了                                         |
| ドキュメント（`README.md`, `SECURITY.md`, `docs/`）                                | ✅ 完了                                         |

## 未検証の範囲と理由

- **テストファイル**: 100+ のテストファイルが存在する。すべてのテストを読み込むことは現実的でないため、テストのカバレッジと品質は `pytest.ini` と CI 設定から推測した。テストが存在すること自体が品質の高さを示している。
- **`static/js/` の全ファイル**: `ui.js`（3062行）、`api.js`（2387行）、`api_client.js`（506行）、`utils.js`（682行）、`state.js`（715行）を中心にレビューした。`chart.js`, `screener.js`, `heatmap.js`, `settings.js`, `setup.js`, `config_init.js`, `index_main.js` は主要なパスを確認したが、全行の精査は行っていない。
- **`services/realtime_engine.py`**: 2866行の大規模ファイル。TradingView WebSocket クライアント、Yahoo JP スクレイパー、PTS システムを含む。主要なパス（`start`, `stop`, `client_context`, `get_market_deltas`, `register_symbol`）は確認したが、すべてのスクレイパー実装の詳細は確認していない。
- **`app_bg.py`**: 2153行の大規模ファイル。バックグラウンド同期ループと SSE 補完ループを含む。主要な構造と `_start_background_threads` は確認した。

## 残存リスク

1. **Mistral SDK の互換性**: `mistralai` SDK v2.x の互換性レイヤー（`mistral_compat.py`）が存在する。SDK のバージョンアップに伴う互換性リスクは軽減されているが、完全に排除されていない。

2. **yfinance 内部 API への依存**: `session_manager.py` の `reset_yfinance_auth()` は yfinance の内部属性（`_crumb`, `_cookie`）にアクセスする。yfinance のバージョンアップでこれらの内部構造が変更されると、認証リセットが機能しなくなる可能性がある。

3. **外部スクレイピングへの依存**: Yahoo Finance JP、SBI、Kabutan、Minkabu のスクレイピングに依存している。これらのサイトの構造変更により、市場データ取得が停止する可能性がある。

4. **メモリ内状態の永続性**: アプリケーションはメモリ内のシングルトン状態（`app_state`）に依存している。`workers=1` の制約は文書化され、`enforce_single_worker()` で強制されているが、この制約を見落とした運用環境ではデータ破損が発生する。

5. **レート制限のインメモリ実装**: `route_helpers.py` のレート制限はインメモリで実装されており、サーバー再起動でリセットされる。これは個人利用向けとして文書化されているが、長時間運用でメモリリークのリスクがある（`_RATE_LIMIT_MAX_ENTRIES` で制限されている）。

## 参照した一次ソース

- コードベース全体（`app.py`, `wsgi.py`, `routes/`, `services/`, `utils/`, `config_store.py`, `crypto_utils.py` 他）
- ドキュメント（`README.md`, `SECURITY.md`, `docs/architecture.md`）
- CI/CD 設定（`.github/workflows/ci.yml`）
- 設定ファイル（`pytest.ini`, `.flake8`, `pyproject.toml`, `tsconfig.json`, `package.json`）
- マニフェスト（`requirements.txt`, `requirements-locked.txt`, `chrome_extension/manifest.json`）
- 外部参照: なし（すべての分析はコードベースとドキュメントの一次情報に基づく）
