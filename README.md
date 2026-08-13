# Mistral NeX Stocks

Flask ベースのローカルファースト株式ダッシュボードです。市場データ取得、AI 分析、ニュース集約、ポートフォリオ管理、Chrome/Edge 拡張連携、Windows ネイティブホストをひとつのリポジトリで提供します。

This project is a local-first stock dashboard built with Flask. It combines market data retrieval, AI analysis, news aggregation, portfolio tracking, browser extension integration, and a Windows native messaging host in one repository.

## 概要 / Overview

- 日本語: 個人利用を前提に、リアルタイム株価、ヒートマップ、スクリーナー、AI 分析、ニュース検索をまとめて扱えるようにしています。
- English: The app is designed for personal use and brings real-time quotes, heatmaps, screeners, AI analysis, and news search into one place.
- 日本語: セキュリティは CSRF、Origin チェック、rate limit、shutdown token、暗号化保存を中心に構成しています。
- English: Security is centered around CSRF, origin checks, rate limiting, shutdown tokens, and encrypted secret storage.

## 主な機能 / Key Features

- リアルタイム株価、ヒートマップ、スクリーナー表示
  - English: Live quotes, heatmaps, and screening views.
- Mistral LLM を使った銘柄分析、チャット、テクニカル補助生成
  - English: Stock analysis, chat, and technical line generation powered by Mistral.
- DDGS / LangSearch / Tavily を組み合わせたニュース・調査検索
  - English: News and research search that can blend DDGS, LangSearch, and Tavily.
- yfinance、TradingView、Yahoo! Finance JP 由来の市場データ統合
  - English: Market data aggregation from yfinance, TradingView, and Yahoo! Finance Japan sources.
- API キーの暗号化保存（keyring / DPAPI）
  - English: Encrypted secret storage via keyring or Windows DPAPI.
- Chrome / Edge 拡張と native host を使ったローカル連携
  - English: Local browser integration via a Chrome / Edge extension and native host.
- CSRF、Origin、rate limit、shutdown token を組み合わせた防御
  - English: Defense-in-depth with CSRF, origin checks, rate limiting, and shutdown tokens.

## プロジェクト構成 / Project Structure

- [app.py](app.py) - Flask アプリの生成、セキュリティ初期化、ブループリント登録、bootstrap / App factory, security setup, blueprint registration, and runtime bootstrap.
- [app_bg.py](app_bg.py) - バックグラウンド同期、SSE 補完、yfinance 収集 / Background sync, SSE supplementation, and yfinance collection.
- [app_state.py](app_state.py) - アプリ全体の状態管理 / Shared application state.
- [routes/](routes) - ページ表示と API ルート / Page handlers and API blueprints.
- [services/](services) - AI、検索、株価、ニュース、リアルタイムエンジン / AI, search, stock, news, and realtime services.
- [utils/](utils) - 検証、正規化、キャッシュ、ネットワーク、保存ユーティリティ / Validation, normalization, caching, networking, and storage helpers.
- [static/](static) - CSS / JavaScript / 静的ファイル / Stylesheets, scripts, and static assets.
- [templates/](templates) - Jinja2 テンプレート / Jinja2 templates.
- [chrome_extension/](chrome_extension) - ブラウザ拡張 / Browser extension.
- [native_host/](native_host) - Windows native messaging host / Windows native messaging host.
- [tests/](tests) - pytest ベースのテスト / Pytest-based tests.

## 画面と API / Screens and APIs

### 画面 / Pages

- `/` / `/setup` - 初期設定画面 / Initial setup page.
- `/main` - メインダッシュボード / Main dashboard.
- `/heatmap` - ヒートマップ / Heatmap view.
- `/screener` - スクリーナー / Screener page.
- `/settings` - 設定画面 / Settings page.
- `/experimental/orbit` - 実験的表示モード「Market Observatory」（深宇宙市場観測所） / Experimental Market Observatory mode.

