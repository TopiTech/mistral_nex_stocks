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
  - `/api/stocks/stream`
- `routes/api_analysis.py`
  - `/api/trending`
  - `/api/chat`
  - `/api/news`
  - `/api/analyze-v2`
  - `/api/ai-technical-lines`

## セットアップ / Setup

1. Python 3.11 以上を用意する / Install Python 3.11 or newer.
2. 依存関係をインストールする / Install Python dependencies.

   ```bash
   pip install -r requirements.txt
   ```

3. フロントエンドの開発ツールも使う場合は Node.js 依存を入れる / Install the Node.js dependencies if you also want the frontend tooling.

   ```bash
   npm install
   ```

4. 必要な API キーを設定画面で登録する / Register your API keys in the Settings page.
5. 起動する / Start the app.

   ```bash
   python app.py
   ```

6. ブラウザで `http://localhost:5000` を開く / Open `http://localhost:5000` in your browser.

## 開発用コマンド / Development Commands

- `pytest -q` - Python テスト / Python test suite.
- `npm run typecheck` - TypeScript 型チェック / TypeScript type checking.
- `npm run lint` - JavaScript / 拡張機能の lint / JavaScript and extension linting.
- `npm run build` - 型チェック、compile、lint、Prettier 検証 / Typecheck, compile, lint, and Prettier checks.

## 重要な設定項目 / Important Configuration

| 環境変数                                    | デフォルト値 / Default | 役割 / Role                                                                                                            |
| ------------------------------------------- | ---------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `FLASK_SECRET_KEY`                          | `auto`                 | Flask セッション鍵。未設定時は開発向けに自動生成される / Flask session key. Auto-generated for development when unset. |
| `MNS_MASTER_KEY`                            | `auto`                 | 保存済みシークレットの暗号化鍵 / Master key for encrypted secrets.                                                     |
| `MNS_ADMIN_TOKEN`                           | `none`                 | リモート API / 管理系エンドポイントの追加認証 / Extra auth for remote API or admin endpoints.                          |
| `MNS_ALLOW_REMOTE_API`                      | `0`                    | reverse proxy 経由のアクセスを許可するフラグ / Enables reverse-proxy access.                                           |
| `MNS_PROXY_FIX`                             | `0`                    | ProxyFix を有効化するフラグ / Enables Werkzeug ProxyFix.                                                               |
| `CSP_ENFORCE`                               | `1`                    | CSP を強制するか Report-Only にするか / Controls whether CSP is enforced or report-only.                               |
| `MNS_COOKIE_SECURE`                         | `0`                    | セッションクッキーの Secure 属性 / Forces Secure cookies.                                                              |
| `MNS_BACKEND_PORT`                          | `5000`                 | バックエンドサーバーのポート番号 / Backend server port.                                                                |
| `DDGS_TIMEOUT`                              | `5`                    | DuckDuckGo News 検索のタイムアウト / DuckDuckGo News timeout.                                                          |
| `MNS_MISTRAL_API_TIMEOUT`                   | `60.0`                 | Mistral API のタイムアウト / Mistral API timeout.                                                                      |
| `MNS_MISTRAL_MIN_INTERVAL`                  | `1.35`                 | Mistral API 呼び出しの最小間隔 / Minimum interval between Mistral requests.                                            |
| `MNS_YFINANCE_SHORT_CACHE_TTL`              | `300`                  | yfinance の短期キャッシュ TTL / Short cache TTL for yfinance data.                                                     |
| `MNS_YFINANCE_REQ_MIN_INTERVAL_BASE`        | `0.5`                  | yfinance リクエスト最小間隔ベース / Base min interval for yfinance requests.                                           |
| `MNS_YFINANCE_MAX_CONCURRENT_REQUESTS`      | `3`                    | yfinance 同時リクエスト最大数 / Max concurrent yfinance requests.                                                      |
| `MNS_YFINANCE_SESSION_POOL_MAX`             | `64`                   | yfinance セッションプール最大数 / Max yfinance session pool size.                                                      |
| `MNS_YFINANCE_SESSION_RECLAIM_INTERVAL_SEC` | `600`                  | yfinance セッション回収間隔 / yfinance session reclaim interval.                                                       |
| `MNS_YFINANCE_SESSION_IDLE_TTL_SEC`         | `3600`                 | yfinance セッションアイドル TTL / yfinance session idle TTL.                                                           |
| `MNS_SCRAPER_BACKOFF_INITIAL`               | `60`                   | スクレイパー遮断時の初期バックオフ / Initial backoff when web scrapers are blocked.                                    |
| `MNS_SCRAPER_BACKOFF_MAX`                   | `600`                  | スクレイパー遮断バックオフの上限 / Max web scraper block backoff.                                                      |
| `MNS_SCRAPER_BACKOFF_MULTIPLIER`            | `2.0`                  | スクレイパー遮断バックオフの倍率 / Web scraper block backoff multiplier.                                               |
| `MNS_SCRAPER_MAX_WORKERS`                   | `3`                    | 日本株スクレイパーの並列ワーカー数 / Max parallel JP scraper workers per cycle.                                        |
| `MNS_SCRAPER_REQUEST_STAGGER_SEC`           | `0.1`                  | スクレイパー並列時の送信間隔 / Stagger delay between scraper submissions.                                              |
| `MNS_SIMULATE_FLUCTUATION`                  | `1`                    | mode1 補完 SSE の擬似変動を有効化するか / Enable artificial noise in complementary SSE.                                |
| `MNS_MAX_SSE_LISTENERS`                     | `64`                   | SSE リスナー最大接続数 / Maximum active SSE listeners.                                                                 |
| `MNS_NEGATIVE_CACHE_TTL`                    | `90`                   | 失敗キャッシュ TTL / Negative cache TTL.                                                                               |
| `NATIVE_HOST_MAX_MESSAGE_BYTES`             | `1048576`              | native host のメッセージ上限 / Native host message size limit.                                                         |

## セキュリティメモ / Security Notes

- このアプリはローカル個人利用を前提に設計されています。 / The app is designed for local, personal use.
- credentials API は CSRF と local-origin の両方で保護されます。 / The credentials API is protected by CSRF and local-origin checks.
- `MNS_ALLOW_REMOTE_API=1` の場合は `MNS_ADMIN_TOKEN` が必須です。 / When `MNS_ALLOW_REMOTE_API=1`, `MNS_ADMIN_TOKEN` is required.
- `/api/shutdown` は native host 経由の一時トークンを要求します。 / `/api/shutdown` requires a one-time token from the native host.
- チャット履歴はブラウザのセッション単位で分離されます。共有ブラウザでは履歴も共有されます。 / Chat history is isolated by browser session; shared browser profiles share history.

## 実行の補足 / Deployment Notes

- 開発時は `python app.py` で起動できます。 / For development, run `python app.py`.
- 配布や運用で WSGI を使う場合は [wsgi.py](wsgi.py) と [gunicorn.conf.py](gunicorn.conf.py) を参照してください。 / For WSGI deployment, see [wsgi.py](wsgi.py) and [gunicorn.conf.py](gunicorn.conf.py).
- Chrome / Edge 拡張を使う場合は [chrome_extension/](chrome_extension) を読み込み、native host は [native_host/](native_host) のインストーラを使います。 / For the browser extension, load [chrome_extension/](chrome_extension) and install the native host from [native_host/](native_host).

## ライセンス / License

MIT License
