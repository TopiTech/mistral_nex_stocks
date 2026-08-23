# services 領域 自律レビュー結果

- レビュー実施日: 2026-08-16
- 対象HEAD: 現在の作業ディレクトリ（`c:\Users\mibu0\Documents\develop\mistral_nex_stocks_complete_fixed_v3\mistral_nex_stocks_complete_fixed_v3`）
- モード: レビューのみ（コード変更なし）
- 未コミット差分（`static/css/index.css`, `static/js/ai_portfolio.js`, `templates/index.html`）は変更・破棄していない
- 対象スコープ: `services/` 配下（`ai_service.py`, `ai_portfolio_service.py`, `fallback_provider.py`, `market_data_service.py`, `news_service.py`, `news_formatter.py`, `realtime_engine.py`, `search_service.py`, `stock_provider.py`, `stock_service.py`, `services/search/*`）

## レビュー結果サマリー

### レビュー対象ファイル

- [`services/ai_service.py`](services/ai_service.py) — Mistral LLM 呼び出し・JSON修復・テクニカル線生成・ストリーミング
- [`services/ai_portfolio_service.py`](services/ai_portfolio_service.py) — AIポートフォリオ生成・暗号化ストレージ
- [`services/fallback_provider.py`](services/fallback_provider.py) — Yahoo/AlphaVantage/Nikkei225JP/Minkabu フォールバックプロバイダ
- [`services/market_data_service.py`](services/market_data_service.py) — ヒートマップ・スクリーナー正規化
- [`services/news_service.py`](services/news_service.py) — ニュース並列収集・LLM要約
- [`services/news_formatter.py`](services/news_formatter.py) — ニュース整形・正規化
- [`services/realtime_engine.py`](services/realtime_engine.py) — TradingView WS / Yahoo JP スクレイパー / PTS / ユニファイドエンジン
- [`services/search_service.py`](services/search_service.py) — 検索戦略コーディネータ
- [`services/search/ddgs.py`](services/search/ddgs.py), [`services/search/langsearch.py`](services/search/langsearch.py), [`services/search/tavily.py`](services/search/tavily.py)
- [`services/stock_provider.py`](services/stock_provider.py) — yfinance プロバイダ
- [`services/stock_service.py`](services/stock_service.py) — 履歴フェッチ・キャッシュ

補助確認: [`ai_state.py`](ai_state.py), [`market_state.py`](market_state.py), [`mistral_compat.py`](mistral_compat.py), [`utils/stock_payload.py`](utils/stock_payload.py), [`utils/threading.py`](utils/threading.py), [`utils/caching.py`](utils/caching.py), [`utils/text_utils.py`](utils/text_utils.py), [`utils/http_utils.py`](utils/http_utils.py), [`app_state.py`](app_state.py), [`routes/api_analysis.py`](routes/api_analysis.py)

### 問題候補数

- 確定候補: **4件**（内訳: Critical 0 / High 0 / Medium 1 / Low 3）
- 要確認: **3件**

### 実行テスト（裏取り）

全てパス（exit code 0）。指示された対象領域の回帰テストは全てグリーンであり、**以下に挙げる問題は既存テストでカバーされていない未検証の経路**であることを示唆する。

```
[MNS TEST DIAGNOSTIC] Python 3.14.6 ... Windows-11
[MNS TEST DIAGNOSTIC] MNS_SKIP_BOOTSTRAP=1
[MNS TEST DIAGNOSTIC] KEYRING_BACKEND=keyring.backends.fail.Keyring
```