### 主要 API / Main APIs

- `routes/api_system.py`
  - `/api/credentials`
  - `/api/health`
  - `/api/cache-stats`
  - `/api/metrics`
  - `GET /api/csrf-token`
  - `/api/csp-report`
  - `/api/shutdown`
- `routes/api_stocks.py`
  - `/api/indices`
  - `/api/stocks`
  - `/api/stock-details`
  - `/api/stock-history`
  - `/api/search`
  - `/api/screener`
  - `/api/stocks/add`
  - `/api/stocks/delete`
  - `/api/stocks/portfolio`
  - `/api/stocks/portfolio/snapshot`
  - `/api/stocks/add_ext`
  - `/api/stocks/reset`
  - `/api/heatmap`
  - `POST /api/stocks/stream/ticket`
  - `/api/stocks/stream`
  - `GET /api/ai-portfolio`
  - `POST /api/ai-portfolio/generate`
  - `POST /api/ai-portfolio/rebalance`
  - `POST /api/ai-portfolio/save`
  - `DELETE /api/ai-portfolio/custom`
  - `POST /api/ai-portfolio/copy-to-my`
- `routes/api_analysis.py`
  - `/api/trending`
  - `/api/chat`
  - `/api/news`
  - `/api/analyze-v2`
  - `/api/ai-technical-lines`

## セットアップ / Setup

1. Python 3.11–3.14 を用意する / Install Python 3.11–3.14 (the CI-supported matrix).
2. 依存関係をインストールする / Install Python dependencies.

   ```bash
   uv sync --locked
   ```

3. フロントエンドの開発ツールも使う場合は Node.js 依存を入れる / Install the Node.js dependencies if you also want the frontend tooling.

   ```bash
   npm install
   ```

4. 必要な API キーを設定画面で登録する / Register your API keys in the Settings page.
5. 起動する / Start the app.

   ```bash
   uv run --locked python app.py
   ```

6. ブラウザで `http://localhost:5000` を開く / Open `http://localhost:5000` in your browser.

## 開発用コマンド / Development Commands

- `uv run --locked --group test pytest -q` - Python テスト / Python test suite.
- `npm run typecheck` - TypeScript 型チェック / TypeScript type checking.
- `npm run verify-generated` - 生成成果物検証(静的生成 `api_client.js` が `api_client.ts` と同期しているか) / Verify generated frontend artifact is in sync.
- `npm run lint` - JavaScript / 拡張機能の lint / JavaScript and extension linting.
- `npm run build` - 型チェック、compile、lint、Prettier 検証(注意: `verify-generated` は含まない。CIの `Verify generated frontend artifact` は別途 `npm run verify-generated` で実行) / Typecheck, compile, lint, and Prettier checks (note: does NOT include `verify-generated`; CI runs it separately).
- `npm audit --audit-level=high` - npm 依存関係の脆弱性監査(High以上で失敗。CI `Audit npm dependencies` と同等) / Audit npm dependencies (fails on high or above, same as CI).

## 重要な設定項目 / Important Configuration

