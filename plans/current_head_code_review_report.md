# Mistral NeX Stocks 自律コードレビュー報告書（現在HEAD）

- 報告日: 2026-08-14（JST）
- 対象: 現在の HEAD 全体（差分レビューではなくリポジトリ全体）
- レビューモード: Architect（読み取り専用）→ サブタスクB（Code、R3-1 修正＋回帰テスト）→ サブタスクC（Code、全体検証・最終自己レビュー・本報告書の最終結果反映）
- 前提: 既存の `plans/code_review_report.md` は過去の成果物であり編集しない。本報告書は独立した新規ファイル。
- **本報告書はサブタスクCにて最終結果（検証結果・各指摘の最終状態）を反映済み**。

---

## 1. 基準状態

### 1.1 検証環境と制約

- **サブタスクA（Architect）時点**ではコマンド実行ツールが利用できない環境であり、基準検証は静的解析（ファイル読み取り・正規表現検索・構造調査）による代替とした。
- **サブタスクC（Code）** ではコマンド実行ツールが利用可能な環境にて、下記の実検証を実施した（詳細は「5. 確認できなかった範囲」更新および「付録A. サブタスクCの検証結果」参照）。
  - `git status --porcelain` / `git diff --stat` / `git log -1 --oneline` / `git ls-files` / `git check-ignore`: **実行済み**。ブランチ master・HEAD `eefa8e9` を確定。未コミット差分は `app_state.py`・`tests/test_app_state_lifecycle.py` の修正と、本報告書（未追跡）のみ。
  - `pytest`（全スイート・関連テスト群）: **実行済み・全パス**（coverage 78.55%、`--cov-fail-under=68` を上回る）。
  - ruff / flake8 / pylint / ruff format / mypy / pyrefly: **実行済み**（詳細は付録A）。
  - npm typecheck / verify-generated / eslint / prettier / npm ci --dry-run: **実行済み・全パス**。
  - `uv lock --locked --check` / `uv export --locked` と `requirements-locked.txt` の突合: **実行済み・一致**。
- セキュリティ方針に従い、**秘密情報・資格情報・トークンの値は一切表示・転記しない**。環境変数・暗号化方式の「存在・参照方法・管理方法」のみ確認した。

### 1.2 ブランチ・HEAD

- **確定値**: ブランチ `master`、HEAD `eefa8e9`（`git log -1 --oneline` で確認。commit message: "ci: implement CI pipeline, add regression tests, and centralize error payload handling"）。
- リポジトリの CI は `push` / `pull_request` の `main`, `master` ブランチを対象とする（`.github/workflows/ci.yml:3-7`）。SECURITY.md は「latest release version on the main/master branch」をサポート対象としており、`>=3.0` をサポート（`SECURITY.md:9`）。`pyproject.toml` の `version = "3.0.0"` に整合。
- **サブタスクA時点の残存リスク（ブランチ・HEAD・既存差分の確定）は解消済み**。

### 1.3 ランタイム・フレームワーク・マニフェスト

| 項目 | 内容 | 根拠 |
| --- | --- | --- |
| 言語 / ランタイム | Python `>=3.11,<3.15`（CI は 3.12/3.13/3.14 でテスト） | `pyproject.toml:37`, `.github/workflows/ci.yml:164` |
| Web フレームワーク | Flask `>=3.1.3,<3.2` | `pyproject.toml:41` |
| WSGI | Gunicorn（`sys_platform != 'win32'`）、単一ワーカー必須 | `pyproject.toml:63`, `gunicorn.conf.py:33`, `wsgi.py` |
| 主要依存 | pandas, pydantic, requests, tenacity, mistralai, yfinance, curl_cffi, ddgs, keyring, cryptography, websocket-client, tavily-python ほか | `requirements.txt`, `pyproject.toml:40-65` |
| ロック | `uv.lock`（uv 管理）、`requirements-locked.txt`（pip 互換エクスポート） | `README.md:122` |
| Node / TS | TypeScript 型チェック・ESLint・Prettier・verify-generated | `package.json:5-11`, `tsconfig.json` |
| テスト | pytest `9.0.3` + pytest-cov + pytest-timeout、カバレッジ閾値 68% | `pyproject.toml:81-85`, `ci.yml:197` |
| 静的解析 | ruff / flake8（E9,F63,F7,F82）/ pylint（errors-only）/ mypy / pyrefly / bandit / pip-audit | `pyproject.toml`, `ci.yml` |

### 1.4 作業ツリーのランタイム成果物（既存ユーザー変更として保護対象）

ワークスペースルートに、`.gitignore` で追跡除外される以下のランタイム成果物が存在することを確認した。

