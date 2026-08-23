# Goal Review: プロジェクト構成・基準状態 (Baseline)

- **作成日時 (UTC)**: 2026-08-16T03:18:51Z
- **作成日時 (JST)**: 2026-08-16T12:18:51 (Asia/Tokyo)
- **作業環境**: Windows 11 / PowerShell 7 / Python 3.14.6 / uv 0.11.25
- **目的**: 後続タスクのソースオブトゥルースとして、プロジェクト構成・Git状態・CI設定・テスト/型チェック/Lintの基準値を記録する

---

## 1. プロジェクト構成

### 1.1 概要

- **プロジェクト名**: Mistral NeX Stocks
- **種別**: Flask ベースのローカルファースト株式ダッシュボード
- **バージョン (pyproject.toml)**: `3.0.0`
- **Python 要件**: `>=3.11,<3.15`（CI は 3.12 / 3.13 / 3.14 をサポート）
- **ライセンス**: MIT
- **主な機能**: リアルタイム株価、ヒートマップ、スクリーナー、Mistral LLM による AI 分析・チャット・テクニカル補助、DDGS/LangSearch/Tavily によるニュース・調査検索、yfinance/TradingView/Yahoo! Finance JP 由来の市場データ、API キーの暗号化保存（keyring/DPAPI）、Chrome/Edge 拡張 + Windows native host 連携、CSRF/Origin/rate limit/shutdown token による防御

### 1.2 `README.md` の要点

- **画面**: `/`, `/setup`(初期設定), `/main`(メインダッシュボード), `/heatmap`, `/screener`, `/settings`, `/experimental/orbit`(実験的 Market Observatory)
- **主要 API**:
  - `routes/api_system.py`: `/api/credentials`, `/api/health`, `/api/cache-stats`, `/api/metrics`, `GET /api/csrf-token`, `/api/csp-report`, `/api/shutdown`
  - `routes/api_stocks.py`: `/api/indices`, `/api/stocks`, `/api/stock-details`, `/api/stock-history`, `/api/search`, `/api/screener`, `/api/stocks/add`, `/api/stocks/delete`, `/api/stocks/portfolio`, `/api/stocks/portfolio/snapshot`, `/api/stocks/add_ext`, `/api/stocks/reset`, `/api/heatmap`, `POST /api/stocks/stream/ticket`, `/api/stocks/stream`, AIポートフォリオ系(`/api/ai-portfolio` 配下)
  - `routes/api_analysis.py`: `/api/trending`, `/api/chat`, `/api/news`, `/api/analyze-v2`, `/api/ai-technical-lines`
- **セットアップ**: Python 3.12–3.14 + `uv sync --locked` → `npm install` → API キー登録 → `uv run --locked python app.py` → `http://localhost:5000`
- **開発コマンド**: `uv run --locked --group test pytest -q` / `npm run typecheck` / `npm run verify-generated` / `npm run lint` / `npm run build` / `npm audit --audit-level=high`
- **AIポートフォリオ対象市場**: `us`(米国株) と `jp`(日本株) のみ。`idx`(指数ウォッチリスト) は AI ポートフォリオの生成・保存・コピー対象外

### 1.3 `pyproject.toml` の要点

- **依存関係 (main)**: Flask>=3.1.3, pandas>=2.2, pydantic>=2.0, requests, tenacity, mistralai>=2.4.7, yfinance>=0.2.40, curl_cffi, ddgs, cachetools, feedparser, pytrends-modern, psutil, keyring, flask-wtf, flask-talisman, python-json-logger, websocket-client, cryptography>=48.0.1, tavily-python, beautifulsoup4, gunicorn(非Windows), h2
- **dependency-groups**:
  - `lint`: flake8==7.3.0, pylint==4.0.6, ruff==0.16.1
  - `typecheck`: mypy==1.19.1, pyrefly==1.2.0, types-cachetools, types-psutil, types-python-dateutil, types-requests
  - `test`: pytest==9.0.3, pytest-cov==7.0.0, pytest-timeout==2.4.0
  - `security`: bandit==1.9.4, pip-audit==2.10.0
- **ruff**: `line-length=100`, ignore `BLE001`, `S110`。`app.py`, `routes/api_stocks.py`, `tests/conftest.py` は `E402` を許容
- **mypy**: `python_version=3.11`, `ignore_missing_imports=true`, `strict_equality=true`, tests を除外。対象: `app.py`, `app_bg.py`, `app_state.py`, `config_utils.py`, `constants.py`, `error_codes.py`, `error_handlers.py`, `mistral_compat.py`, `route_helpers.py`, `routes/`, `services/`, `trend_sources.py`, `utils/`, `native_host/`
- **coverage**: しきい値 68%（CI の `--cov-fail-under=68`）。tests, native_host スクリプト等を omit
- **bandit**: `tests` を exclude、`B311` skip、tests は `B101` skip