| 環境変数                                    | デフォルト値 / Default   | 役割 / Role                                                                                                                                                                                                            |
| ------------------------------------------- | ------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `FLASK_SECRET_KEY`                          | `auto`                   | Flask セッション鍵。未設定時は開発向けに自動生成される / Flask session key. Auto-generated for development when unset.                                                                                                 |
| `MNS_MASTER_KEY`                            | `auto`                   | 保存済みシークレットの暗号化鍵。headless / keyring 非対応環境では永続値を設定 / Master key; set a persistent value when no keyring is available.                                                                       |
| `MNS_EPHEMERAL_FALLBACK`                    | `0`                      | 一時キー利用の保護フラグ。これだけでは永続化されない / Guard for ephemeral keys; this alone does not make a key persistent.                                                                                            |
| `MNS_ALLOW_EPHEMERAL_MASTER_KEY`            | `0`                      | 開発・テスト限定の非永続マスターキー明示許可。再起動後に復号不能 / Development/test-only opt-in; data cannot be decrypted after restart.                                                                               |
| `MNS_ADMIN_TOKEN`                           | `none`                   | 設定時はすべての保護 API が `X-MNS-Admin-Token` ヘッダーを要求します（ブラウザ UI はヘッダーを送信できないため非対応。下記セキュリティメモ参照）/ When set, every protected API requires the `X-MNS-Admin-Token` header (the browser UI cannot send it and is incompatible; see the security notes below).
| `MNS_ALLOW_REMOTE_API`                      | `0`                      | reverse proxy 経由のアクセスを許可するフラグ / Enables reverse-proxy access.                                                                                                                                           |
| `MNS_PROXY_FIX`                             | `0`                      | ProxyFix を有効化するフラグ / Enables Werkzeug ProxyFix.                                                                                                                                                               |
| `CSP_ENFORCE`                               | `1`                      | CSP を強制するか Report-Only にするか / Controls whether CSP is enforced or report-only.                                                                                                                               |
| `MNS_COOKIE_SECURE`                         | `0`                      | セッションクッキーの Secure 属性 / Forces Secure cookies.                                                                                                                                                              |
| `MNS_BACKEND_PORT`                          | `5000`                   | バックエンドサーバーのポート番号 / Backend server port.                                                                                                                                                                |
| `DDGS_TIMEOUT`                              | `5`                      | DuckDuckGo News 検索のタイムアウト / DuckDuckGo News timeout.                                                                                                                                                          |
| `MNS_MISTRAL_API_TIMEOUT`                   | `60.0`                   | Mistral API のタイムアウト / Mistral API timeout.                                                                                                                                                                      |
| `MNS_MISTRAL_MIN_INTERVAL`                  | `1.35`                   | Mistral API 呼び出しの最小間隔 / Minimum interval between Mistral requests.                                                                                                                                            |
| `MNS_MISTRAL_BASE_URL`                      | `https://api.mistral.ai` | Mistral API のベースURL（プロキシ/セルフホスト対応。SDKが `/v1` などのバージョンパスを自動付与するため `/v1` は付けないこと）/ Mistral API base URL (the SDK appends versioned paths itself, so do not include `/v1`). |
| `MNS_MISTRAL_SDK_RETRIES`                   | `2`                      | SDK内リトライ回数（一時的5xx/接続エラー）/ Transient retries handled inside the SDK.                                                                                                                                   |
| `MNS_MISTRAL_JITTER_FACTOR`                 | `0.1`                    | レート制限待ちのジッター係数（バースト防止）/ Jitter factor for the rate-limit wait.                                                                                                                                   |
| `MNS_MISTRAL_REASONING_MODELS_EXTRA`        | _(空)_                   | reasoning_effort対応モデルの追加（カンマ区切り）/ Extra reasoning-capable model IDs.                                                                                                                                   |
| `MNS_CHAT_CONTEXT_MAX_CHARS`                | `6000`                   | チャット履歴の最大文字数（LLM送信分）/ Max chat-history chars sent to the LLM.                                                                                                                                         |
| `MNS_STREAM_CHAT_MAX_CONCURRENT`            | `2`                      | チャットSSEストリーミングの同時実行上限（超過は503）/ Max concurrent chat SSE streams (excess returns 503).                                                                                                            |
| `MNS_RATE_LIMIT_MAX_TOKEN_POLLS`            | `120`                    | request_token ごとのポーリング重複スキップ上限。超過分は通常のクォータを消費します / Max polling-duplicate skips per request_token before same-token requests count against the quota.                                 |
| `MNS_YFINANCE_SHORT_CACHE_TTL`              | `300`                    | yfinance の短期キャッシュ TTL / Short cache TTL for yfinance data.                                                                                                                                                     |
| `MNS_YFINANCE_REQ_MIN_INTERVAL_BASE`        | `0.5`                    | yfinance リクエスト最小間隔ベース / Base min interval for yfinance requests.                                                                                                                                           |
| `MNS_YFINANCE_MAX_CONCURRENT_REQUESTS`      | `3`                      | yfinance 同時リクエスト最大数 / Max concurrent yfinance requests.                                                                                                                                                      |
| `MNS_YFINANCE_SESSION_POOL_MAX`             | `64`                     | yfinance セッションプール最大数 / Max yfinance session pool size.                                                                                                                                                      |
| `MNS_YFINANCE_SESSION_RECLAIM_INTERVAL_SEC` | `600`                    | yfinance セッション回収間隔 / yfinance session reclaim interval.                                                                                                                                                       |
| `MNS_YFINANCE_SESSION_IDLE_TTL_SEC`         | `3600`                   | yfinance セッションアイドル TTL / yfinance session idle TTL.                                                                                                                                                           |
| `MNS_SCRAPER_BACKOFF_INITIAL`               | `60`                     | スクレイパー遮断時の初期バックオフ / Initial backoff when web scrapers are blocked.                                                                                                                                    |
| `MNS_SCRAPER_BACKOFF_MAX`                   | `600`                    | スクレイパー遮断バックオフの上限 / Max web scraper block backoff.                                                                                                                                                      |
| `MNS_SCRAPER_BACKOFF_MULTIPLIER`            | `2.0`                    | スクレイパー遮断バックオフの倍率 / Web scraper block backoff multiplier.                                                                                                                                               |
| `MNS_SCRAPER_MAX_WORKERS`                   | `3`                      | 日本株スクレイパーの並列ワーカー数 / Max parallel JP scraper workers per cycle.                                                                                                                                        |
| `MNS_SCRAPER_REQUEST_STAGGER_SEC`           | `0.1`                    | スクレイパー並列時の送信間隔 / Stagger delay between scraper submissions.                                                                                                                                              |
| `MNS_SIMULATE_FLUCTUATION`                  | `1`                      | mode1 補完 SSE の擬似変動を有効化するか / Enable artificial noise in complementary SSE.                                                                                                                                |
| `MNS_MAX_SSE_LISTENERS`                     | `64`                     | プロセス全体での SSE リスナー最大接続数。Gunicorn のスレッド数はこの値に通常リクエスト用6枠を加えた値になります / Process-wide maximum active SSE listeners. Gunicorn derives its thread count as this value plus six normal-request slots. |
| `MNS_SSE_EVENT_LOG_MAX`                     | `500`                    | SSE リプレイバッファの最大イベント数 / Max SSE replay-buffer events.                                                                                                                                                   |
| `MNS_SSE_MODE2_FULL_SNAPSHOT_INTERVAL_SEC`  | `5.0`                    | モード2 の定期フルスナップショット間隔（秒）/ Mode-2 periodic full snapshot interval (sec).                                                                                                                            |
| `MNS_NEGATIVE_CACHE_TTL`                    | `90`                     | 失敗キャッシュ TTL / Negative cache TTL.                                                                                                                                                                               |
| `NATIVE_HOST_MAX_MESSAGE_BYTES`             | `1048576`                | native host のメッセージ上限 / Native host message size limit.                                                                                                                                                         |