- `config.json.bak`, `config.json.lock`, `config.json.update.lock`
- `backend.log`, `error.log`
- `user_stocks.lock`, `.backend.start.lock`
- `.coverage`, `coverage.xml`, `bandit_report.txt`

これらは**ユーザーの作業として保護対象**である。`.gitignore:1-5,29-47` で `config.json*`, `logs/`, `backend.log`, `user_stocks.lock`, `.backend.start.lock` などが除外されており、Git 追跡される通常経路はない。値の詳細は表示しない。**本レビューではこれらを削除・変更・参照先の改変を行わない**（指示の保護要件）。

`config.json.template` について、**サブタスクCの実検証**により、`git ls-files config.json.template` で追跡対象に含まれること、`git check-ignore -v config.json.template` で無視対象でないこと（exit 1）を確認した。したがってテンプレートは配布物に含まれる。`config_store.py` はこのテンプレートを参照しておらず（`LEGACY_CONFIG_FILE = BASE_DIR / "config.json"`、`config_store.py:31`）、参考資料としてのみ機能する。**R3-2 は「対応不要（問題不存在）」と確定**（詳細は「3. 確定問題リスト」参照）。

### 1.5 基準テスト（サブタスクCで実検証済み）

- **サブタスクA時点**: コマンド実行不可のため未実行（スキップ）。
- **サブタスクC時点の実検証**（詳細は「付録A. サブタスクCの検証結果」参照）:
  - 関連テスト群3バッチ: すべてパス（63 / 118 / 87 passed, 1 skipped = POSIX-only）。
  - 全テストスイート `pytest tests/`（`--timeout=60 --timeout-method=thread --cov=. --cov-fail-under=68`）: **全パス、coverage 78.55%**。
  - `tests/startup_smoke_runner.py`: 未実行（後述の「5. 確認できなかった範囲」参照。実行条件 `MNS_SKIP_BOOTSTRAP=0` が実ネットワーク/バックグラウンド起動を伴うため）。
- CI が検証する内容（`.github/workflows/ci.yml`）:
  - frontend: typecheck / compile / verify-generated / eslint / prettier / npm audit
  - lint: ruff, flake8, pylint, ロックファイル整合性（`uv export` との diff）
  - type-check: mypy, pyrefly
  - security-scan: bandit（MEDIUM 以上で失敗）、pip-audit、CDN SRI ハッシュ検証
  - test: pytest（Windows、3.12/3.13/3.14、`--timeout=60`、`--cov-fail-under=68`）、startup smoke

---

## 2. 調査した主要実行経路・公開境界

### 2.1 起動・初期化・終了

- `app.py: create_app()`（アプリファクトリ）、`bootstrap()`（ランタイム初期化）、`_register_signal_handlers()`、`_cleanup_on_exit()`（atexit）
- `wsgi.py`: 単一ワーカー強制（`utils/worker_validation.enforce_single_worker`）、リモートモードの ProxyFix 検証
- `gunicorn.conf.py`: `workers=1`、gthread、`MNS_MAX_SSE_LISTENERS+6` のスレッド数、`on_starting` でスレッド数下限検証
- `app_bg._start_background_threads()`: Yahoo/LeaderElection/SessionReap/Interpolate/Watchdog スレッド、クラッシュ時の指数バックオフ再起動
- `shutdown_manager.py`: ワンタイム shutdown token の生成・検証・消費・ローテーション（`%LOCALAPPDATA%/MistralNeXStocks` に Fernet 暗号化保存）

### 2.2 API サーフェス

- `routes/api_system.py`: `/api/credentials`（GET/POST/DELETE）、`/api/health`、`/api/cache-stats`、`/api/metrics`、`GET /api/csrf-token`、`/api/csp-report`、`/api/shutdown`
- `routes/api_stocks.py`: `/api/indices`、`/api/stocks`、`/api/stock-details`、`/api/stock-history`、`/api/search`、`/api/screener`、`/api/stocks/add`、`/api/stocks/delete`、`/api/stocks/portfolio`、`/api/stocks/portfolio/snapshot`、`/api/stocks/add_ext`、`/api/stocks/reset`、`/api/heatmap`、`/api/stocks/stream/ticket`、`/api/stocks/stream`、`/api/ai-portfolio*`（GET・generate・rebalance・save・copy-to-my・DELETE custom）
- `routes/api_analysis.py`: `/api/trending`、`/api/chat`（ポーリング+SSE ストリーミング）、`/api/news`、`/api/analyze-v2`、`/api/ai-technical-lines`
- `routes/pages.py`: `/`、`/setup`、`/main`、`/heatmap`、`/screener`、`/settings`、`/experimental/orbit`、`/favicon.ico`、Chrome DevTools プローブ