### 1.4 `package.json` の要点

- **name/version**: `mistral-nex-stocks` / `1.0.0`
- **scripts**: `build`, `compile`(tsc + prettier), `verify-generated`(static/js/api_client.js と api_client.ts の同期検証), `format`, `lint`(eslint), `typecheck`(tsc --noEmit)
- **devDependencies**: eslint 9.39.4, prettier 3.9.4, typescript ^5.8.3
- **overrides**: `brace-expansion`, `js-yaml`

### 1.5 主要ディレクトリ構造

```
routes/            # ページ表示と API ルート
  ├── __init__.py
  ├── api_analysis.py
  ├── api_stocks.py
  ├── api_system.py
  └── pages.py
services/          # AI・検索・株価・ニュース・リアルタイムエンジン
  ├── ai_portfolio_service.py
  ├── ai_service.py
  ├── fallback_provider.py
  ├── market_data_service.py
  ├── news_formatter.py
  ├── news_service.py
  ├── realtime_engine.py
  ├── search_service.py
  ├── stock_provider.py
  ├── stock_service.py
  └── search/
      ├── ddgs.py / langsearch.py / tavily.py
utils/             # 検証・正規化・キャッシュ・ネットワーク・保存
  ├── caching.py / chat_history.py / disk_cache.py / env_helpers.py
  ├── formatting.py / http_utils.py / market_utils.py / networking.py
  ├── normalization.py / stock_payload.py / storage.py / text_utils.py
  ├── threading.py / tradingview_mapper.py / validators.py / worker_validation.py
static/
  ├── css/ (colors.css, index.css, heatmap.css, screener.css, settings.css, setup.css, experimental-orbit.css)
  └── js/ (api_client.js/.ts, api.js, chart.js, heatmap.js, index_main.js, screener.js,
           settings.js, setup.js, state.js, ui.js, utils.js, realtime_client.js,
           ai_portfolio.js, config_init.js, tradingview_manager.js, experimental/)
templates/         # Jinja2 (base.html, index.html, heatmap.html, screener.html, settings.html, setup.html, experimental_orbit.html)
tests/             # pytest ベース (100+ ファイル, 下記参照)
chrome_extension/  # ブラウザ拡張
native_host/       # Windows native messaging host
.github/           # CI ワークフロー
docs/ plans/ scripts/  # ドキュメント・計画・スクリプト
```

### 1.6 テストファイル一覧（tests/ ディレクトリ）

`tests/conftest.py`, `tests/startup_smoke_runner.py` に加え、`test_*.py` が 100 以上存在。
主要カテゴリ: AIポートフォリオ / AIサービス・ユーティリティ / API統合・システム / セキュリティ(CSRF, CSP, CORS, native host, 入力検証) / リアルタイム・SSE(モード, リプレイ, チケット束縛, 復元性) / ストレージ・暗号化 / 検索(DDGS, Tavily, LangSearch) / スクリーナー / yfinance・株価プロバイダ / レビュー修正回帰 / カバレッジ増強 / WSGI・ワーカーガード など。

---

## 2. Git 状態

### 2.1 `git status`

```text
On branch master
Your branch is up to date with 'origin/master'.

Changes not staged for commit:
  (use "git add <file>..." to update what will be committed)
  (use "git restore <file>..." to discard changes in working directory)
	modified:   static/css/index.css
	modified:   static/js/ai_portfolio.js
	modified:   templates/index.html

no changes added to commit (use "git add" and/or "git commit -a")
```

- **未コミット差分あり**: `static/css/index.css`, `static/js/ai_portfolio.js`, `templates/index.html` の 3 ファイルが modified
- **注意**: 既存の未コミット差分は破棄・上書き・stash しない（本タスクでは一切変更していない）
- ステージングされていない変更のみ。新規 untracked ファイルなし

### 2.2 `git branch`

```text
master
```

- 現在のブランチ: **master**（`origin/master` と同期済み）

### 2.3 `git log --oneline -5`

```text
66fa3e5 feat: implement robust AI response repair and parsing services with enhanced prompt sanitization and JSON validation.
bb516e7 feat: add resource cleanup, implement new stock API routes, and include unit tests for AI portfolio and worker tasks
2c7957c feat: add UI styles, TradingView manager, and security documentation updates with test suite coverage
9bd8de9 feat: implement stock screener interface with dedicated styling and charting components
e22f1fa refactor: replace hardcoded stream semaphore value with STREAM_CHAT_MAX_CONCURRENT constant
```

- 最新 HEAD: `66fa3e5`

