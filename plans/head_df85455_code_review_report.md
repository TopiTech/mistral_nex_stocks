# コードレビュー最終報告（HEAD `df85455`）

レビュー対象: リポジトリ全体（HEAD `df85455` = "test: add regression tests for code-review findings R3, R7, and R17"）
実施日: 2026-08-17
前提: 既存の未コミット差分なし（`git status` クリーン）。本レビューによる修正のみが新規差分。

---

## 1. 対応結果

### [R1][Low] テスト環境で yfinance の SQLite キャッシュ接続がクローズされず ResourceWarning を発生

- **該当箇所**:
  - `tests/conftest.py:246-269`（修正）— テスト環境に本番同等の yfinance キャッシュ分離を適用
  - `tests/test_coverage_utils_extra.py:116-123`（修正）— `ChatHistoryTestCase` に `tearDown` 追加
  - `tests/test_yfinance_cache_test_isolation.py`（新規・回帰テスト 4 件）

- **影響経路**: フルスイート実行時、`test_audit_comprehensive_fixes_2026.py::TestUsdJpyPersistenceThrottling::test_usdjpy_persistence_throttled_when_rate_unchanged`（`_update_indices_data` → `fetch_index_data` → `get_history` → `yf.Ticker` 構築）や `test_code_review_fixes.py::test_r2_yahoojp_scraper_rapid_stop_start_lifecycle` が本物の yfinance パスを実行。yfinance はモジュールレベルの peewee SQLite キャッシュ（タイムゾーン `_TZ_KV`、cookie DB、ISIN DB）を初回利用時にオープンする。これらはクローズされず、後続テスト中の GC タイミング（例: werkzeug ルーティングコンパイル時）で `ResourceWarning: unclosed database in <sqlite3.Connection>` として顕在化。`pytest.ini` は `filterwarnings = error::ResourceWarning` を設定しているが、`__del__` 内の unraisable 例外はこのフィルタを迂回するため、これまで CI で黙殺され続けていた。

- **問題・根本原因**: 本番では `app.py` の `bootstrap()` → `app_state.initialize_yfinance_cache()` が **あらゆる yfinance 利用より前に** SQLite キャッシュをインメモリ実装（`_InMemoryYfCache` / `_InMemoryCookieCache`）へ置換するため、本番で SQLite 接続は開かれない。テストは `MNS_SKIP_BOOTSTRAP=1` により bootstrap をスキップするため、この置換が実行されず、実 yfinance パスを叩くテストだけが SQLite 接続を開き、クローズされない。実害はテスト環境に限定されるが、(1) 全実行で ResourceWarning が出力され続け、真のリソースリークをマスクする、(2) CI の警告検知の信頼性を損なう、の運用リスクがある。

- **対応内容**:
  1. `tests/conftest.py`: 本番 `initialize_yfinance_cache()` と同じ 3 キャッシュ置換（`_TzCacheManager._tz_cache` / `_CookieCacheManager._Cookie_cache` / `_ISINCacheManager._isin_cache` → インメモリ実装）をテスト環境インポート時に適用。yfinance 内部 API が変わった場合に備え `try/except (ImportError, AttributeError)` で防御。
  2. `tests/test_coverage_utils_extra.py`: `ChatHistoryTestCase` は `setUp` で自前の `SQLiteChatHistoryStore` を生成しテストケースが生存し続けるため、スレッドローカル sqlite3 接続が GC まで開きっぱなしになる。`tearDown` で `close_all()` を明示。

- **結果**: `修正済み`
- **回帰テスト**: `tests/test_yfinance_cache_test_isolation.py` に 4 件追加
  - `test_tz_cache_is_in_memory_in_test_env`: TZ キャッシュがインメモリ実装であること＋store/lookup 往復
  - `test_cookie_cache_is_in_memory_in_test_env`: cookie キャッシュがインメモリ実装かつ `{"cookie": ..., "age": ...}` 契約を保持
  - `test_isin_cache_is_in_memory_in_test_env`: ISIN キャッシュがインメモリ実装であること
  - `test_yfinance_cache_lookup_opens_no_sqlite_connection`: TZ キャッシュ lookup で sqlite3 接続が新規オープンされないこと（`gc.get_objects()` で開いている `sqlite3.Connection` 数を比較）
  - 検証: 修正前にフルスイートで再現した `PytestUnraisableExceptionWarning: ResourceWarning: unclosed database` が、修正後 `-W error::ResourceWarning` 付きフルスイート（2247 tests）で **0 件** になることを確認。