### 2.3 セキュリティ境界（詳細確認）

- CSRF: Flask-WTF `CSRFProtect`。`api_csp_report` / `api_shutdown` / `api_add_stock_ext` のみ例外（各々が独自トークン機構を持つ）。`/api/credentials` は CSRF 除外しない（設計として明記）
- Origin / Sec-Fetch-Site: `_enforce_sec_fetch_site_check()`（app.py）、`_is_local_request()`（RAW_REMOTE_ADDR ベース）、`_is_allowed_shutdown_origin()`、`require_trusted_or_admin()`
- レート制限: `route_helpers.rate_limit`（IP/トークン別、ローカル倍率、ポーリング重複スキップ）
- 認可: `MNS_ADMIN_TOKEN`（全保護 API）、SSE セッション紐づけチケット、拡張 Bearer トークン
- 暗号化: keyring / DPAPI / Fernet（マスターキー）による秘密保存、`protect_data`/`unprotect_data`
- 永続化: config（Fernet エンベロープ）、user_stocks.json（Fernet）、chat_history SQLite（Fernet）、ai_portfolios.json（Fernet）。fail-closed 設計

### 2.4 SSE / リアルタイム

- `/api/stocks/stream`（モード0/1/2）、Last-Event-ID リプレイ（`SSEEventLog`）、リスナー上限（`SseListenerLimiter`）、バックプレッシャー
- `services/realtime_engine.py`: TradingView WS クライアント、Yahoo JP スクレイパー、PTS、SBI/Nikkei225JP/Minkabu フォールバック、クライアントカーソル（per-listener delta）
- `app_bg`: `announce_current_market_state` / `announce_real_market_state` / 補完 SSE（`_interpolate_and_fluctuate_market`）
- ポートフォリオ境界: `_PORTFOLIO_RESPONSE_FIELDS` を公開市場データ・SSE・payload disk cache から除去（`utils/stock_payload.py:770-801`）。`include_portfolio=True` は `/api/stocks/portfolio/snapshot`（Origin + CSRF 必須）のみ

### 2.5 バックグラウンド / 永続化

- `app_bg`: `sync_all_stocks_now`、`fetch_stocks_batch`、`_auto_remove_invalid_symbols`（一時障害を誤って削除しない、保存失敗時ロールバック）
- `utils/storage.py`: user_stocks の暗号化保存・ロード（ロック・fsync・アトミック置換）
- `utils/chat_history.py`: SQLite（WAL、スレッド毎接続、Fernet 暗号化、fail-closed）
- `services/ai_portfolio_service.py`: ai_portfolios.json の Fernet 暗号化保存・読込（legacy 平文読込互換）
- `utils/disk_cache.py`: 株価履歴・ペイロードのディスクキャッシュ（プロセス間ロック）

### 2.6 ネイティブホスト / 拡張

- `native_host/native_host.py`: ネイティブメッセージプロトコル（4バイト長+JSON）、拡張 ID・プロセス祖先・origin 引数の三重検証、レート制限、トークンアクション上限
- `native_host/start_backend.py`: バックエンド起動・ヘルスチェック・PID/ロック管理
- `chrome_extension/`: MV3、`host_permissions` は loopback 限定、トークンは session storage、`/api/stocks/add_ext` 経由
- `chrome_extension/content.js`: `<all_urls>` ホストアクセスを持つが、明示的な `detectTickers` メッセージ時のみページテキストを読み取る（SECURITY.md 記載のとおり）

### 2.7 詳細確認対象外としたファイル群（出所・更新方法のみ確認）

- バイナリ・画像: `favicon.ico`, `static/favicon.ico`（静的資産）
- 生成物: `static/js/api_client.js`（`api_client.ts` からコンパイル生成。`npm run verify-generated` で同期検証）
- ロック・マニフェスト: `package-lock.json`, `uv.lock`（CI で整合性検証）
- ネイティブホスト生成物: `native_host/com.mistral_nex_stocks.host.json`, `native_host.cmd`（マシン固有で Git 追跡外。`.gitignore:35-36`）
- テスト大量ファイル（100+）: 個別全文精査は行わず、契約検証テストの存在と網羅性を確認

---

## 3. 確定問題リスト

重要度は影響・発生可能性・検出可能性・回復可能性で判定。同じ根本原因から派生する問題は統合した。

### [R3-1][Medium] ディスクキャッシュがプロジェクトルート `.cache` に作成され、ランタイムデータの格納先が分散する