## セキュリティメモ / Security Notes

- このアプリはローカル個人利用を前提に設計されています。 / The app is designed for local, personal use.
- credentials API は CSRF と local-origin の両方で保護されます。 / The credentials API is protected by CSRF and local-origin checks.
- `MNS_ALLOW_REMOTE_API=1` の場合は `MNS_ADMIN_TOKEN` が必須です。 / When `MNS_ALLOW_REMOTE_API=1`, `MNS_ADMIN_TOKEN` is required.
- `MNS_ADMIN_TOKEN` を設定すると、SSE チケット発行 POST や /api/stocks/stream を含む**すべての保護 API** が `X-MNS-Admin-Token` ヘッダーを要求します。ブラウザ UI はこのヘッダーを送信できないため、ダッシュボード全体が 403 になり**使用できなくなります**（SSE だけが使えなくなるのではなく、30秒ポーリングのフォールバック自体も同じ 403 のエンドポイントを呼ぶため機能しません）。トークンはヘッダーを送信できる非ブラウザ / API クライアント専用です。個人利用のローカル環境ではトークンを設定しないでください。 / When `MNS_ADMIN_TOKEN` is set, **every** protected API (including the SSE ticket POST and /api/stocks/stream) requires the `X-MNS-Admin-Token` header. The browser UI cannot send this header, so the entire dashboard returns 403 and becomes unusable - there is no SSE-only degradation, and the 30-second polling fallback calls the same 403 endpoints so it does not work either. The token is intended only for non-browser / API clients that can supply the header. Leave it unset for personal localhost use.
- `/api/shutdown` は native host 経由の一時トークンを要求します。 / `/api/shutdown` requires a one-time token from the native host.
- チャット履歴はブラウザのセッション単位で分離されます。共有ブラウザでは履歴も共有されます。 / Chat history is isolated by browser session; shared browser profiles share history.