- `tests/test_ai_service_utils.py tests/test_ai_portfolio.py tests/test_ai_technical_lines.py tests/test_search_service.py tests/test_search_service_more.py tests/test_search_null_response.py tests/test_tavily.py tests/test_langsearch_utils.py tests/test_market_data_service.py tests/test_yfinance_overhaul.py tests/test_yfinance_perf_fixes.py tests/test_fallback_provider.py tests/test_realtime_engine.py tests/test_realtime_producer_fixes.py tests/test_news_formatter.py tests/test_tradingview_mapper.py tests/test_tradingview_fallback.py tests/test_networking_extra.py tests/test_stock_provider.py tests/test_llm_repair.py tests/test_mistral_api_improvements.py` → 全てパス（exit code 0）

---

## 確定問題候補

### [SVC-1][Medium] 問題候補: `generate_ai_technical_lines` の例外メッセージがクライアントへ生露出する経路（内部エラーメッセージ漏洩）

- 該当箇所: [`services/ai_service.py`](services/ai_service.py:927)-[`services/ai_service.py`](services/ai_service.py:929)（`except Exception` → `return {"error": f"AIテクニカル線生成エラー: {exc}"}`）、呼び出し元 [`routes/api_analysis.py`](routes/api_analysis.py:1637)-[`routes/api_analysis.py`](routes/api_analysis.py:1643)
- 影響経路:
  1. `generate_ai_technical_lines` 内で例外（SDKエラー・パース失敗等）が発生すると `logger.exception(...)` の後、`str(exc)` を直接 error 文字列に埋め込んで返す
  2. 呼び出し元 `routes/api_analysis.py` は `details={"reason": str(res["error"])}` として `error_response(..., status_code=500)` を組み立てる（[`routes/api_analysis.py`](routes/api_analysis.py:1638)-1643）
  3. `error_response`（[`utils/stock_payload.py`](utils/stock_payload.py:1030)）は `details` をレスポンス JSON に含めるため、`str(exc)` の内容（SDK 例外の本文・エンドポイント・内部スタック断片など）が HTTP レスポンスに露出する
- 問題・根本原因: 他の Mistral 経路（`call_mistral_chat` の `_short_text` ログ、`stream_mistral_chat` の固定メッセージ正規化）では例外文字列をクライアントへ露出させない方針（R5）が取られているが、`generate_ai_technical_lines` のみ例外文字列をそのままレスポンスへ返す非対称な実装になっている。`error_response`（[`utils/stock_payload.py`](utils/stock_payload.py:1030)）は `details` を `_sanitize_error_message`（[`utils/text_utils.py`](utils/text_utils.py:124)-140）で処理するが、これは API キー/トークン/パスワード等の既知パターンのみ REDACTED するもので、SDK 例外メッセージに含まれる内部文字列（エンドポイント断片・HTTP ステータス詳細・モデル名等）はそのままレスポンスに含まれる。
- 重要度評価: Medium。`_sanitize_error_message` により API キー自体は REDACTED されるため秘密情報そのものの漏洩は無いが、Mistral API のエラーメッセージ本文（デバッグ情報・リクエスト内容の断片）がクライアント（ブラウザ開発者ツールから閲覧可能）に露出する。エラーハンドリング不備（内部エラーメッセージの過剰露出）に該当。
- 客観的根拠: [`routes/api_analysis.py`](routes/api_analysis.py:1639)-1641 で `details={"reason": str(res["error"])}` を `error_response` に渡していることを確認。`_sanitize_error_message` の REDACTED パターン（[`utils/text_utils.py`](utils/text_utils.py:128)-136）は `api_key`/`token`/`password`/`bearer` 等に限定され、それ以外の文字列は透過することを確認。一方 `stream_mistral_chat` 経路では [`routes/api_analysis.py`](routes/api_analysis.py:859)-863 で `friendly_message` に正規化している。既存テスト `tests/test_ai_technical_lines.py` は正常系・モック応答のみで、例外経路のレスポンス内容は検証されていない。

---

### [SVC-2][Low] 問題候補: `CompositeFallbackProvider` に `close()` が無く、シャットダウン時に curl_cffi/HTTP コネクションプールが明示解放されない