- **箇所**: [`app_state.py:230`](app_state.py:230)-[`app_state.py:239`](app_state.py:239)（`stock_disk_cache` / `payload_disk_cache` の `cache_dir=BASE_DIR / ".cache"`）
- **影響経路**: 起動時 `AppState.__init__` → `stock_disk_cache` / `payload_disk_cache` の `_ensure_cache_dir` → `BASE_DIR/.cache/stock_history` / `BASE_DIR/.cache/stock_payloads` に JSON ファイルが作成される。
- **問題（発生条件・根拠・影響）**:
  - 他のランタイムデータ（config・user_stocks・shutdown token・chat_history・ai_portfolios）は `APP_DATA_DIR`（Windows: `%LOCALAPPDATA%/MistralNeXStocks`、`config_store.py:34-56`）に移行済み。`chat_history.py:16-21` も `APP_DATA_DIR` へ移行済み（既存レビュー M6 の解決）。しかしディスクキャッシュのみ `BASE_DIR/.cache` に残っており、**格納先が分散する**。
  - `.gitignore:26` の `.cache/` で追跡除外されているため Git 追跡や配布物への混入リスクは低い。またキャッシュ内容はポートフォリオフィールド除去済み（`app_bg.py:445-447`）で機密性リスクはない。
  - 実害は限定的（保守性・分散）だが、`MNS_DATA_DIR` を一時ディレクトリに設定したテスト環境（conftest）や、複数ユーザー環境でルートへの書き込み権限がない場合に動作が想定と異なる。
- **根本原因**: `app_state.py` のディスクキャッシュ生成時に `APP_DATA_DIR` ではなく `BASE_DIR / ".cache"` を参照している。
- **必要な修正結果**: `stock_disk_cache` / `payload_disk_cache` の `cache_dir` を `APP_DATA_DIR / ".cache"`（または `APP_DATA_DIR` 配下）に変更し、他ランタイムデータと格納先を統一する。既存 `BASE_DIR/.cache` に残存するデータのマイグレーション（初回起動時のコピーまたは再取得許容）を検討。
- **受け入れ条件**: 起動後、ディスクキャッシュの JSON が `APP_DATA_DIR` 配下に作成される。`BASE_DIR/.cache` に新規ファイルが作成されない。既存キャッシュを破棄しても起動・株価表示が正常。
- **必要な回帰テスト**: キャッシュディレクトリパスが `APP_DATA_DIR` 配下であることを検証するテスト（conftest の `MNS_DATA_DIR` 一時ディレクトリ内に作成されること）。既存の `test_disk_cache*.py` / `test_indices_cache.py` / `test_stock_payload_extra.py` のパス依存を更新。
- **最終状態: ✅ 修正済み（サブタスクB）**
  - **実施した修正**: [`app_state.py:227`](app_state.py:227) で `from config_store import APP_DATA_DIR` を追加し、`constants.BASE_DIR` の import を削除。`stock_disk_cache` / `payload_disk_cache` の `cache_dir` を `APP_DATA_DIR / ".cache" / ...` に変更（`app_state.py:237-246`）。既存 `BASE_DIR/.cache` のファイルは放置（初回ミス時に再取得する best-effort）。
  - **回帰テスト追加**: [`tests/test_app_state_lifecycle.py:24`](tests/test_app_state_lifecycle.py:24) `test_app_state_disk_caches_reside_under_app_data_dir`、[`tests/test_app_state_lifecycle.py:54`](tests/test_app_state_lifecycle.py:54) `test_app_state_disk_cache_writes_land_in_runtime_dir`。キャッシュが `APP_DATA_DIR/.cache` 配下であること・`BASE_DIR` 配下でないこと・実書き込みがランタイムディレクトリに生成されることを検証。
  - **検証結果（サブタスクC）**:
    - 関連テスト群 63 / 118 / 87 passed（1 skipped = POSIX-only）。
    - 全スイート `pytest tests/ --cov-fail-under=68`: **全パス、coverage 78.55%**。
    - **検出性の実証**: `app_state.py` を一時的に HEAD 状態（`BASE_DIR/.cache`）に戻したところ、追加した回帰テスト2件が確実に失敗（`AssertionError: ...BASE_DIR.../.cache/stock_history == ...APP_DATA_DIR.../.cache/stock_history`）。その後、修正を元に戻し再実行で 3 passed を確認。
    - 公開境界・保存形式の整合性: `.cache` 参照は `app_state.py`（修正後 `APP_DATA_DIR`）、`tests/conftest.py`（一時ディレクトリ）、回帰テスト期待値のみ。`BASE_DIR/.cache` 参照は残存なし。
    - mypy / pyrefly / ruff / flake8 / pylint / ruff format（変更行）: すべてパス。
  - **根拠**: 受け入れ条件（キャッシュが `APP_DATA_DIR` 配下に作成される・`BASE_DIR/.cache` に新規作成されない）を満たす。既存キャッシュ破棄時の再取得は設計どおり（ディスクキャッシュはベストエフォート）。