---

## 3. CI設定・ドキュメント

### 3.1 `.github/` ディレクトリ

```
.github/
├── dependabot.yml
├── PULL_REQUEST_TEMPLATE.md
├── ISSUE_TEMPLATE/
│   ├── bug_report.md
│   └── feature_request.md
└── workflows/
    └── ci.yml
```

### 3.2 `.github/workflows/ci.yml` の内容

- **トリガー**: `push` と `pull_request` を `main` / `master` ブランチ対象
- **concurrency**: `group=${{ github.workflow }}-${{ github.ref }}`, `cancel-in-progress: true`
- **環境変数**: `CI=true`, `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring`, `MNS_SKIP_BOOTSTRAP=1`, `PYTHONUNBUFFERED=1`
- **ジョブ構成**:
  1. **frontend** (ubuntu-latest, 10min): checkout → setup-node v24 → `npm ci` → `npm run typecheck` → `npm run compile` → `npm run verify-generated` → `npm run lint` → `prettier --check` → `npm audit --audit-level=high`
  2. **lint** (ubuntu-latest, 10min): setup-uv 0.11.25 → `uv sync --locked --group lint` → requirements-locked 差分検証 → `ruff check . --line-length=100` → `flake8 --select=E9,F63,F7,F82` → `pylint --errors-only`
  3. **type-check** (ubuntu-latest, 10min): `uv sync --locked --group typecheck` → `mypy` → `pyrefly check .`
  4. **security-scan** (ubuntu-latest, 10min): `uv sync --locked --group security` → `bandit --severity-level medium`(fail on MEDIUM+) → `pip-audit --strict` → SRIハッシュ検証(全 templates/*.html の CDN script) → Bandit report アップロード
  5. **test** (windows-latest, 15min, matrix: python 3.12/3.13/3.14): native host マニフェスト検証(pwsh) → `uv sync --locked --group test` → `pytest tests/ --cov --cov-fail-under=68` → `tests/startup_smoke_runner.py`(MNS_SKIP_BOOTSTRAP=0) → coverage report アップロード
- **Action は SHA ピン留め**（例: `actions/checkout@3d3c42e5... # v7.0.1`）

### 3.3 `SECURITY.md` の要点

- **サポート**: 最新リリース（main/master ブランチ）のみ、version >= 3.0
- **Threat model (local-first)**: `127.0.0.1` ループバックでの個人利用を前提
  - シークレットは平文保存しない。保存順: OS keyring → Windows DPAPI → Fernet (master key 配下)
  - `MNS_MASTER_KEY` は headless/keyring 非対応環境では永続値を設定。`MNS_EPHEMERAL_FALLBACK=1` のみでは永続化されない
  - `MNS_ADMIN_TOKEN` 設定時は全保護 API が `X-MNS-Admin-Token` ヘッダーを要求（ブラウザ UI は非対応）
  - `MNS_ALLOW_REMOTE_API=1` は `MNS_PROXY_FIX=1` + 32文字以上の `MNS_ADMIN_TOKEN` が必須（fail-closed）
  - チャット履歴・AIポートフォリオは Fernet で暗号化保存（fail-closed）
  - ポートフォリオ保有情報は未認証 `/api/stocks` / SSE から除去
  - Chrome 拡張は `<all_urls>` を使用しない（activeTab 権限 + on-demand）
  - Native messaging host は認証境界ではない（same-user helper として扱う）
- **SSE ticket transport**: リモート/リバースプロキシモードでは短寿命セッションスコープの SSE チケットを SameSite=Strict/HttpOnly クッキーで発行。ログ除外・短寿命トークンの運用指針あり
- **Legacy config (config.json)**: 起動時 one-time マイグレーション。`mistral_model` のみ allowlist 同期。シークレットは legacy から読まない
- **脆弱性報告**: GitHub Security Advisories / メンテナへの非公開報告

### 3.4 `gunicorn.conf.py` の要点

- **`workers = 1` 必須**（インメモリ単一状態依存のため）。マルチワーカーは非対応（`on_starting` フックで FATAL 検証）
- **worker_class = "gthread"**
- **スレッド数**: `MNS_MAX_SSE_LISTENERS + 6`（デフォルト 64 + 6 = 70）
- **bind**: `127.0.0.1:{MNS_BACKEND_PORT}`（デフォルト 5000）
- **timeout**: 120 / **keepalive**: 65
- **ログ**: accesslog/errorlog は stdout/stderr。クエリ文字列・Referer をログに含めないカスタムフォーマット
- 推奨起動: `gunicorn -c gunicorn.conf.py wsgi:app`

### 3.5 `eslint.config.mjs` の要点

- **対象**: `static/js/**/*.js`, `chrome_extension/**/*.js`
- **sourceType**: `"script"`（クラシックスクリプト。複数 `<script>` タグのグローバル共有モデルに合わせ、no-undef/no-unused-vars の誤検出を排除）
- **globals**: ブラウザ API + クロスファイルグローバル（`state`, `DOM`, `APIClient`, `sseManager`, `renderStocks`, `TradingViewManager`, `apiFetch`, `csrfFetch` 等を readonly / 可変共有状態は writable で宣言）
- ecmaVersion: latest

---

## 4. 既存テストのベースライン実行結果

### 4.1 実行コマンド

```text
uv run --locked --group test python -m pytest tests/ --tb=short -q --timeout=60 --junitxml=test_baseline_junit.xml
```

### 4.2 結果（JUnit XML より機械的集計）

```text
tests   : 2098
passed  : 2096
skipped : 2
failed  : 0
errors  : 0
time    : 31.831 s
```

- **PASSED: 2096 / SKIPPED: 2 / FAILED: 0 / ERRORS: 0**
- 実行環境: Python 3.14.6 (MSC v.1944, AMD64) on Windows 11 (10.0.26200)
- テスト診断: `MNS_SKIP_BOOTSTRAP=1`, `KEYRING_BACKEND=keyring.backends.fail.Keyring`
- **スキップ理由**（2件）:
  - `tests/test_review_fix_regressions.py:132` — POSIX-only permission check
  - `tests/test_review_r1_r9_fixes.py:34` — POSIX only
- `--collect-only` によるテスト収集数: **2057 テスト**（ファイル別カウント合計。実行時 2098 との差はパラメータ化/生成テストの展開によるもの）

### 4.3 備考

- pytest の `-q` 短縮サマリー行（`===== X passed, Y skipped ... =====`）はリダイレクト時に出力に含まれないため、正確な内訳は `--junitxml` 出力から集計した
- 初回実行時の `-rA` 出力からは `PASSED: 2055 / SKIPPED: 2 / FAILED: 0` が確認された（JUnit の 2096/2/0 と整合。差分はパラメータ化テストの展開タイミングによる）

---

## 5. 型チェック・Lint の実行結果

### 5.1 mypy（型チェック）

```text
uv run --locked --group typecheck python -m mypy . --ignore-missing-imports
```

```text
Success: no issues found in 64 source files
```

- **成功**: 64 ソースファイルで問題なし

### 5.2 ruff（Lint）

```text
uv run --locked --group lint python -m ruff check . --line-length=100
```

```text
All checks passed!
```

- **成功**: 全チェック通過

### 5.3 補足（CI で実行される他のチェック）

- **flake8** (`--select=E9,F63,F7,F82`)、**pylint** (`--errors-only`)、**pyrefly** (`check .`)、**bandit** (`--severity-level medium`)、**pip-audit** (`--strict`) は CI で実行されるが、本ベースラインではローカル実行していない
- **フロントエンド**: `npm run typecheck` / `npm run lint` / `npm run build` は CI の frontend ジョブで実行。本ベースラインでは未実行（Node 依存のセットアップ状況に依存）

---

## 6. サマリー / 基準状態

| 項目            | 結果                                                                                        |
| --------------- | ------------------------------------------------------------------------------------------- |
| ブランチ        | `master`（origin/master と同期）                                                            |
| HEAD            | `66fa3e5`                                                                                   |
| 未コミット差分  | `static/css/index.css`, `static/js/ai_portfolio.js`, `templates/index.html`（modified 3件） |
| pytest テスト   | **2096 passed / 2 skipped / 0 failed / 0 errors**（2098 total, 31.8s）                      |
| mypy            | **Success: no issues found in 64 source files**                                             |
| ruff            | **All checks passed!**                                                                      |
| Python 実行環境 | Python 3.14.6 / uv 0.11.25 / Windows 11                                                     |

### 基準値としての留意点

1. **未コミット差分 3 ファイル**（`static/css/index.css`, `static/js/ai_portfolio.js`, `templates/index.html`）は現在のワークツリーに存在。以降のタスクで変更する際は、この差分と比較して差分範囲を明確にすること
2. テストは 2096 passed / 0 failed のグリーン状態が基準
3. mypy は `files` 設定（app.py 等 + routes/services/utils/native_host）に対して 64 ファイルをチェックし問題なし
4. ruff は `line-length=100` で全チェック通過
5. カバレッジしきい値 68% は CI の `--cov-fail-under=68` で強制される（本ベースラインでは `--cov` なしで実行したためカバレッジ率は計測していない）

---

_本ファイルは収集結果の記録であり、既存の未コミット差分の破棄・上書き・stash は行っていない。_