- 該当箇所: [`services/fallback_provider.py`](services/fallback_provider.py:761)-[`services/fallback_provider.py`](services/fallback_provider.py:819)（`CompositeFallbackProvider`）、[`services/fallback_provider.py`](services/fallback_provider.py:163)-226 等（各プロバイダの `_get_client` が `curl_cffi.Session(impersonate=...)` を生成）、[`app_state.py`](app_state.py:330)-371（シャットダウン処理）
- 影響経路:
  1. `app_state.fallback_provider = CompositeFallbackProvider()`（[`app_state.py`](app_state.py:225)）で、`YahooWebScraperProvider` / `YahooJPScraperProvider` / `Nikkei225JPProvider` / `MinkabuProvider` がインスタンス化される
  2. 各プロバイダの `get_latest_quote` は `_get_client()` でスレッドローカルに `curl_cffi.Session`（`impersonate="chrome120"/"chrome110"`）を生成・保持する（[`services/fallback_provider.py`](services/fallback_provider.py:213)-226 等）。curl_cffi セッションは HTTP コネクションプールを保持する
  3. `CompositeFallbackProvider` には `close()` メソッドが存在せず、`app_state.shutdown()`（[`app_state.py`](app_state.py:330)-371）では `yf_session_manager.close_all()` / `realtime_market_engine.stop()` / Mistral クライアント close は実行されるが、`fallback_provider` 配下のセッションは解放されない
- 問題・根本原因: フォールバックプロバイダのセッションは `threading.local` に保持され、プロセス終了時まで GC されない。ローカル1プロセスではOSが回収するため実害は小さいが、テストやプロセス再初期化で `CompositeFallbackProvider` を再生成すると旧インスタンスのコネクションプールがメモリに残る。シャットダウン時のリソース解放の対称性が欠如している。
- 重要度評価: Low。リソース解放漏れに該当するが、実害（メモリ・FD枯渇）はプロセス長寿命・反復再生成でなければ顕在化しない。シャットダウン管理の不完全性として記録。
- 客観的根拠: `CompositeFallbackProvider` クラスに `close` が定義されていないことを確認（`services/fallback_provider.py` 内の `def close` は0件）。`app_state.shutdown()` の [`app_state.py`](app_state.py:330)-371 で `fallback_provider` への close 呼び出しが存在しないことを確認。既存テスト `tests/test_fallback_provider.py` / `tests/test_nikkei225jp_scraper.py` はプロバイダ生成・応答のみで、シャットダウン時のセッション解放は検証されていない。

---

### [SVC-3][Low] 問題候補: `news_service` のモジュールレベル `_NEWS_FANOUT_POOL` がシャットダウン時に明示 shutdown されない

- 該当箇所: [`services/news_service.py`](services/news_service.py:32)-35（`_NEWS_FANOUT_POOL = DaemonThreadPoolExecutor(max_workers=min(32, max(12, cpu*4)))`）、[`app_state.py`](app_state.py:330)-371（シャットダウン処理）
- 影響経路:
  1. `/api/news` の並列ファンアウトに使う `_NEWS_FANOUT_POOL` はモジュールレベルの `DaemonThreadPoolExecutor` で、`max_workers` が最大32
  2. `app_state.shutdown()` ではこのプールの `shutdown()` が呼ばれない（検索結果: `_NEWS_FANOUT_POOL.shutdown` は0件）
  3. ただし `DaemonThreadPoolExecutor` はデーモンスレッドを使用するため（[`utils/threading.py`](utils/threading.py:19)-107）、プロセス終了をブロックしない