### [R3-2][Medium] `config.json.template` が `.gitignore` の `config.json.*` にマッチし、Git 追跡されない可能性が高い

- **箇所**: [`.gitignore:1`](.gitignore:1)-[`.gitignore:5`](.gitignore:5)（`config.json`, `config.json.*`, `config.json.bak` 等）、ルートの `config.json.template`
- **影響経路**: リポジトリ取得 → `config.json.template` が配布物に含まれない → ユーザーが設定の初期構成をテンプレートから作成できない。
- **問題（発生条件・根拠・影響）**:
  - `.gitignore` のパターン `config.json.*` は `config.json.template` にもマッチする。`git ls-files` が実行できないため確定はできないが、このパターンでは**テンプレートは追跡されない**。
  - `config_store.py` はこのテンプレートを参照せず、テンプレートはドキュメント的価値のみ。実害は限定的（初期構成の参考資料が配布に含まれない）。
  - **注意**: ルートの `config.json.template` が既存ユーザーによって作成されたものである可能性がある。削除・改名は行わない（保護対象）。修正は `.gitignore` に `!config.json.template` を追加して追跡を明示する形で実施する。
- **根本原因**: `.gitignore` のワイルドカードパターンがテンプレートまで除外している。
- **必要な修正結果**: `.gitignore` に `!config.json.template`（または `config.json.template` を明示的に追跡する例外）を追加し、テンプレートが配布物に含まれるようにする。またはテンプレートを `docs/` 等の追跡対象ディレクトリへ移動。
- **受け入れ条件**: `git check-ignore config.json.template` が非0（追跡されない状態でない）か、`git ls-files` にテンプレートが含まれる。
- **必要な回帰テスト**: なし（`.gitignore` の変更はテスト対象外。CI の lint ジョブで整合性は維持される）。
- **最終状態: ✅ 対応不要（問題不存在・サブタスクCの実検証で確定）**
  - **根拠**: `git ls-files config.json.template` がテンプレートを出力（**追跡済み**）、`git check-ignore -v config.json.template` が exit 1（**無視対象でない**）。`.gitignore` の `config.json.*` パターンは追跡対象には影響しない（追跡済みファイルは無視パターンより優先される）。テンプレートは配布物に含まれ、R3-2 の前提は成立しない。
  - R4-3 も併せて確定（下記参照）。

### [R4-1][Low] ローカルリクエストのレート制限が「ローカル倍率」と「ポーリング重複スキップ」で実質的に広い余裕を持つ

- **箇所**: [`route_helpers.py:371`](route_helpers.py:371)-[`route_helpers.py:386`](route_helpers.py:386)（ローカル倍率）、[`route_helpers.py:320`](route_helpers.py:320)-[`route_helpers.py:364`](route_helpers.py:364)（ポーリング重複スキップ）
- **影響経路**: ローカルからの API 呼び出し → `is_local=True` → 既定上限の 10 倍 + 高めの ceiling。
- **問題（発生条件・根拠・影響）**:
  - ローカル個人利用向けの意図的な設計であり、`MNS_DISABLE_LOCAL_RATE_LIMIT` や `MNS_LOCAL_RATE_LIMIT_CEILING` で調整可能。README にも明記されている。
  - 実害は限定的（ローカルでの暴走時に 429 が遅めに発動する）が、`MNS_DISABLE_LOCAL_RATE_LIMIT=1` を明示設定した場合の ceiling（既定600）は、AI 有料 API への大量連打を完全には防げない。
  - これは「一般論だけのセキュリティ懸念」に近いが、`app.py:198-207` が設定検出時に警告を出しており、実害の確認は**設定依存**である。重要度 Low で「対応不要/対応不能」ではなく、設定ガイダンスの強化として記録。
- **根本原因**: ローカル優先設計の意図的な緩和。
- **必要な修正結果**: 対応不要（仕様どおり）。任意で README に「`MNS_DISABLE_LOCAL_RATE_LIMIT=1` 使用時の ceiling は AI 有料 API の暴走を完全には防がない」旨の注意を追記。
- **受け入れ条件**: なし（仕様変更を伴わない）。
- **必要な回帰テスト**: なし。
- **最終状態: ⚪ 対応不要（仕様どおり・サブタスクCで変更なし）**
  - **根拠**: 意図的なローカル優先設計でありコード修正対象でない。サブタスクCでは README 追記も行わない（スコープ外のドキュメント変更を避けるため）。任意対応として残存事項に記録。