---

## 2. 変更ファイル一覧

- `tests/conftest.py`: yfinance SQLite キャッシュをインメモリ実装へ置換（本番 `initialize_yfinance_cache` と同一の分離をテスト環境に適用）— R1
- `tests/test_coverage_utils_extra.py`: `ChatHistoryTestCase.tearDown` で `store.close_all()` を明示 — R1
- `tests/test_yfinance_cache_test_isolation.py`: R1 の回帰テスト（新規 4 件）— R1

---

## 3. 検証結果

- **成功**:
  - `pytest tests/`（2247 tests, `-W error::ResourceWarning` 付き）→ 全て成功・ResourceWarning 0 件（exit 0）
  - リーク探索プローブ（`sqlite3.connect` ラップ＋テスト境界ごとの未クローズ接続検出）→ 修正後フルスイートで未クローズ接続 0 件
  - `ruff check` → 全チェック通過
  - `flake8` → エラーなし
  - `mypy`（50 source files）→ Success
  - 修正対象テストの個別実行（`test_yfinance_cache_test_isolation.py` / `test_coverage_utils_extra.py` / `test_audit_comprehensive_fixes_2026.py` / `test_code_review_fixes.py` / `test_chat_history.py`）→ 全て成功
  - `npm run verify-generated` → `api_client.js` は TypeScript 出力と一致（フロントエンド契約にドリフトなし）
- **失敗/スキップ**: `npm audit --audit-level=high` はローカルの npm 設定（EALLOWSCRIPTS）起因で実行不可。リポジトリ起因ではなく、依存関係の変更もないためリスク低。`pip-audit --strict` は成功済み。

---

## 4. 互換性・移行

- 公開 API・スキーマ・設定・データ構造への影響なし。
- テスト環境のみの変更であり、本番コード（`app.py` / `app_state.py` / yfinance 関連サービス）は無変更。
- yfinance 内部属性（`_TzCacheManager` 等）への依存は既存の本番コード（`app_state.initialize_yfinance_cache`）と同一であり、新規の依存追加ではない。

---

## 5. 調査範囲・残存リスク

- **調査済み**: バックエンド全ルート（`routes/`）、全サービス（`services/`）、全ユーティリティ（`utils/`）、状態管理（`app_state.py` / `app_bg.py` / `market_state.py` / `ai_state.py` / `execution_state.py`）、設定・暗号化・資格情報管理（`config_store.py` / `crypto_utils.py` / `credential_manager.py`）、セキュリティ層（`security_config.py` / `error_handlers.py` / `shutdown_manager.py` / `route_helpers.py`）、ネイティブホスト（`native_host/`）、フロントエンド JS 全ファイル（XSS パターン・API 契約・SSE 処理）、Chrome 拡張、SSE リプレイ・リアルタイムエンジン・AIポートフォリオ等の新機能、動的プローブ（SSE ストリーム・エッジケース API・ポートフォリオ計算）。
- **確定した指摘**: R1 のみ。他は深刻度基準（再現性＋客観的根拠＋実害）を満たす問題なし。コードベースは過去 3 回以上のレビューサイクルを経ており、認証・入力検証・リソース管理・並行処理が非常に堅牢。
- **残存リスク（軽微）**:
  - `test_audit_comprehensive_fixes_2026.py` 等のテストが実 yfinance パスを叩く際、ネットワーク到達性に依存する部分がある（本セッションではモック・スタブにより成功）。CI 環境によってはネットワーク依存のテストが別途失敗する可能性があるが、本変更とは無関係。
  - yfinance 内部属性への依存は既存設計の一部（前述）。yfinance のメジャーアップグレード時は `initialize_yfinance_cache` と共にレビューが必要。
  - `npm audit` はローカル環境要因で未実施（依存関係の変更なし）。