- 問題・根本原因: デーモンスレッドなのでシャットダウン時にプロセス終了はブロックされないが、稼働中の `/api/news` 収集タスクがシャットダウン後も完了まで実行され、シャットダウン中の外部HTTP呼び出し（DDGS/LangSearch/Tavily）が継続する。長時間の収集タスク（タイムアウト45秒まで）がシャットダウン処理と並行して走る。実害は限定的だが、シャットダウン時のリソース解放・タスクキャンセルのフックが存在しない。
- 重要度評価: Low。デーモンスレッドのため起動不能・ハングは無い。シャットダウン時の外部呼び出し継続（ネットワークトラフィック）という運用上の軽微な実害。
- 客観的根拠: `_NEWS_FANOUT_POOL.shutdown` への呼び出しがアプリケーションコードに存在しないことを確認。`DaemonThreadPoolExecutor` はデーモンスレッド化（[`utils/threading.py`](utils/threading.py:96)-101）を確認。既存テスト `tests/test_api_integration.py` 等は /api/news の応答検証のみで、シャットダウン中のファンアウトタスク挙動は検証されていない。

---

### [SVC-4][Low] 問題候補: `fetch_history_sync_impl` の成功ペイロードがシャローコピーで `yfinance_short_cache` に格納され、`history` リストが呼び出し元と共有される

- 該当箇所: [`services/stock_service.py`](services/stock_service.py:284)-289（`app_state.yfinance_short_cache[corrected_cache_key] = dict(result)`）、[`services/stock_service.py`](services/stock_service.py:276)-282（`result["history"] = data_list`）
- 影響経路:
  1. `fetch_history_sync_impl` は `result = {"symbol": ..., "history": data_list, ...}` を構築し、`dict(result)` でシャローコピーを `yfinance_short_cache` に格納する
  2. `dict(result)` は最上位キーのみをコピーするため、`result["history"]`（`data_list` リスト）はキャッシュと返り値で共有される
  3. 呼び出し元（`routes` のハンドラ等）が返り値の `history` リストを破壊的変更（要素追加・削除・並べ替え）した場合、キャッシュにも反映され、後続の同一 `(symbol, period, interval)` リクエストが汚染されたデータを返す
- 問題・根本原因: キャッシュ格納がシャローコピーで、可変の `history` リストを共有している。読み取り専用の利用が前提のため現状は実害が無いが、将来の呼び出し元変更でキャッシュ汚染（データ不整合）のリスクを持つ。
- 重要度評価: Low。現状の呼び出し元は読み取り専用のため実害なし。保守上の実害（キャッシュ共有による潜在的なデータ不整合リスク）として記録。
- 客観的根拠: [`services/stock_service.py`](services/stock_service.py:287)-289 の `dict(result)` を確認。`data_list`（[`services/stock_service.py`](services/stock_service.py:254)-274）が呼び出し元へ返る `result["history"]` と同じオブジェクトであることを確認。既存テスト `tests/test_stock_interval.py` 等は返り値の内容検証のみで、キャッシュ共有による破壊的変更は検証されていない。

---

## 要確認問題候補（確証が取れないもの）

### [SVC-C1][要確認] `call_mistral_chat` / `stream_mistral_chat` の `except` 節が httpx 例外（`httpx.ReadTimeout` / `httpx.ConnectError`）を捕捉しきれるか

- 該当箇所: [`services/ai_service.py`](services/ai_service.py:700)、[`services/ai_service.py`](services/ai_service.py:1069)（`except (SDKError, RequestsTimeout, CurlRequestsTimeout, ConnectionError, OSError)`）
- 内容: 本番の mistralai SDK v2 は内部で httpx を使用し、トランスポートエラーを `SDKError` にラップする。ただし、カスタム `httpx.Client`（`httpx.MockTransport` 等）を注入する場合や、SDK バージョンによっては `httpx.ReadTimeout` / `httpx.ConnectError`（`httpx.TransportError` 継承）がそのまま伝播する可能性がある。`RequestsTimeout` は `requests.exceptions.Timeout`（`constants.py:11`）であり、httpx の `ReadTimeout` は別系統。`ConnectionError`（組み込み）は httpx の `ConnectError` とは別。捕捉漏れが起きた場合、`call_mistral_chat` から例外が伝播し、ルート層の catch-all（`error_handlers.py`）で 500 になる。テスト `tests/test_mistral_api_improvements.py` は `httpx.MockTransport` を使うが、例外伝播の網羅検証は無い。実害は SDK 標準利用では低いが、環境依存のため要確認。
- 根拠: `tests/test_mistral_api_improvements.py:458-462` で `httpx.MockTransport` を注入するクライアントを使うテストがあるが、エラー送出時の `call_mistral_chat` 経由の捕捉は検証されていない。