## 実行の補足 / Deployment Notes

- 開発時は `uv run --locked python app.py` で起動できます。`run_app.sh` はプロジェクトの `.venv` を優先し、存在しない場合は同じ locked 環境を `uv` で起動します。 / For development, run `uv run --locked python app.py`. `run_app.sh` prefers the project `.venv` and otherwise starts the same locked environment through `uv`.
- 配布や運用で WSGI を使う場合、Gunicorn は必ず `gunicorn -c gunicorn.conf.py wsgi:app` で起動してください。この設定は `MNS_MAX_SSE_LISTENERS + 6` の gthread を確保します。本アプリはインメモリ状態依存のため、**単一ワーカープロセス（`workers=1` / `WEB_CONCURRENCY=1`）での起動が必須**です。uWSGI を使う場合も `uwsgi --processes 1 --enable-threads --module wsgi:app` とし、追加プロセスを動的に作る `cheaper` モードは使用できません。 / For WSGI deployment, start Gunicorn only with `gunicorn -c gunicorn.conf.py wsgi:app`; the configuration reserves `MNS_MAX_SSE_LISTENERS + 6` gthreads. Due to in-memory state dependencies, **a single worker process (`workers=1` / `WEB_CONCURRENCY=1`) is strictly required**. With uWSGI, use `uwsgi --processes 1 --enable-threads --module wsgi:app`; dynamic `cheaper` mode is unsupported because it can create more processes.
- Chrome / Edge 拡張を使う場合は [chrome_extension/](chrome_extension) を読み込み、native host は [native_host/](native_host) のインストーラを使います。 / For the browser extension, load [chrome_extension/](chrome_extension) and install the native host from [native_host/](native_host).
- Native host の `native_host.cmd` と `com.mistral_nex_stocks.host.json` は、Python パスと拡張機能 ID を含むマシン固有の生成物で、Git 管理外です。インストール後は `diagnose_native_host_windows.ps1` で登録状態を診断し、構造だけ確認する場合は `validate_native_host_windows.ps1` を使ってください。 / The generated native-host launcher and manifest contain machine-specific Python paths and extension IDs and are intentionally ignored by Git. Diagnose an installed host with `diagnose_native_host_windows.ps1`, or run the read-only structural validator with `validate_native_host_windows.ps1`.

## ライセンス / License

### AIポートフォリオの対象市場

AIポートフォリオの構成銘柄は米国株（`us`）と日本株（`jp`）のみを対象とします。指数ウォッチリスト（`idx`）は通常の株価APIでは利用できますが、AIポートフォリオの生成・保存・マイポートフォリオへのコピー対象外です。

MIT License