### 対応不要/対応不能と判断した問題（最終状態）

| ID | 内容 | 最終状態 | 判断理由 |
| --- | --- | --- | --- |
| R4-2 | ワークスペースルートのランタイム成果物（`config.json.bak`, `config.json.lock`, `config.json.update.lock`, `backend.log`, `error.log`, `user_stocks.lock`, `.backend.start.lock`）の残留 | ⚪ 対応不能（保護対象） | 既存ユーザー環境の成果物であり、指示により保護対象。コード修正で除去すべきではない。`.gitignore` で追跡除外済みであり、実害（Git 混入・配布物混入）は限定的。サブタスクCでも変更・削除なし。 |
| R4-3 | `config.json.template` の追跡状態の不明確さ（R3-2 の前提） | ✅ 対応不要（問題不存在） | `git ls-files` / `git check-ignore` をサブタスクCで実行し、**追跡済み・無視対象外**を確定（R3-2 参照）。不明確さは解消された。 |
| R4-4 | `PYTHONKEYRING_BACKEND`（`.github/workflows/ci.yml:16`） | ⚪ 対応不要（実害なし） | 正しい `PYTHON_KEYRING_BACKEND`（`ci.yml:15`）と `conftest.py:13` が設定済みで、`PYTHONKEYRING_BACKEND`（アンダースコアなし）は無効な冗長行。実害なし（既存レビュー M2 と同内容だが、CI で問題にならない）。サブタスクCでは CI 設定を変更しない（スコープ外）。 |
| R4-5 | `app_state.py` の `KeyringError` フォールバック（`app_state.py:33-42`）が `keyring` 非インストール時に `_KeyringErrorFallback` を使う | ⚪ 対応不要（実害なし） | 実害なし（`keyring` は必須依存）。到達不能なフォールバックパス。サブタスクB/C の変更とは無関係。 |

---

## 4. 既存レビュー報告書（`plans/code_review_report.md`）との対応

既存報告書の指摘はほぼ解決済みであることを確認した。

| 既存指摘 | 現状 |
| --- | --- |
| M1（ルートに `config.json` 残留） | 移行コードで `LEGACY_CONFIG_FILE.unlink()` が実装済み（`config_store.py:685,700`）。 |
| M2（`PYTHONKEYRING_BACKEND` 誤り） | `ci.yml:16` に残存するが実害なし（R4-4 参照）。 |
| M3（SSE 初期スナップショットのデータ競合） | ロック順序の整理・`_strip_portfolio_fields` の defense-in-depth（`app_bg.py:771-801`）等で緩和。 |
| M4（`last_event_id` の URL マスク） | `_SENSITIVE_QUERY_PARAMS` に `last_event_id` 追加済み（`networking.py:155-166`）。 |
| M5（レガシー config 移行後に元ファイル削除） | `load_config` で削除実装済み（`config_store.py:685-707`）。 |
| M6（chat_history が `.cache` に作成） | `APP_DATA_DIR` へ移行済み（`chat_history.py:16-21`）。 |
| M7（エラーハンドラーの error_code 一貫性） | `error_handlers.py` でステータス対応の error_code 設定が実装済み。 |
| M8（`last_loaded_rev` 初期値不一致） | `market_state.py:105` で `last_loaded_rev = -1` に修正済み。 |

---

## 5. 確認できなかった範囲と理由・環境上実行できなかった検証

### サブタスクA時点（コマンド実行不可）で未確認 → サブタスクCで解消・実検証済みの項目

- **git 系コマンド（HEAD ハッシュ・ブランチ・差分・追跡対象）**: サブタスクCで実行済み。ブランチ `master`・HEAD `eefa8e9` を確定。`git status --porcelain` は `app_state.py` / `tests/test_app_state_lifecycle.py` の修正と本報告書（未追跡）のみ。`config.json.template` は追跡済み・無視対象外（R3-2）。
- **pytest 実行**: サブタスクCで全スイート実行済み・全パス（coverage 78.55%）。CI 設計（`--cov-fail-under=68`、Python 3.12-3.14、Windows）とも整合。
- **npm typecheck / eslint / prettier / verify-generated**: サブタスクCで実行済み・全パス。`npm audit` は実行していない（ネットワーク依存のため）。

### サブタスクCでも実行しなかった・実行不可の検証（残存）