### [SVC-C2][要確認] SSE クライアント切断（`GeneratorExit`）時の `stream_mistral_chat` 内部ストリーム close

- 該当箇所: [`services/ai_service.py`](services/ai_service.py:1038)（`for chunk in client.chat.stream(**kwargs)`）、[`routes/api_analysis.py`](routes/api_analysis.py:875)-880（`finally`）
- 内容: クライアント切断で `generate()` に `GeneratorExit` が送られると、`stream_mistral_chat` ジェネレータは `with app_state.ai.mistral_stream_semaphore:` を `__exit__` で抜けてセマフォは解放される。しかし `client.chat.stream` の内部イテレータ（httpx ストリーミング接続）が明示的に close されないため、切断時に進行中の HTTP ストリーム接続が残る可能性がある。`GeneratorExit` は `BaseException` であり、`except (SDKError, ...)` では捕捉されない。Python の GC により最終的には回収されるが、高頻度の接続切断時に一時的な接続リークが発生し得る。実害は軽微だが要確認。
- 根拠: `stream_mistral_chat` に `try/finally` で SDK ストリームを閉じる処理が無く、`with` がセマフォ解放のみを保証している。

### [SVC-C3][要確認] `_handle_producer_update` が全更新時に `_get_yfinance_previous_close` を呼び、初回キャッシュミス時のみ `sse_data_lock` を取得する

- 該当箇所: [`services/realtime_engine.py`](services/realtime_engine.py:2334)（`prev_close = _get_yfinance_previous_close(symbol)`）、[`utils/stock_payload.py`](utils/stock_payload.py:1079)-1105（`sse_data_lock` 内で stocks キャッシュを全走査）
- 内容: 更新のたびに `_get_yfinance_previous_close` が呼ばれるが、`update_previous_close_cache` により前回 close がキャッシュ済みなら `get_previous_close_cached`（ロックフリー）で即時返る。キャッシュミスの初回のみ `sse_data_lock` で stocks キャッシュを全シンボル走査する。`sse_data_lock` は広域 RLock であり、SSE ストリームと競合し得るが、頻度は1シンボルにつき最初の数回のみ。実害は限定的と判断するが、ウォッチリストが大きく初期同期が同時に行われる場面でのロック競合を要確認。
- 根拠: [`utils/stock_payload.py`](utils/stock_payload.py:1064)-1073 で previous-close キャッシュがロックフリーで優先参照されることを確認。`market_state.update_previous_close_cache`（[`market_state.py`](market_state.py:329)-342）がキャッシュを更新することを確認。

---

## 指摘対象外（健全と判断した項目）