- **外部 API（Mistral / Tavily / LangSearch / DDGS / yfinance / TradingView / Yahoo JP）への実接続**: 指示により外部接続を伴う検証は実施しない。実装はタイムアウト・レート制限・サーキットブレーカー・フォールバックで防御されており、静的解析＋モック前提テストで整合性を確認した。
- **ネイティブホストの実プロセス起動・ブラウザ実機での UI 挙動**: 実行不可・未実施。構造とテスト（`test_native_host*.py`, `test_start_backend.py`）で検証済み。UI は静的解析のみ。
- **`tests/startup_smoke_runner.py`**: 未実行。`MNS_SKIP_BOOTSTRAP=0` を要求し、バックグラウンド起動・ネットワーク I/O（yfinance/ニュース/トレンド warmup）を伴うため、外部接続を避ける方針の下で省略。全スイート・`AppState()` 新規構築回帰テスト（`test_app_state_lifecycle.py`）で代替検証済み。
- **`npm audit`**: ネットワーク依存のため未実行（CI の frontend ジョブで検証される設計）。`npm ci --dry-run`（ロック整合性）は実行済み。
- **bandit / pip-audit / CDN SRI ハッシュ検証**: セキュリティスキャンは今回の Python 変更（R3-1）と直接関係がなく、ネットワーク依存（pip-audit）を含むため省略。

---

## 6. 残存リスク

1. **yfinance 内部 API 依存**: `session_manager.reset_yfinance_auth()` は yfinance の内部属性（`_crumb`, `_cookie`）にアクセスする。yfinance バージョンアップで壊れる可能性（`app_state.initialize_yfinance_cache` が 1.5.x 以外を警告する設計で緩和）。
2. **外部サイト構造依存**: Yahoo JP / Kabutan / SBI / Minkabu / TradingView のスクレイピングに依存。サイト構造変更で取得停止の可能性（フォールバックチェーンと graduated cooldown で緩和）。
3. **インメモリ単一状態**: 単一ワーカー必須（`wsgi.py` / `gunicorn.conf.py` で fail-closed 強制）。`MNS_WORKER_VALIDATION=0` を誤設定した環境ではデータ不整合の可能性。
4. **SSE チケットの In-Memory 管理**: `_SSE_TICKETS` はプロセス内メモリで 500 件上限。再起動で失われるが、チケットは短命（120秒）のため実害なし。
5. **レート制限の In-Memory 実装**: 再起動でリセット。個人利用向けとして文書化済み。
6. **ブラウザ実機での UI 検証未実施**: アクセシビリティ・レスポンシブ・実ブラウザ互換性は静的解析のみ。
7. **R3-1 の既存キャッシュ移行**: 旧 `BASE_DIR/.cache` のファイルは削除・コピーせず放置（初回ミス時に再取得）。初回起動時にディスクキャッシュ未命中（再フェッチ）が1回発生し得るが、機能・表示への影響はない（キャッシュはベストエフォート）。
8. **`ruff format --check` の既存差異**: リポジトリ全体では 46 ファイルが未フォーマット（CI の ruff ゲートは `ruff check .` のみで、`ruff format` はゲート対象外）。`app_state.py:157` も HEAD 時点から未フォーマット（今回の変更起因ではない）。新規追加行（`app_state.py:227-246`・`test_app_state_lifecycle.py` 全体）はフォーマット準拠。

---

## 7. 既存ユーザー変更の保護確認記録

- サブタスクAは Architect モード（`.md` のみ編集可）で実施し、コード・設定ファイル・ランタイム成果物の編集は一切行わなかった。
- サブタスクB/C は `app_state.py`・`tests/test_app_state_lifecycle.py`・本報告書のみを編集した。
- `git reset --hard` / `git checkout -- .` / `git clean -fd` 等の作業を失わせる操作は行わない（実行していない）。
- commit / push / タグ / PR 作成は行わない。
- テスト実行は安全なものに限定（外部接続を伴うテストは実行せず、モック/ローカル/dry-run 優先）。`AppState()` 新規構築や `create_app()` はテスト・スモークで代替検証。
- ワークスペースルートのランタイム成果物（`config.json.bak`, `backend.log`, `user_stocks.lock`, `.backend.start.lock`, `config.json.template` 等）は既存ユーザーの作業として保護対象とし、削除・変更・参照先の改変を行わなかった。本報告書でも値の詳細を表示しない。
- **サブタスクCの検出性検証**では `app_state.py` を一時的に HEAD 状態へ変更したが、検証後に必ず修正状態へ復元済み（`git diff` で復元を確認、回帰テスト 3 passed を再確認）。

---

## 付録A. サブタスクCの検証結果（実行コマンド一覧）

サブタスクCで実行した実検証の結果を記録する。環境: Windows 11 / Python 3.14.6 / uv 0.11.25 / Node v24.19.0。

### A-1 テスト

| 実行 | 結果 |
| --- | --- |
| `uv run --locked --group test python -m pytest tests/test_app_state_lifecycle.py tests/test_disk_cache.py tests/test_disk_cache_extra.py tests/test_indices_cache.py tests/test_build_stock_payload.py tests/test_market_data_service.py tests/test_leader_election.py tests/test_review_fixes_all.py -q --timeout=120` | ✅ パス（63 passed） |
| `uv run --locked --group test python -m pytest tests/test_api_system.py tests/test_experimental_orbit.py tests/test_stock_provider.py tests/test_review_r1_r4_fixes_current.py tests/test_stock_payload_extra.py -q --timeout=120` | ✅ パス（118 passed） |
| `uv run --locked --group test python -m pytest tests/test_app_state_lifecycle.py tests/test_route_helpers.py tests/test_api_integration.py tests/test_config_utils.py tests/test_config_utils_extra.py tests/test_review_fix_regressions.py tests/test_review_fix_regressions_v2.py -q --timeout=120` | ✅ パス（87 passed, 1 skipped = POSIX-only permission check） |
| `uv run --locked --group test python -m pytest tests/ -q --timeout=60 --timeout-method=thread --cov=. --cov-report=term --cov-fail-under=68` | ✅ 全パス・coverage 78.55%（`--cov-fail-under=68` を上回る） |
| 検出性検証（`app_state.py` を一時的に HEAD 状態へ）`pytest tests/test_app_state_lifecycle.py` | ✅ 期待どおり 2 件失敗（回帰テストが障害を検出）。その後修正を復元し 3 passed |

### A-2 lint / format

| 実行 | 結果 |
| --- | --- |
| `uv run --locked --group lint ruff check app_state.py tests/test_app_state_lifecycle.py --line-length=100` | ✅ All checks passed |
| `uv run --locked --group lint ruff check . --line-length=100` | ✅ All checks passed |
| `uv run --locked --group lint flake8 app_state.py tests/test_app_state_lifecycle.py --count --select=E9,F63,F7,F82 --show-source --statistics` | ✅ 0 件 |
| `uv run --locked --group lint flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics` | ✅ 0 件 |
| `uv run --locked --group lint pylint --errors-only --disable=import-error app.py config_utils.py trend_sources.py native_host/native_host.py routes/ services/ utils/ app_state.py` | ✅ 0 件 |
| `uv run --locked --group lint ruff format --check tests/test_app_state_lifecycle.py` | ✅ 1 file already formatted |
| `uv run --locked --group lint ruff format --check app_state.py` | ⚠️ 変更行は準拠。`app_state.py:157` が未フォーマット（HEAD にも存在する既存差異・CI ゲート外） |

### A-3 型チェック

| 実行 | 結果 |
| --- | --- |
| `uv run --locked --group typecheck mypy` | ✅ Success: no issues found in 50 source files |
| `uv run --locked --group typecheck pyrefly check .` | ✅ 0 errors（17 suppressed, 13 warnings not shown） |

### A-4 フロントエンド / ビルド / ロック整合性

| 実行 | 結果 |
| --- | --- |
| `npm run typecheck` | ✅ パス |
| `npm run verify-generated` | ✅ static/js/api_client.js matches the TypeScript output |
| `npm run lint` | ✅ パス |
| `npx prettier --check "static/js/**/*.js" "chrome_extension/**/*.js"` | ✅ All matched files use Prettier code style! |
| `npm ci --dry-run` | ✅ up to date（package-lock 整合） |
| `uv lock --locked --check` | ✅ Resolved 133 packages（uv.lock 最新） |
| `uv export --locked --no-hashes --no-dev` と `requirements-locked.txt`（先頭3行除く）突合 | ✅ 完全一致（234 行） |

### A-5 git 検証

| 実行 | 結果 |
| --- | --- |
| `git status --porcelain` | ` M app_state.py` / ` M tests/test_app_state_lifecycle.py` / `?? plans/current_head_code_review_report.md`（ユーザー既存変更は保持） |
| `git log -1 --oneline` | `eefa8e9 ci: implement CI pipeline, add regression tests, and centralize error payload handling` |
| `git ls-files config.json.template` | 追跡済み（R3-2 確定） |
| `git check-ignore -v config.json.template` | 無視対象外（exit 1）（R3-2 確定） |