- **Mistral レート制限・サーキットブレーカー**: [`services/ai_service.py`](services/ai_service.py:454)-476 の `_acquire_mistral_call_slot`（ジッタ付き）、[`services/ai_service.py`](services/ai_service.py:600)-608 のセマフォ+サーキット確認、429/容量エラーのバックオフ（[`services/ai_service.py`](services/ai_service.py:718)-721）。健全。
- **Mistral レスポンスキャッシュのスレッドセーフティ**: [`services/ai_service.py`](services/ai_service.py:577)-581 の `mistral_response_lock`、空レスポンス非キャッシュ（`_response_has_content`）。健全。
- **TradingView WS のライフサイクル管理**: [`services/realtime_engine.py`](services/realtime_engine.py:760)-968 の `_worker_epoch` 世代管理・`stop()` の `join(timeout=2.0)`・`_on_open` の stale チェック。健全。
- **Yahoo JP スクレイパーの世代管理**: [`services/realtime_engine.py`](services/realtime_engine.py:1478)-1603 の `_epoch` 世代管理、`stop()` の executor shutdown。健全。
- **PTS ワーカーの世代管理**: [`services/realtime_engine.py`](services/realtime_engine.py:2738)-2831 の `_pts_epoch` 世代管理と `_interruptible_sleep`。健全。
- **SSE クライアントのカーソル管理**: [`services/realtime_engine.py`](services/realtime_engine.py:2397)-2446 の `register_client`/`client_context` による確実な解放、`_purge_stale_clients`（TTL 120秒）。健全。
- **検索プロバイダのタイムアウト・リトライ**: [`services/search/langsearch.py`](services/search/langsearch.py:120)-166 の tenacity リトライ（429/503/Timeout）、[`services/search/tavily.py`](services/search/tavily.py:37)-46 の tenacity リトライ。健全。
- **DDGS クエリの長さ制限・タイムアウト**: [`services/search/ddgs.py`](services/search/ddgs.py:40)-63 の `_sanitize_ddgs_query`、[`services/search/ddgs.py`](services/search/ddgs.py:35)-37 の `_get_ddgs_timeout`。健全。
- **CDATA によるプロンプトインジェクション対策**: [`services/news_service.py`](services/news_service.py:199)-241、[`utils/text_utils.py`](utils/text_utils.py:21)-30 の `wrap_cdata`/`sanitize_cdata`（`]]>` エスケープ）。健全。
- **ストリーミングエラーの固定メッセージ正規化**: [`routes/api_analysis.py`](routes/api_analysis.py:859)-863。健全。
- **AIポートフォリオの暗号化 at-rest**: [`services/ai_portfolio_service.py`](services/ai_portfolio_service.py:158)-221 の Fernet 暗号化・fail-closed・原子書き込み。健全。
- **AIポートフォリオの同時生成ロック**: [`services/ai_portfolio_service.py`](services/ai_portfolio_service.py:411)-433 の `_AI_GEN_INFLIGHT` イベントベース。健全。
- **APIキー漏洩防止**: [`services/ai_service.py`](services/ai_service.py:617)-623 の `_token_fingerprint`（SHA256 先頭16桁）によるログ出力、[`utils/text_utils.py`](utils/text_utils.py:45)-54。健全。
- **yfinance セッションプール**: [`services/stock_provider.py`](services/stock_provider.py:66)-67 の TTL/最大件数付き Ticker キャッシュ、レート制限検出（[`services/stock_provider.py`](services/stock_provider.py:70)-141）。健全。
- **履歴フェッチのセマフォ・サーキット**: [`services/stock_service.py`](services/stock_service.py:63)-128 の `yfinance_history_semaphore`（タイムアウト付き）+ サーキットブレーカー。健全。

---

## 補足: テスト裏取りの限界

- 既存テストは `KEYRING_BACKEND=keyring.backends.fail.Keyring` / `MNS_SKIP_BOOTSTRAP=1` で実行され、実キーリング/DPAPI 分岐や bootstrap の fail-closed パスは実網羅されていない。
- `httpx.MockTransport` を注入した Mistral クライアントの例外伝播（SVC-C1）、SSE クライアント切断時のストリーム close（SVC-C2）、`yfinance_short_cache` のシャローコピー共有（SVC-4）は、いずれも単体テストでカバーされていない未検証の経路。
- `fallback_provider` の `close()` 欠如（SVC-2）と `_NEWS_FANOUT_POOL` の shutdown 欠如（SVC-3）は、シャットダウン時のリソース解放を検証するテストが存在しない。

## 保存場所

- 本ファイル: `goal-review-services.md`（作業ディレクトリ直下）
