# 包括コードレビュー — ディスカバリ・パス（変更なし）

- 対象: `mistral_nex_stocks_complete_fixed_v3`（branch `master` / working tree クリーン）
- 実施日: 2026-10-21
- このパスの制約: **コード変更・コミット・push は行わない**。調査・実測・課題棚卸しのみ。
- 本ドキュメントは次パス以降の「唯一の課題リスト（single source of truth）」として更新していく。

---

## 0. 結論サマリ

このプロジェクトは既に**異例に高い品質基盤**を持つ。テスト 3006 件中 3003 件が通り（3 skip）、ruff / flake8 / mypy / pyrefly / pylint / bandit / tsc / eslint / prettier / 生成物検証がすべてグリーン、CI には coverage 75% ゲート・startup smoke・SRI 検証・pip-audit・win32 向け pyrefly まで入っている。

したがって本レビューの価値は「 lint が拾えない構造的負債 」にある。最大の根本原因は次の 3 つに集約される。

1. **型・検証の単一の源が存在しない** — `schemas/`（Pydantic）が本番コードから一度も import されておらず、同じ制約が 3 箇所で手書きされている（A-01〜A-03）。
2. **循環 import を `sys.modules.get()` による実行時ディスパッチで回避している** — 24 箇所。mypy は `Any` として素通りし、import 順で挙動が変わり、テスト 257 箇所がこの内部構造に依存している（A-04）。
3. **ドメイン不変条件が巨大モジュールに埋没している** — `config_store.py` 1248 行、`stock_provider.py` 1814 行、`sync_worker.py` 1377 行、`api_system.py` 1411 行、`ui.js` 3793 行。その結果、サーキットブレーカのキー空間不一致（D-01）や時価総額の次元エラー（D-03）のような「静かに間違う」バグが残っている。

修正の順序は **共通基盤・型・API 境界 → サービス → ルート → フロント** の一方向でなければならない。個別バグを先に直すと、基盤変更で再度書き換えることになる。

### 修正サイクル 1（2026-10-21 実施）

サービス / リアルタイム層の verified 不具合を 1 ユニットとして修正した。

| 課題                 | 状態                 | 変更内容                                                                                                                                                                                                         | 回帰テスト                                                                                                     |
| -------------------- | -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| D-01 circuit key     | **修正済み**         | `utils/normalization.history_circuit_key()` を新設し、route / async task / service / provider の全 6 呼び出しを統一。provider は symbol から market を導出（`.T`/`^`/4 桁 JP コード）。                          | `test_history_circuit_key_is_market_qualified`, `test_provider_reads_the_same_circuit_key_the_service_reports` |
| D-03 market cap      | **修正済み**         | `price * volume` の代用を削除。`shares * price` のみ許可し、cap 不明の行は除外。`shares_outstanding`（snake_case）も受容。                                                                                       | `test_heatmap_excludes_symbols_without_a_market_cap`, `test_heatmap_accepts_snake_case_shares_outstanding`     |
| K-02 pause/fallback  | **修正済み**         | 一時停止を「銘柄の除外」ではなく「一次ソースのスキップ」に変更。`_fetch_regular_with_fallback(use_primary=...)` と `_fetch_pts_with_fallback(use_primary=...)` に分離し、worker は停止中もフォールバックを実行。 | `test_paused_primary_still_uses_fallback`, `test_engine_pts_fallback_runs_when_primary_paused`                 |
| K-03 stop/restart    | **修正済み**         | PTS ループの `time.sleep` を `_interruptible_sleep` に置換。                                                                                                                                                     | `test_pts_worker_stops_promptly_during_inter_symbol_sleep`                                                     |
| K-04 dead except     | **修正済み**         | `except (RuntimeError, Exception)` → `except Exception`（2 箇所）。                                                                                                                                              | 静的解析で確認（重複要素の除去、振る舞い不変）                                                                 |
| K-05 double dispatch | **誤検知・取り下げ** | 実コードは dash 変種を `"." in bare_sym` のときだけ送出しており、重複は存在しなかった。挙動ロックのテストのみ残す。                                                                                              | `test_tv_quote_without_dot_dispatches_once_per_distinct_symbol`                                                |

未着手（次ユニット以降）: K-01（Low）、K-06〜K-11、U1 の残り（A-01 の他エンドポイント、A-06）、U2・U3、U6〜U10、U11 の E-02、U12〜U14。K-07 は拡張のトークン取得ライフサイクルとネイティブ予算の再設計が必要なため、単独ユニットとして分離した（予算値は変更しない）。

### 修正サイクル 2（2026-10-21 実施） — U1: 要求ボディ契約の単一化（ポートフォリオ + config）

対象は「実行中のルートが実際に受け取る JSON ボディ」のうち、重複が明示されているポートフォリオと、同名別型が併存していた config に限定した。

| 課題                           | 状態                                  | 変更内容                                                                                                                                                                                                                                                                                                                              | 回帰テスト                                                                                                                                                                                                    |
| ------------------------------ | ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A-02 同名別型                  | **修正済み**                          | `schemas/config.py` が `RuntimeConfigSchema`（永続設定）と `TemplateConfigSchema`（テンプレート文脈）を所有。旧 `AppConfigSchema` の別名は削除し、`routes/pages.py` とテストは `TemplateConfigSchema` を直接参照。                                                                                                                    | `test_schemas.py::test_runtime_config_schema_defaults`、`test_validators.py::TemplateConfigSchemaTestCase`、`test_review_autonomous_head_fixes_2026.py::test_r7_app_config_schema_preserves_ephemeral_fields` |
| A-03 ポートフォリオ検証 3 実装 | **修正済み（/api/stocks/portfolio）** | `schemas/stocks.py:PortfolioUpdateRequest` が唯一の契約。`route_helpers._parse_portfolio_request()` は schema → 既存 `error_response` への写像だけを行う。`utils/validators.PortfolioInputSchema`、`MnsValidationError`、`validate_portfolio_input` シム、ルート内の `_parse_stock_request` + `parse_non_negative_float` 連鎖を削除。 | `tests/test_schema_contract_2026_10_21.py`（21 ケース、Flask クライアント経由）+ 旧実装との差分照合 603 ペイロード                                                                                            |

**重複間で意図が違った点の解決（本番挙動が勝者）**:

- `avg_fx_rate` の非 US 市場: schema は 400 で拒否していたが、本番は 200 で受け取り保存時に除去する。**本番を採用**し、schema の市場制約を削除。`tests/test_schemas.py` の死コード前提テストを「受理される」に更新し、保存時除去は既存の `test_portfolio_avg_fx_rate_idx.py` が固定。
- 数値の受理規則: 数値文字列を許可し、bool / NaN / Inf を拒否する本番パーサの挙動を採用（`allow_inf_nan=False` + `mode="before"` の bool 拒否）。
- エラー本文: `MISSING_REQUIRED_FIELD`（shares/avg_price/symbol）、`INVALID_INPUT`（`shares must be a number` 等の英語メッセージ、総額と fx=0 の日本語メッセージ）、`INVALID_SYMBOL`、`INVALID_MARKET`、`MALFORMED_INPUT` のコード・文言・HTTP ステータスを変更なし。schema の `le` は float で報告されるため、旧ルートと同じ int 表記に整形している。

**このパスで確立した構造**:

```
schemas/stocks.py           要求ボディの契約（PortfolioUpdateRequest が唯一の規則所有者）
schemas/config.py           RuntimeConfigSchema（永続設定） / TemplateConfigSchema（テンプレート文脈）
route_helpers.py            HTTP アダプタ: body → schema、ValidationError → 既存エラー封筒
utils/validators.py         ポートフォリオ検証は持たない（他スキーマ・AI 解析・JSON 修復）
routes/stocks/portfolio.py  認可・ウォッチリスト・永続化・SSE のみ（フィールド検証なし）
```

**検証（サイクル 2+3 の最終実測）**:

| コマンド                                                          | 結果                                            | 終了コード |
| ----------------------------------------------------------------- | ----------------------------------------------- | ---------- |
| `.venv/Scripts/python.exe -m pytest tests/ -q -n 0`               | 3006 tests / 3003 passed / 3 skipped / 0 failed | 0          |
| `ruff check . --line-length 100`                                  | All checks passed                               | 0          |
| `flake8 . --count --select=E9,F63,F7,F82`                         | 0                                               | 0          |
| `mypy .`                                                          | Success: no issues found in 90 source files     | 0          |
| `pyrefly check`                                                   | 0 errors                                        | 0          |
| `pylint --errors-only`（変更ファイル + schemas）                  | 出力なし                                        | 0          |
| `npm run typecheck` / `npm run lint` / `npm run verify-generated` | エラーなし / 生成物一致                         | 0          |
| `npx prettier --check`                                            | All matched files use Prettier code style       | 0          |

**意図的に据え置いたスライス**:

- AI ポートフォリオ（generate/rebalance/save/delete/copy-to-my）、chat、analyze-v2、ai-technical-lines、analyze-chart-image、credentials、stocks add/delete/add_ext は手書きパーサのまま。各エンドポイントは独自のエラーコード・文言（例: delete の 256 文字超過は `MALFORMED_INPUT`、copy-to-my の項目別メッセージ）を持ち、契約を移すには個別の写像監査が必要なため、一度に移すと「byte-identical」を保証できない。
- `ScreenerFilterSchema` / `HeatmapFilterSchema`（`utils/validators.py`）と `ScreenerQueryRequest` / `HeatmapQueryRequest`（`schemas/stocks.py`）の重複は残存。両者はクエリパラメータ用で、ルートは独自の日本語エラーを返すため、A-03 とは別スライスとして扱う。
- A-06 の成功封筒統一、A-04 の `sys.modules` ディスパッチ、拡張のトークンライフサイクル（K-07）は本パスのスコープ外（指示どおり触れていない）。

### 修正サイクル 4（2026-10-21 実施） — U16: K-07 拡張のトークン取得を 1 ライフサイクル 1 回に

ネイティブホストの秘密返却は `get_shutdown_token` と `get_extension_api_token` で**合計** 3 回 / 30 秒（`native_host/native_host.py:243-260`）。`getContext` が 2 回、`startBackend` の `queryTokens` が最大 5 回 × 2 取得していたため、ポップアップを開いて起動するだけで予算を使い切り、`/api/stocks/add_ext` が 401/403 になっていた（K-07）。

| 変更                       | 内容                                                                                                                                                                                                                                       |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `getContext`               | トークンを読まない（診断情報のみ）。shutdown / extension の両取得を削除                                                                                                                                                                    |
| `startBackend`             | `queryTokens` のポーリングを削除。起動応答を遅らせない                                                                                                                                                                                     |
| `getOrFetchExtensionToken` | メモリ + `storage.session` の単一キャッシュと in-flight 共有を維持。401/403 時だけ `forceRefresh` で両コピーを破棄し、**セッションコピーは読み直さず**ネイティブホストへ 1 回だけ再取得（`storage.session.remove` が無いブラウザでも回復） |
| 起動時の水和               | 取得済みトークンを古い `storage.session.get` コールバックが上書きしない                                                                                                                                                                    |
| shutdown トークン          | `stopBackend` が必要時に 1 回取得（従来の遅延取得のまま）                                                                                                                                                                                  |

変更していないもの: `NATIVE_HOST_TOKEN_ACTION_MAX=3` / `NATIVE_HOST_TOKEN_ACTION_WINDOW=30`、`_token_action_allowed` の fail-closed、`secrets.compare_digest` による照合、`sendNativeMessage` の再試行（token action のレート制限は再試行しない）。

**回帰テスト**（`tests/test_extension_token_lifecycle.py`。実 `chrome_extension/background.js` を Node VM で実行し、`sendNativeMessage` を `native_host.py` から読み取った実予算のモックに接続。`/api/stocks/add_ext` は現在の Bearer 以外を 403 にする）:

| テスト                                                                  | 固定する挙動                                                                                             |
| ----------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `test_one_round_of_extension_requests_stays_within_native_token_budget` | getContext → startBackend → add → stop が予算内。add_ext の 401/403 0 回、extension 1 回 + shutdown 1 回 |
| `test_context_and_start_backend_do_not_spend_the_token_budget`          | 診断・起動はトークンを 1 回も読まない                                                                    |
| `test_expired_cached_token_is_refetched_exactly_once`                   | 期限切れキャッシュで 403 1 回 → 再取得 1 回 → 再試行成功、保存値も更新                                   |
| `test_refresh_ignores_an_unremovable_stale_session_copy`                | `storage.session.remove` 不在でも再取得して回復                                                          |
| `test_token_that_stays_rejected_stops_after_one_refresh_and_one_retry`  | 再取得しても拒否される場合は POST 2 回で停止（再試行ストームなし）                                       |
| `test_concurrent_adds_share_a_single_token_acquisition`                 | 同時 add は同一 in-flight 取得を共有（ネイティブ取得 1 回）                                              |

**検証（サイクル 4 の最終実測）**:

| コマンド                                                              | 結果                                                              | 終了コード |
| --------------------------------------------------------------------- | ----------------------------------------------------------------- | ---------- |
| `.venv/Scripts/python.exe -m pytest tests/ -q -n 0`                   | 2965 collected / 2962 passed / 3 skipped / 47 subtests / 0 failed | 0          |
| `ruff check . --line-length 100`                                      | All checks passed                                                 | 0          |
| `flake8 . --count --select=E9,F63,F7,F82`                             | 0                                                                 | 0          |
| `mypy .`                                                              | Success: no issues found in 90 source files                       | 0          |
| `pyrefly check`                                                       | 0 errors（20 suppressed）                                         | 0          |
| `pylint --errors-only tests/test_extension_token_lifecycle.py`        | 出力なし                                                          | 0          |
| `npm run typecheck` / `npm run lint` / `npm run verify-generated`     | エラーなし / 生成物一致                                           | 0          |
| `npx prettier --check "static/js/**/*.js" "chrome_extension/**/*.js"` | All matched files use Prettier code style                         | 0          |

**残り**: K-08（`addStockViaExtension` のタイムアウト）は K-07 と同じ U16 だが、今回の指示は K-07 のみのため据え置き。— **サイクル 5 で解消**。

### 修正サイクル 5（2026-10-21 実施） — U16: K-08 と同型の「無期限 fetch」を一括で閉じる

K-08 の `addStockViaExtension` だけでなく、拡張・フロントエンドの生 `fetch` を全件確認した。タイムアウトも呼び出し元の中断も持たない呼び出しが残っており、バックエンドが接続を受けたまま応答しないと次の状態で固まる。

| 箇所                                         | 固まる状態                                                                                          |
| -------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| `background.js` add_ext                      | ポップアップの追加ボタン / コンテキストメニューが処理中+"NG" にならない                             |
| `background.js` `/api/indices`               | `isUpdatingBadge` が立ちっぱなしになり、以後のバッジ更新がすべてスキップされる                      |
| `background.js` `/api/shutdown`              | 停止ボタンが処理中のまま                                                                            |
| `popup.js` `/api/stocks`                     | 5 秒ポーリングが 1 回で止まり、古いデータが「新鮮」に見える                                         |
| `utils.js` `csrfFetch` / `refreshCsrfToken`  | 全フロントエンドの API 呼び出しと CSRF 再取得が無期限に待つ（`_csrfRefreshPromise` が永久に未解決） |
| `setup.js` / `state.js` / `ui.js` の生 fetch | 認証情報取得・チャットのフォールバックが無期限に待つ                                                |

**修正（根の共有化）**:

- `static/js/utils.js` に `fetchWithTimeout()` を新設。`csrfFetch` の全呼び出し（30 箇所）と `refreshCsrfToken`、`setup.js` / `state.js` / `ui.js` の生 fetch がこれを通る。
- 既定は **120 秒**で、`gunicorn.conf.py:timeout = 120` と一致。CSRF 再取得と credential 状態取得は **10 秒**（即答が前提の制御系）。
- 拡張は `background.js` / `popup.js` に同じヘルパを置き、add/indices/poll は **10 秒**、health は従来の **3 秒**、shutdown は **5 秒**。
- タイマーはレスポンス受信時に解除するため、チャットの `text/event-stream` や長いボディを途中で切らない。呼び出し元の `AbortSignal` は従来どおり伝播する。
- タイムアウトは `TimeoutError`、利用者による中断は `AbortError` として区別。`apiFetch` はタイムアウトを既存の TIMEOUT トーストで報告し、拡張は `{ok:false,error:...}` / stale 表示 / NG バッジの既存経路に乗せる（未処理の rejection を出さない）。

**回帰テスト**（`tests/test_fetch_timeouts_2026_10_21.py`。`fetch` を「AbortSignal が発火するまで解決しない」モックにし、旧コードではハーネスの watchdog が失敗する）:

| テスト                                                                 | 固定する挙動                                                                                                               |
| ---------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `test_add_stock_fetch_times_out_and_reports_the_failure`               | 実 `background.js` の `addDetectedStock` が 1 回の abort 後に `{ok:false,error:"リクエストがタイムアウトしました"}` を返す |
| `test_popup_stock_poll_times_out_and_marks_the_list_stale`             | 実 `popup.js` の `fetchAndRenderStocks` が abort 後に stale 表示と「更新失敗」を出す                                       |
| `test_csrf_fetch_has_a_timeout_and_keeps_caller_cancellation_distinct` | `csrfFetch` のタイムアウトは `TimeoutError`、呼び出し元 abort は `AbortError` のまま                                       |

旧コード（HEAD の 3 ファイル）に対して同じハーネスを実行し、3 件とも watchdog で失敗することを確認済み。

**検証（サイクル 5 の最終実測）**:

| コマンド                                                              | 結果                                             | 終了コード |
| --------------------------------------------------------------------- | ------------------------------------------------ | ---------- |
| `.venv/Scripts/python.exe -m pytest tests/ -q -n 0`                   | 2965 passed / 3 skipped / 47 subtests / 0 failed | 0          |
| `ruff check . --line-length 100`                                      | All checks passed                                | 0          |
| `flake8 . --count --select=E9,F63,F7,F82`                             | 0                                                | 0          |
| `mypy .`                                                              | Success: no issues found in 90 source files      | 0          |
| `pyrefly check`                                                       | 0 errors（20 suppressed）                        | 0          |
| `pylint --errors-only`（新規テスト）                                  | 出力なし                                         | 0          |
| `npm run typecheck` / `npm run lint` / `npm run verify-generated`     | エラーなし / 生成物一致                          | 0          |
| `npx prettier --check "static/js/**/*.js" "chrome_extension/**/*.js"` | All matched files use Prettier code style        | 0          |
| `node --check`（変更 JS 6 ファイル）/ `git diff --check`              | エラーなし                                       | 0          |

**残り**: `background.js` / `popup.js` のヘルパは 2 つの独立した拡張コンテキスト（service worker と side panel）で重複している。共有モジュール化は MV3 の `type: module` 移行を伴うため、このパスでは行っていない。

---

## 1. 実測ベースライン（レビュー初回時点の記録）

実行環境: Windows / `.venv` の Python 3.14.7 / Node（`node_modules` 導入済み）。

| #   | コマンド                                                              | 結果                                                    | 終了コード |
| --- | --------------------------------------------------------------------- | ------------------------------------------------------- | ---------- |
| 1   | `.venv/Scripts/python.exe -m pytest tests/`                           | **2946 passed, 3 skipped, 47 subtests passed** (14.84s) | 0          |
| 2   | `MNS_SKIP_BOOTSTRAP=0 python tests/startup_smoke_runner.py`           | 起動 smoke 成功（bootstrap 有効・health 到達）          | 0          |
| 3   | `python -m ruff check . --line-length=100`                            | All checks passed                                       | 0          |
| 4   | `python -m flake8 . --count --select=E9,F63,F7,F82`                   | 0                                                       | 0          |
| 5   | `python -m mypy`                                                      | Success: no issues found in 90 source files             | 0          |
| 6   | `python -m pyrefly check`                                             | **0 errors (20 suppressed, 21 warnings not shown)**     | 0          |
| 7   | `python -m pylint --errors-only …`                                    | 出力なし（エラー 0）                                    | 0          |
| 8   | `python -m bandit -r . --severity-level medium`                       | Issue 0 件                                              | 0          |
| 9   | `npm run typecheck` (`tsc --noEmit`)                                  | エラーなし                                              | 0          |
| 10  | `npm run lint` (`eslint static/js chrome_extension`)                  | エラーなし                                              | 0          |
| 11  | `npx prettier --check "static/js/**/*.js" "chrome_extension/**/*.js"` | All matched files use Prettier code style               | 0          |
| 12  | `npm run verify-generated`                                            | `static/js/api_client.js` matches the TypeScript output | 0          |

**修正サイクル 1 後の再実行（2026-10-21、この作業ツリー）**

| コマンド                                  | 結果                                                    | 終了コード |
| ----------------------------------------- | ------------------------------------------------------- | ---------- |
| `pytest tests/`                           | **2955 passed, 3 skipped, 47 subtests passed** (14.56s) | 0          |
| `ruff check . --line-length=100`          | All checks passed                                       | 0          |
| `flake8 . --count --select=E9,F63,F7,F82` | 0                                                       | 0          |
| `mypy`                                    | Success: no issues found in 90 source files             | 0          |
| `pyrefly check`                           | 0 errors (20 suppressed, 22 warnings not shown)         | 0          |
| `pylint --errors-only …`                  | 出力なし                                                | 0          |
| `npm run typecheck`                       | エラーなし                                              | 0          |
| `npm run lint`                            | エラーなし                                              | 0          |
| `npx prettier --check`                    | All matched files use Prettier code style               | 0          |
| `npm run verify-generated`                | 生成物一致                                              | 0          |

追加されたテストは `tests/test_review_fixes_2026_10_21.py` の 9 件（うち K-05 は既存挙動のロック）。既存テスト 1 件（`test_yahoo_old_worker_generation_cannot_publish_after_restart`）は内部メソッドの新キーワード引数に合わせてテストダブルを `**kwargs` 化した（アサーションは不変）。

補足:

- `pytest.ini` は `-n auto --dist=loadfile` を既定にしており、ファイル単位でワーカーに分散する。グローバル状態を持つテスト同士の衝突はこの設定で緩和されているが、**同一ファイル内の順序依存は残る**。
- `npm run build` は `typecheck → compile → verify-generated → lint → prettier --check` の合成。個別に全てグリーンであることは確認済みだが、`compile` が追跡ファイルを書き換えるため合成実行は作業ツリーを dirty にしうる（H-05）。
- `pyrefly` の **20 suppressed** と 21 の非表示 warning は CI を通過するが、内訳は追跡されていない（J-06）。

---

## 2. 網羅性の実態（どこまで読んだか）

追跡ファイル 416、うち Python 本番 91 ファイル / テスト 228 ファイル / フロント JS・TS・CSS 33 ファイル。

| 領域                                                                                                                                                                                                                                                                                         |   行数 | 本パスの深さ                                                                                | 備考                                                                            |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -----: | ------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| ルート直下のコア（`app.py`, `app_state.py`, `market_state.py`, `config_store.py`, `crypto_utils.py`, `credential_manager.py`, `session_manager.py`, `shutdown_manager.py`, `messaging.py`, `ai_state.py`, `execution_state.py`, `error_*`, `security_config.py`, `constants.py`, `wsgi.py`） | 11,174 | **全読**                                                                                    | 主要な不変条件を確認                                                            |
| `routes/`（7 ファイル + `stocks/` 6 ファイル）                                                                                                                                                                                                                                               |  7,406 | **全読**                                                                                    | `api_analysis.py` は境界と入力検証を精読                                        |
| `services/realtime/engine.py` / `tv_client.py` / `utils.py`                                                                                                                                                                                                                                  |  1,574 | **全行読**                                                                                  | スレッド・ロック・WS 再接続                                                     |
| `services/realtime/scrapers.py`                                                                                                                                                                                                                                                              |  1,232 | **全行読**（`51-210`, `605-1232` を精読、`211-604` は SBI/Nikkei/Minkabu の取得関数を精読） |                                                                                 |
| `services/ai_service.py`                                                                                                                                                                                                                                                                     |  2,258 | 実装本体を精読（`418-592`, `796-1250`, `1937-2258`）                                        | 残りは委譲・定数・ヘルパ                                                        |
| `services/openai_service.py`                                                                                                                                                                                                                                                                 |  1,214 | 呼び出し・ストリーム終端（`304-460`, `677-882`）を精読                                      | 残りはツールループ／画像／埋め込み                                              |
| `services/fallback_provider.py`                                                                                                                                                                                                                                                              |  1,078 | セッション・キャッシュ更新（`105-393`, `619-770`）を精読                                    |                                                                                 |
| `utils/`（17 ファイル）                                                                                                                                                                                                                                                                      |  7,809 | 主要 6 ファイル全読 + 横断 grep                                                             | `validators.py`(1468) は対象関数を精読、`storage.py`/`market_utils.py` は部分読 |
| `bg/`（5 ファイル）                                                                                                                                                                                                                                                                          |  2,307 | **全読**                                                                                    |                                                                                 |
| `schemas/`（5 ファイル）                                                                                                                                                                                                                                                                     |    643 | **全読**                                                                                    | 本番未使用を確認                                                                |
| `static/js/ui.js` / `chart.js`                                                                                                                                                                                                                                                               |  5,764 | 描画ホットパス・タイマー・オブザーバ・チャート生成を精読                                    | 残りは描画関数の網羅的突合（U14）                                               |
| `static/js` その他 + `static/css`                                                                                                                                                                                                                                                            | 28,056 | 横断 grep + 初期化経路                                                                      |                                                                                 |
| `templates/`                                                                                                                                                                                                                                                                                 |  3,067 | grep（inline handler / SRI / nonce）                                                        |                                                                                 |
| `native_host/native_host.py`                                                                                                                                                                                                                                                                 |  1,578 | 認証・フレーム読み書き（`205-334`, `1075-1324`）を精読                                      | 残りは Windows プロセス検証（U14）                                              |
| `native_host/start_backend.py`                                                                                                                                                                                                                                                               |    429 | **全行読**                                                                                  |                                                                                 |
| `chrome_extension/background.js`                                                                                                                                                                                                                                                             |    825 | メッセージ処理・トークン取得（`140-215`, `593-700`）を精読                                  | 残りはバッジ更新（U14）                                                         |
| `chrome_extension/popup.js` / `content.js`                                                                                                                                                                                                                                                   |  1,150 | ポーリング・注入（`320-360`）を精読                                                         | 残りは UI 文字列（U14）                                                         |
| `tests/`                                                                                                                                                                                                                                                                                     | 60,175 | 実行 + patch 依存の grep                                                                    | 個別テストの妥当性は未監査                                                      |

**限界（残）**: `utils/validators.py` の JSON 修復ヒューリスティクス（1,468 行の大半）、`services/ai_service.py` のツールループ残部、`openai_service.py` のツールループ／画像／埋め込み、`native_host.py` の Windows プロセス署名検証、`ui.js`/`chart.js` の全描画関数、`content.js` の DOM 抽出は、依然として関数単位の精読に留まる。これらは U14 に残す。**本パスで「全行読」と記したファイル以外を「問題なし」と解釈してはならない。**

---

## 3. 評価レンズと重大度

| 記号     | レンズ                                         |
| -------- | ---------------------------------------------- |
| `EDGE`   | エッジケース・異常系・入力境界                 |
| `RACE`   | 非同期競合・ロック順序・並行更新               |
| `LEAK`   | リソース / メモリリーク                        |
| `SEC`    | セキュリティ                                   |
| `PERF`   | パフォーマンス                                 |
| `BOUND`  | API 境界（型整合・バリデーション・エラー形式） |
| `DESIGN` | 構造・責務・状態所有権                         |

| 重大度       | 意味                                                  |
| ------------ | ----------------------------------------------------- |
| **Critical** | データ損失・認可不成立・本番停止に直結                |
| **High**     | 静かに誤った結果を返す / 構造的に同種バグを増殖させる |
| **Medium**   | 特定条件で誤動作・性能劣化・保守コスト増              |
| **Low**      | 契約の曖昧さ・命名・重複・衛生                        |

「根本原因か」の判定は **root**（構造を直せば同種が消える）/ **symptomatic**（局所修正に留まる）/ **intended**（意図的仕様・後方互換のため変更禁止）で示す。

---

## 4. 課題一覧

### A. 共通基盤・型定義・API 境界

#### A-01 `schemas/` が本番コードから一度も使われていない — High / BOUND / root — **一部修正**

- 所在: `schemas/__init__.py:1-62`, `schemas/stocks.py`, `schemas/analysis.py`, `schemas/ai_portfolio.py`, `schemas/config.py`
- レビュー時点の事実: `grep -rn "from schemas\|import schemas" --include=*.py .` のヒットは `schemas/` 内部と `tests/` のみ。`StockAddRequest` / `AIChatRequest` / `ScreenerQueryRequest` / `PortfolioUpdateRequest` / `AIPortfolioSaveRequest` / `StockHistoryQueryRequest` はいずれも本番参照 0。
- 影響: Pydantic スキーマという「契約の単一の源」が存在するのに、実際の境界は手書きパーサ。スキーマを直しても本番挙動は変わらず、**テストは通るが本番は別物**という最悪の分岐を作る。
- 判定: **root**。採用するか削除するかを決めるのが修正。放置が最も悪い。
- 修正方向: ルートで `Model.model_validate(...)` を使い、`ValidationError` を `error_response(ErrorCode.MALFORMED_INPUT)` に写像する薄いアダプタを 1 つ作る。あるいはスキーマ層を削除して `utils/validators.py` に一本化する。**どちらでもよいが二重は許さない。**
- **本パスでの進捗**: ポートフォリオと config は schema 採用。他のボディは未移行で、依然として手書きパーサが契約。
- 変更禁止: スキーマの制約値（`PORTFOLIO_SHARES_MAX` 等）は既存 `constants.py` が正。数値を緩めない。

#### A-02 同名別型 `AppConfigSchema` が 2 つ存在 — High / BOUND / root — **修正済み**

- 所在: `schemas/config.py:27`（`port` / `host` / `security` / `logging`）と `utils/validators.py:221`（API キー有無・モデル設定・provider notice）
- レビュー時点の事実: `routes/pages.py:7` は `utils.validators.AppConfigSchema` のみを import。`schemas/config.py` 側は完全な死コード。
- 影響: 同じ名前で意味が違う型が併存し、IDE 補完・grep・将来の contributer を確実に誤導する。
- 判定: **root**（命名と所有権の問題）。
- 修正方向: `schemas/config.py` 側を `PersistedAppConfig` 等に改名して採用するか削除。テンプレート用は `TemplateConfigSchema` に改名。
- **本パスで修正済み**: `RuntimeConfigSchema`（永続設定）と `TemplateConfigSchema`（テンプレート文脈）に分離。旧 `AppConfigSchema` の別名は削除し、定義は `TemplateConfigSchema` の 1 つだけ。

#### A-03 ポートフォリオ入力の検証が 3 箇所で独立実装 — High / BOUND / root — **修正済み（portfolio）**

- 所在（レビュー時点）: `schemas/stocks.py:99`（`PortfolioUpdateRequest`）、`utils/validators.py:253`（`PortfolioInputSchema`）、`route_helpers.py:765`（`_parse_stock_request`）+ 呼び出し `routes/stocks/portfolio.py:71`, `routes/stocks/views.py:414,481,615`
- 事実: いずれも「symbol 正規化 / market 3 値 / shares>=0 / avg_price>=0 / bool 拒否 / allow_inf_nan=False / US 以外の avg_fx_rate 拒否 / 合計上限」を別実装で持つ。
- 影響: 制約の追加漏れが必ず起きる。実際、bool 拒否の実装は 3 箇所で書き方が異なる（`isinstance(v, bool)` と `type(v).__name__ in ("bool_","bool")` の組み合わせ揺れ）。
- 判定: **root**。A-01 と同一修正単位。
- 修正方向: 1 つの Pydantic モデルに統合し、ルートはそれだけを使う。`_parse_stock_request` はモデル呼び出しへ委譲するか削除。
- 変更禁止: 既存のエラーコード対応（`MALFORMED_INPUT` / `INVALID_MARKET` / `UNSAFE_INPUT`）と HTTP ステータスはフロントが依存。変えない。
- **本パスで修正済み（/api/stocks/portfolio のみ）**: `PortfolioUpdateRequest` が唯一の規則所有者。`PortfolioInputSchema`、`MnsValidationError`、`validate_portfolio_input` シムを削除し、ルートは `_parse_portfolio_request()` だけを呼ぶ。`_parse_stock_request` は add/delete/add_ext 用に残る（ポートフォリオは通らない）。

#### A-04 循環 import を `sys.modules.get()` 実行時ディスパッチで回避（24 箇所） — High / DESIGN / root

- 所在: `routes/stocks/common.py:44-100`、`bg/common.py:51,59`、`bg/leader_election.py:36,45,53,59`、`bg/sync_worker.py:61,83,109,117`、`services/realtime/scrapers.py:45`、`services/realtime/tv_client.py:28,311`、`services/realtime/utils.py:35,45,148,208,239`
- 事実: `_get_api_stocks_attr(name, fallback)` は `sys.modules.get("routes.api_stocks").__dict__` を引き、存在すればそちらを、無ければ別モジュールの関数を呼ぶ。テストは `routes.api_stocks` の属性を 257 箇所で patch している。
- 影響:
  - mypy / pyrefly は戻り値を `Any` として扱うため、**型チェックがこの層を完全に素通り**する（「型安全」の看板に穴）。
  - import 順・モジュール存在で呼ばれる実装が変わる。起動経路とテスト経路で別コードが走りうる。
  - 呼び出しグラフが静的に辿れず、デッドコード検出も効かない。
- 判定: **root**。これは「テストのための構造」が本番構造を歪めた典型。
- 修正方向: 依存を**引数注入**（`fetch_batch_fn=` のように既に `services/market_data_service.py` が採用しているパターン）へ統一し、デフォルトは直接 import。テストは関数引数か `monkeypatch.setattr` で差し替える。循環そのものは `app_bg` の責務分割で解消する。
- 変更禁止: `app_bg` の公開関数名（`schedule_sync_all_stocks_now` 等）はテストが直接参照。改名は別ユニットで一括。

#### A-05 本番コードが `"pytest" in sys.modules` で挙動を変える（3 箇所） — Medium / EDGE / root

- 所在: `app.py:563`（シグナルハンドラ登録を skip）、`services/realtime/scrapers.py:797`（`_is_startup_ready` が常に True）、`utils/env_helpers.py:83,96`
- 事実: 明示スイッチ `MNS_SKIP_BOOTSTRAP` が既にあるのに、暗黙のテスト検出が併存する。
- 影響: `scrapers.py:797` は pytest 下で「起動準備完了」を無条件に返すため、**起動待ちの失敗をテストが検出できない**。CI の startup smoke は pytest 外で走るので救われているが、通常のテストではこの経路が未検証のまま。
- 判定: **root**（設定の二重管理）。
- 修正方向: `MNS_SKIP_BOOTSTRAP`（または明示の config フラグ）に一本化し、`sys.modules` 判定を削除。テスト側は conftest で環境変数を設定する。

#### A-06 レスポンス封筒が統一されていない — Medium / BOUND / symptomatic

- 所在: `utils/stock_payload.py:error_response`（`ok/error/error_flag/code/error_code/message/details`）、成功例 `routes/stocks/views.py:459,521`（`{"success": True, "ok": True}`）、`routes/stocks/views.py:383-390`（`{"ok": True, "total": …, "stocks": …}`）
- 影響: フロントは `ok` と `success` の両方を見る必要があり、新規エンドポイントでどちらを返すか判断できない。
- 判定: **symptomatic**（表面）。ただし A-09 の契約型を入れれば自然に解消する。
- 修正方向: 成功封筒を 1 つのヘルパ（`ok_response(**payload)`）に統一し、`success` は後方互換のため当面併記、移行後に削除。
- 変更禁止: `error_code`（int）と `code`（str）の両キーは既存 JS が参照。削除しない。

#### A-07 `code: str(error_code)` は IntEnum では数値文字列になる — Low / BOUND — **誤検知（影響なし）**

- 所在: `utils/stock_payload.py:error_response`（`"code": str(error_code)`）、`error_codes.py:7`（`class ErrorCode(IntEnum)`）
- 事実: Python 3.11+ の `str(IntEnum.MEMBER)` は `"1005"`。`error_code`（int）と実質同一で、`code` は冗長。
- **確認結果**: `static/js/*.js` は `error_code` のみを読み、`code` の名前比較は存在しない（`api.js:1516`、`api_client.js:240`）。クライアント影響はないため取り下げ。契約から削除するのは A-06 の封筒整理時に行う。

#### A-08 `resolve_stocks_for_response` のデフォルトが実装と逆（保有情報漏洩の footgun） — Medium / SEC / root

- 所在: `routes/stocks/common.py:71`（`include_portfolio: bool = True`）vs `utils/stock_payload.py:879`（`include_portfolio: bool = False`）
- 事実: 現在の全呼び出しは明示（`portfolio.py:286` True / `quotes.py:200,205` False / `views.py:256` False / `stream.py:193` False）なので**現時点で漏洩はない**。
- 影響: 将来 `resolve_stocks_for_response()` を引数なしで呼ぶと、保有株数・取得単価・評価損益が公開 SSE/API に載る。デフォルトは安全側（False）であるべき。
- 判定: **root**（安全側デフォルトの原則）。
- 修正方向: ラッパーのデフォルトを `False` に変更（明示呼び出しのみ True）。回帰テストで「引数なし呼び出しに portfolio フィールドが無い」ことを固定。

#### A-09 バックエンド↔フロントの契約が型で表現されていない — Medium / BOUND / root

- 所在: 全ルートの `jsonify({...})` と `static/js/api_client.ts`（`unknown` からの都度ナローイング）
- 影響: フィールド名変更がコンパイル時に検出されない。`schemas/` がまさにこの穴を埋めるために作られた形跡がある（A-01）。
- 判定: **root**。A-01 と同一ユニットで扱う。

---

### B. コア・設定

#### B-01 `config_store.py` 1248 行に鍵管理・暗号化・永続化・移行が同居 — High / DESIGN / root

- 所在: `config_store.py` 全体（broad except 16、suppression 18 — プロジェクト最多）
- 事実: マスターキーの生成/復号（DPAPI・keyring・環境変数）、Fernet 暗号化、`config.json` の読み書き、ロック、移行、検証が 1 モジュール。
- 影響: 「復号失敗時は fail-closed で上書きしない」という**最重要の不変条件**（`config_store.py:1209-1218`）が巨大な関数群に埋もれ、変更時に壊されやすい。実際に例外を握る箇所が多く、失敗経路の追跡が困難。
- 判定: **root**（責務分離）。
- 修正方向: `master_key`（生成/復号/fail-closed）、`config_io`（read/write/lock）、`secret_codec`（encode/decode）に分割。振る舞いは変えない pure refactor。
- 変更禁止: **fail-closed の鍵復号は絶対に緩めない。** 鍵が復号できない場合に新鍵を生成して上書きする変更は、暗号化データの恒久喪失に直結する。

#### B-02 `app.py` 919 行に bootstrap・認証・CSP・静的配信・Jinja が同居 — Medium / DESIGN / root

- 所在: `app.py`
- 影響: セキュリティ設定（CSP nonce、CSRF、trusted origin）とルーティング/テンプレート都合が同一ファイルで、セキュリティレビューのたびに全体を読む必要がある。
- 判定: **root**。`create_app()` のファクトリは既にあるため、`security_headers.py` / `static_versioning.py` / `template_globals.py` への抽出は機械的。
- 変更禁止: CSP の値（`security_config.py:109`）と nonce 連携は変更しない。

#### B-03 状態が 4 モジュールに分散し所有権が不明 — Medium / DESIGN / root

- 所在: `app_state.py`(434) / `market_state.py`(530) / `execution_state.py`(118) / `ai_state.py`(335)
- 事実: `app_state.market.*` への直接属性代入が各所にあり、`scratch/debug_announce.py` は `app_bg._sse_full_snapshot_counter` を直接書き換える。
- 影響: どのロックがどの状態を保護するかの対応表がコードから読めない。新規フィールド追加時にロック漏れが起きる。
- 判定: **root**（J-01 と合わせて解消）。

#### B-04 `logging_config.py` の秘匿値マスキング範囲が不明 — Low / SEC / 要確認

- 所在: `logging_config.py`(352)
- 事実: JSON ログ出力とローテーションは実装済み。API キー・トークンの redaction がフィールド名列挙依存か、値パターン検出かが未確認。
- 判定: **要確認**（U14）。redaction が無い場合は High に昇格。

---

### C. ルート / API

#### C-01 `api_system.py` 1411 行に権限の異なる 7 機能が同居 — Medium / DESIGN / root

- 所在: `routes/api_system.py`（credentials / verify / health / cache-stats / metrics / csrf-token / csp-report / shutdown / ai-usage）
- 影響: `/api/csp-report`（未認証）と `/api/shutdown`（トークン必須）が同一ファイル。権限境界のレビューが困難。
- 判定: **root**。`api_credentials.py` / `api_admin.py` / `api_observability.py` に分割（URL は不変）。

#### C-02 認可述語が 5 箇所で独立実装 — High / SEC / root

- 所在: `utils/networking.py:259`、`app.py:692`、`routes/api_system.py:171`、`route_helpers.py:337`、`routes/stocks/views.py:585`
- 事実: いずれも `secrets.compare_digest` を使っており**定数時間比較自体は正しい**（G-04）。しかし判定条件（Origin / Sec-Fetch-Site / admin token / allow_remote / extension token）の組み合わせが各実装で異なる。
- 影響: 1 箇所だけ条件が緩い/厳しいと、そこがバイパスまたは機能不全になる。セキュリティ境界が単一の関数で読めない。
- 判定: **root**。
- 修正方向: `utils/networking.require_trusted_or_admin` を唯一の実装とし、他は薄い再エクスポートに。`views.py:585` の拡張トークン経路は別関数（`require_extension_token`）として明示的に分離。
- 変更禁止: 既存の許可条件を「ついでに」厳しくしない。挙動を変えるなら別 PR でテスト付き。

#### C-03 `user_stocks_lock` を保持したまま暗号化+fsync 書き込み — Medium / RACE,PERF / root

- 所在: `routes/stocks/views.py:437-470`（`with app_state.market.user_stocks_lock:` の中で `save_user_stocks()`）、`utils/storage.py:574-660`（内部で再度 RLock 取得 → 再入）
- 影響: 書き込み中（fsync は数百 ms になりうる）、SSE 配信（`_resolve_stocks_for_response` は同じ lock を取る）と snapshot API が待たされる。ディスクが遅い環境で UI が固まる。
- 判定: **root**（ロックのスコープ設計）。
- 修正方向: ロック内で `copy.deepcopy` によるスナップショットを作り、`save_user_stocks(snapshot)` をロック外で実行。失敗時のロールバックは「世代番号 + 再読込」で行う。
- 変更禁止: 失敗時にメモリ状態を巻き戻す現在の挙動（`container.pop` 等）は維持。

#### C-04 `total` が総件数ではなく返却件数 — Low / BOUND / symptomatic

- 所在: `routes/stocks/views.py:388`（`"total": min(len(filtered), limit_val)`）と `:389`（`"totalFiltered": len(filtered)`）
- 影響: `total` をページングの総数として使うクライアントは最終ページを誤認する。
- 判定: **symptomatic**。`total` を `len(filtered)` にするか、`returned` に改名して `total` を総数に。フロントの参照を確認してから（U14）。

#### C-05 SSE の `Last-Event-ID` パース失敗時の扱いが不明 — Medium / EDGE — **仕様確認済み（欠陥なし）**

- 所在: `routes/stocks/common.py:183-192`（`_parse_last_event_id`）
- **確認結果**: 非数値は `0`、負値は `max(0, value)` で `0` にクランプされ、例外→500 にはならない。巨大整数はそのまま通過するが、リプレイはスライディングウィンドウで未保持 ID を初期スナップショットにフォールバックする。既存の `tests/test_sse_replay.py` が有効 ID のリプレイを固定している。追加修正なし。

#### C-06 `routes/api_stocks.py` は後方互換の再エクスポート層 — Low / DESIGN / intended

- 所在: `routes/api_stocks.py`(249)
- 事実: テストが `routes.api_stocks` の属性を patch するため、実体は `utils/*` に移っても名前だけ残している。
- 判定: **intended**（当面は維持）。A-04 の解消時に一緒に畳む。

---

### D. サービス層

#### D-01 サーキットブレーカーのキー空間が 2 種類混在 — High / EDGE,RACE / root — **修正済み**

- 所在: `services/stock_service.py:64,256,353`（`circuit_key = f"{market}:{symbol}" if market else symbol`）vs `services/stock_provider.py:654`（`is_circuit_open("yfinance_history", symbol=symbol)` — 素の symbol）
- 事実: 同じブレーカ名 `yfinance_history` に対し、`us:AAPL` と `AAPL`、`jp:7203.T` と `7203.T` が別エントリとして記録される。
- 影響: 失敗カウントが実質半分になり、**サーキットが開くべき状況で開かない**。逆に market 無し経路だけ開いて market 付き経路が通り続けるなど、不整合な遮断になる。静かに外部 API を叩き続けるため 429 を悪化させる。
- 判定: **root**。
- 修正方向: キー生成を `utils` の単一関数（`history_circuit_key(market, symbol)`）に集約し、全呼び出しをそれに通す。既存の開状態キーとの互換は不要（プロセス内状態）。
- 変更禁止: しきい値・cooldown の値は変えない（別途チューニング）。

#### D-02 `or` 連鎖が 0.0 を「欠損」と誤認 — Medium / EDGE / root

- 所在: `services/market_data_service.py:94-120`（`market_cap`, `pe_ratio` など）、同 `:140-160`
- 事実: `normalize_optional_number()` は `None` と `0.0` を区別して返す設計だが、呼び出し側が `a or b or c` で潰している。
- 影響: PER 0.0（赤字・特殊会計）、出来高 0（取引停止）、変化率 0.0（前日比変わらず）が「次のキーへフォールバック」し、別ソースの値や 0 に化ける。**前日比 0.0 の銘柄が別ソースの古い値で表示される**可能性。
- 判定: **root**（None と 0 の混同）。
- 修正方向: `first_not_none(*values)` ヘルパを導入し、`or` を置換。0.0 は正当値として保持。
- 変更禁止: 欠損時の最終フォールバック（0.0 や price）は現行仕様。変えない。

#### D-03 時価総額の欠損時に「売買代金」を代用している — Medium / EDGE / root — **修正済み**

- 所在: `services/market_data_service.py:165-172`（`fallback_size = price * volume` を `market_cap` の最終フォールバックに使用）
- 影響: 時価総額と売買代金は次元が異なり、ヒートマップのタイル面積が最大 2〜3 桁ずれる。ヒートマップは「面積＝時価総額」という暗黙の契約で読まれるため、**視覚的に静かに嘘をつく**。
- 判定: **root**（ドメイン不変条件の欠落）。
- 修正方向: `shares * price` のみを導出値として許可し、`shares` が無い銘柄は `market_cap` 不明として除外（または `market_cap_source: "estimated"` を付与して UI で区別）。`price*volume` は使わない。
- 変更禁止: `market_cap <= 0` の行を除外する現在の挙動は維持。

#### D-04 バッチ失敗を握り潰し、部分結果を 200 で返す — Medium / EDGE,BOUND / symptomatic

- 所在: `services/market_data_service.py:230-268`（`except Exception as sym_exc:` / `except Exception:` + `logger.exception`）
- 影響: 一部銘柄の取得に失敗しても API は 200 を返し、クライアントは「その銘柄が存在しない」のか「取得に失敗した」のか区別できない。スクリーナーの件数が黙って減る。
- 判定: **symptomatic**（表面）。根本は A-09 の契約不足。
- 修正方向: レスポンスに `missing: [symbol...]` を追加（後方互換）。全滅時のみ 502/503。

#### D-05 `market_is_open_cached` はキャッシュしない — Low / DESIGN / symptomatic

- 所在: `services/market_data_service.py:322`（`return is_market_open(market)`）
- 影響: 名前が実装と矛盾し、呼び出し側が「安い」と誤解してホットパスで呼ぶ。
- 判定: **symptomatic**。改名（`market_is_open`）か、実際に TTL キャッシュを持たせる。

#### D-06 `stock_provider.py` 1814 行に取得・セッション・リトライ・ブレーカが同居 — High / DESIGN / root

- 所在: `services/stock_provider.py`（broad except 28 — 最多）
- 影響: D-01 のキー不一致が生まれた構造的原因。yfinance の仕様変更時に 1 ファイル全体を追う必要がある。
- 判定: **root**。`session` / `circuit` / `fetch` / `parse` に分割。`stock_service.py`(398) との境界（キャッシュ付き履歴 vs 生取得）も明文化。

#### D-07 AI サービス群のプロバイダ分岐が分散 — Medium / DESIGN / 要確認

- 所在: `services/ai_service.py`(2258) / `services/openai_service.py`(1214) / `services/ai_tools.py`(611) / `services/mistral_compat.py`(262) / `services/preview_responses.py`(163)
- 事実: 抽象化層があるように見えるが、プロバイダ固有の分岐が各ファイルに散在する疑い。**未精読**。
- 判定: **要確認**（U14）。

#### D-08 有界キャッシュは適切 — intended / 変更禁止

- `services/embeddings_service.py:21` `LRUCache(maxsize=1024)`、`app_state.py:65` `_MAX_ENTRIES=4096`、`services/stock_provider.py:143` `_TICKER_CACHE_MAX=256`、`utils/disk_cache.py:132` `max_entries=500`。**リーク指摘は該当しない。**

---

### E. バックグラウンド / リアルタイム

#### E-01 `sync_worker.py` 1377 行 — High / DESIGN / root

- 所在: `bg/sync_worker.py`
- 影響: 同期・補間・キャッシュ書き込み・SSE 発火が同居し、ロック順序（`user_stocks_lock` → `sse_data_lock`）の不変条件がコード全体に散る。1 箇所の変更がデッドロックになりうる。
- 判定: **root**。`sync_cycle` / `interpolate` / `publish` に分割。

#### E-02 テスト用グローバルが本番経路に露出 — Medium / DESIGN,LEAK / root

- 所在: `bg/common.py:31-37`（`_sse_payload_cache`, `_sse_payload_cached_generation`, `_sse_full_snapshot_counter`）、`scratch/debug_announce.py`（追跡ファイルが直接代入）
- 影響: モジュールグローバルを外部から書き換える前提の設計で、状態の所有権が無い。J-01 と同根。
- 判定: **root**。世代管理をクラス（`SSEPayloadCache`）に閉じ込め、テストはそのインスタンスを差し替える。

#### E-03 リーダー選出は `workers=1` 前提の二重防御 — Low / RACE / intended

- 所在: `bg/leader_election.py`、`gunicorn.conf.py:34,86-92`（`workers != 1` で FATAL）
- 事実: マルチワーカーを明示的に禁止し、起動時に検証している。**この防御は維持必須**（インメモリ状態前提のため）。
- 判定: **intended**。リーダー選出コードの複雑さは将来の拡張用。削るなら別途判断。

#### E-04 SSE スナップショットがクライアントごとに再構築 — Medium / PERF / root

- 所在: `utils/stock_payload.py:879-1050`（`_resolve_stocks_for_response`）→ `routes/stocks/stream.py:193`
- 事実: クライアントごと・配信ティックごとに 3 市場分の行をコピーし、`realtime_engine.get_market_snapshot()` / `get_pts_snapshot()` を取得してマージ。`bg/common.py` に既にペイロードキャッシュがあるのに、こちらは別経路。
- 影響: クライアント数 × 銘柄数 × ティックの割り算。SSE リスナー上限を上げるほど CPU が線形増加。
- 判定: **root**（キャッシュの二重実装）。世代番号付きの共有スナップショットを 1 つ作り、全クライアントが同じ JSON 文字列を参照する。

---

### F. データ層・永続化

#### F-01 永続化は堅牢（変更禁止） — intended

- 所在: `utils/storage.py:574-660`、`:128`、`:451-469`、`utils/disk_cache.py:452-469`
- 事実: RLock + プロセス間ファイルロック + 一時ファイル + `fsync` + `os.replace` + ディレクトリ fsync + リビジョン/mtime 更新。暗号化は Fernet。
- 判定: **intended**。この設計を簡略化してはならない。

#### F-02 データ配置の `APP_DATA_DIR` 移行が未完 — Medium / DESIGN / root

- 所在: `.gitignore:41`（「If migrating to APP_DATA_DIR, clean these from the repo root manually.」）、`utils/storage.py`、`config_store.py` のパス解決
- 影響: 起動ディレクトリ依存でデータが分裂しうる（同じアプリを別 cwd から起動すると別の `user_stocks.json` を見る）。バックアップ・移行手順も曖昧。
- 判定: **root**。パス解決を単一関数（`data_dir()`）に集約し、ログに実パスを 1 行出す。
- 変更禁止: 既存ユーザーのデータを移動・削除しない。移行はコピー + フォールバック読み込み。

#### F-03 ディスクキャッシュの eviction は「mtime 古い順」で LRU ではない — Low / PERF / 要確認

- 所在: `utils/disk_cache.py:248-262`（`_evict_if_needed` は mtime 昇順で削除）
- 事実: `get` が mtime を更新しない場合、よく読まれるエントリでも古いと削除される。命名・コメントが LRU を示唆していれば契約違反。
- 判定: **要確認**（U14）。`get` での touch 有無を確認し、意図を明文化。

#### F-04 リレーショナル DB は存在しない — 該当なし

- データ層は「暗号化 JSON + ディスクキャッシュ」。SQL/ORM/トランザクションに関する指摘は本プロジェクトには適用されない。

---

### G. セキュリティ

#### G-01 認可の単一実装化 — High / SEC / root

- C-02 と同一。要修正ユニット U6。

#### G-02 `/api/csp-report` は適切に境界化されている — intended / 変更禁止

- 所在: `routes/api_system.py:1166-1180`
- 事実: `_read_bounded_request_body(max_size=MAX_JSON_SIZE)` で 1 MiB 上限を適用した後に `json.loads`。MIME 非依存。**当初懸念は誤検知だったため取り下げる。**
- 判定: **intended**。

#### G-03 CSP は厳格 — intended / 変更禁止

- 所在: `security_config.py:109`（`script-src 'self' 'strict-dynamic'` + nonce、`unsafe-inline` なし）、`security_config.py:137`（`content_security_policy_nonce_in=["script-src"]`）
- 事実: テンプレート内の inline ハンドラは 0 件（`onclick/onchange/onload` の grep 結果 0）。
- 判定: **intended**。緩めない。

#### G-04 トークン比較は全て定数時間 — intended / 変更禁止

- 所在: `app.py:693`、`routes/api_system.py:171,290`、`routes/stocks/views.py:594`、`route_helpers.py:347`、`shutdown_manager.py:192,230`、`utils/networking.py:259,381,423`
- 判定: **intended**。`==` への変更は禁止。

#### G-05 `MNS_ALLOW_CLIENT_API_KEY=1` が CI 全体に設定 — Medium / SEC / 要確認

- 所在: `.github/workflows/ci.yml:16`
- 影響: 本番で誤って有効化されると、クライアント供給の API キーを受理する。起動時に「本番モード + このフラグ」を拒否/警告するガードがあるか未確認。
- 判定: **要確認**（U14）。無ければ起動時ガードを追加。

#### G-06 `/api/shutdown` のトークンが admin token と別系統 — Low / SEC / intended

- 所在: `routes/api_system.py:1298`、`shutdown_manager.py:192,230`
- 事実: `X-MNS-Shutdown-Token` を `compare_digest` で検証。使い捨てトークン（`.mns_shutdown_token.used`）の仕組みあり。
- 判定: **intended**。設計は妥当。ドキュメント化のみ。

---

### H. フロントエンド

#### H-01 `ui.js` 3793 行 + `index.css` 5543 行 — High / DESIGN / root

- 所在: `static/js/ui.js`、`static/css/index.css`
- 影響: 複数画面の DOM 構築が 1 ファイル。変更のたびに全体を把握する必要があり、削除・移動のコストが高い。
- 判定: **root**。画面単位（dashboard / detail / watchlist / settings 連携）に分割。CSS はコンポーネント単位へ。

#### H-02 リアルタイム経路が複数系統併存 — High / DESIGN / 要確認

- 所在: `static/js/api_client.ts`（SSE 本体、`lastEventId` 保持）、`static/js/api.js:222-234`（`sseApiClient = new APIClient("/api")`）、`static/js/realtime_client.js`(273)、`static/js/tradingview_manager.js`(833)
- 影響: どれが正のトランスポートかコードから即答できない。再接続・Last-Event-ID・visibility 復帰のロジックが分散すると、二重接続やイベント欠落の原因になる。
- 判定: **要確認**（U14）。使用箇所を確定し、不要な経路を削除。

#### H-03 `api.js` 2684 行にエラーハンドラ + SSE + ドメイン API が同居 — Medium / DESIGN / root

- 判定: **root**。`errors.js` / `sse.js` / `endpoints/*.js` へ分割。

#### H-04 型安全な TS と生成物検証は良好 — intended / 変更禁止

- 所在: `static/js/api_client.ts`（`unknown` + ナローイング、`any` 0）、`scripts/verify_generated_frontend.mjs`
- 判定: **intended**。`any` の導入や検証スクリプトの緩和は禁止。

#### H-05 `npm run compile` が追跡ファイルを書き換える — Low / DESIGN / symptomatic

- 所在: `package.json:9`（`tsc -p tsconfig.json && prettier --write static/js/api_client.js`）
- 影響: ビルドが作業ツリーを dirty にし、ローカルと CI の prettier バージョン差で `verify-generated` が落ちうる。生成は一時ディレクトリ→比較のみにすべき。
- 判定: **symptomatic**。`verify-generated` があるので、`compile` から `--write` を外せる。

#### H-06 XSS 対策は良好 — intended / 変更禁止

- 事実: `innerHTML` の実使用 0 件（10 ヒットはすべて「innerHTML を使わない」旨のコメント）。DOM API + `textContent` で構築。
- 判定: **intended**。

#### H-07 `JSON.parse` 19 箇所のエラー処理は未監査 — Low / EDGE / 要確認

- 所在: `static/js/*`（`api_client.ts:312` は try 内であることを確認）
- 判定: **要確認**（U14）。SSE の `data:` 行パースは壊れたフレームで例外→接続断になりうる。

#### H-08 `setInterval` のクリーンアップ — intended

- 事実: `setInterval` 10 / `clearInterval` 9。差分の 1 件は `utils.js:648 startCsrfAutoRefresh`（ページ寿命の CSRF 更新タイマー、`window.__mnsCsrfRefreshTimer` で二重起動防止）。
- 判定: **intended**。リークではない。

---

### I. ネイティブホスト / Chrome 拡張

#### I-01 `native_host.py` 1578 行に認証・プロセス起動・パス検証が同居 — Medium / DESIGN / 要確認

- 所在: `native_host/native_host.py`（broad except 15、suppression 14）
- 判定: **要確認**（U14）。トークン検証と `subprocess` 引数の組み立てが同一ファイルにあるため、インジェクション観点のレビューを集中させるべき。

#### I-02 `start_backend.py` の多重起動防止とロック残留 — Medium / RACE / 要確認

- 所在: `native_host/start_backend.py`(429)、`.backend.start.lock`
- 判定: **要確認**（U14）。クラッシュ後にロックが残ると起動不能になる。PID 生存確認とタイムアウトを確認。

#### I-03 拡張のトークン保存先と接続元制限 — Medium / SEC / 要確認

- 所在: `chrome_extension/background.js`(825)、`chrome_extension/manifest.json`
- 判定: **要確認**（U14）。`externally_connectable` と `chrome.storage` のスコープを確認。

#### I-04 ネイティブログは追跡外 — intended

- `.gitignore` に `native_host/native_host.log*` があり、`git ls-files` にも出ない。良好。

---

### J. ツール・テスト・リポジトリ衛生

#### J-01 追跡されたデバッグスクリプト `scratch/debug_announce.py` — High / DESIGN / root — **修正済み**

- 所在: `scratch/debug_announce.py`（15 行、`unittest.mock` を import し `app_bg._sse_full_snapshot_counter` を直接代入）
- **本パスで worktree から削除済み**（`git status` は `D scratch/debug_announce.py`。追跡解除はコミット時）。必要なら `tests/` 配下の正式なテストへ移す。
- 影響: 本番ツリーにテスト専用コードが混入。mypy/bandit/ruff の走査対象を無駄に増やし、グローバル状態への直接代入の前例を作る。`scratch/` は `.gitignore` にも無い。
- 判定: **root**（衛生）。
- 修正方向: 削除し、必要なら `tests/` 配下の正式なテストへ移す。`scratch/` を `.gitignore` に追加。

#### J-02 テストが本番未使用の `schemas/` を検証している — Medium / BOUND / root — **一部修正**

- 所在: `tests/test_schemas.py`、`tests/test_code_review_goal_audit_2026_*.py` など
- 影響: 「バリデーションはテスト済み」という錯覚。実際の境界は `_parse_stock_request` で、こちらは別テスト。
- 判定: **root**。ポートフォリオと config は本番経路が schema を使うようになり解消。AI ポートフォリオ・chat・analyze 系の schema テストはまだ本番未使用のモデルを検証している（A-01 の残り）。

#### J-03 テストが `routes.api_stocks` を 257 箇所 patch — Medium / DESIGN / root

- 影響: A-04 の構造を固定化し、リファクタを困難にする。
- 判定: **root**。A-04 と同時に、関数引数注入へ移行。

#### J-04 CI は充実している — intended

- 事実: ruff / flake8 / pylint(errors) / mypy / pyrefly(Linux+win32) / bandit(medium) / pip-audit / SRI / coverage 75% / startup smoke / Python 3.12-3.14 / POSIX 永続化テスト。
- 判定: **intended**。この水準を落とさない。

#### J-05 flake8 は `E9,F63,F7,F82` のみで ruff と重複 — Low / DESIGN / symptomatic

- 影響: 実質「構文チェック」だけのためにツールを 1 つ維持している。ruff に統合可能。
- 判定: **symptomatic**。

#### J-06 pyrefly の 20 suppressed が不可視 — Low / DESIGN / symptomatic

- 影響: 0 errors で通るが、抑制が増えても気づけない。
- 修正方向: 抑制数を CI で記録し、増加時に失敗させる（または `# pyrefly: ignore` の理由必須化）。

#### J-07 本レビューの作業ディレクトリ `.review-out/` が未追跡 — Low / 衛生 — **修正済み（解消）**

- 事実: レビュー時は `?? .review-out/` があった。
- 判定: **本パスで削除済み**。`git status` に `.review-out/` は存在しない。

#### J-08 `scratch/` が `.gitignore` に無い — Low / 衛生 — **修正済み**

- `.gitignore` の Temp / Debug / Artifacts 節に `scratch/` を追加済み。

---

### K. リアルタイム / AI / ネイティブ / 拡張（本パスの新規所見）

#### K-01 `add_symbol` / `remove_symbol` が `self.lock` 保持中に別ロックを最大 1 秒待つ — Low / RACE / symptomatic

- 所在: `services/realtime/tv_client.py:119-132`, `:138-152`
- 事実: 銘柄集合用の非再入 `self.lock` を保持したまま `_lifecycle_lock.acquire(timeout=1.0)` を試みる。WS 受信スレッド（`_on_message`）も同じ `self.lock` を取るため、`_lifecycle_lock` が競合すると受信処理が最大 1 秒止まる。
- **確認結果**: 逆順の入れ子は存在しない。`_on_open` は `_lifecycle_lock` を解放してから `self.lock` を取る（`tv_client.py:362-367` と `:403`）。したがって**デッドロックではない**。当初「ABBA 逆転」と記載したが、実コード確認の結果取り下げる。
- 影響: 再接続と銘柄登録が重なったとき、クォート受信が最大 1 秒遅延する。頻度は低い。
- 判定: **symptomatic**。ロック内でスナップショットを作り、送信はロック外で行う。
- 変更禁止: `add_symbol` 内の同期的な `quote_add_symbols` 送信は維持（購読漏れ防止）。

#### K-02 Yahoo JP の一時停止がフォールバックまで殺す — Medium / EDGE / root — **修正済み**

- 所在: `services/realtime/scrapers.py:773`（`_pause_until[key] = now + pause_duration`）、`:1079-1084`（`_active_symbols` が停止中の銘柄を除外）、`:1061-1077`（フォールバックは `_fetch_regular_with_fallback` 内のみ）
- 事実: 一次ソース（Yahoo JP）の連続失敗で `_pause_until` が立つと、その銘柄はワーカーの `target_symbols` から外れる。SBI / Nikkei225JP / Minkabu のフォールバックは `_fetch_regular_with_fallback` の内部でしか呼ばれないため、**停止中はフォールバックも実行されない**。
- 影響: 一次ソースが構造変更で壊れたとき、代替ソースがあるのに最大 120 秒間まったく更新されない。「フォールバックのための停止」が「全ソース停止」になる。
- 判定: **root**（停止判定のキーが「パイプライン」ではなく「一次ソース」になっている）。
- 修正方向: 停止は一次ソースのみに適用し、`_active_symbols` は「全ソース停止」でのみ除外する。あるいは `_fetch_regular_with_fallback` を停止中でも呼び、内部で一次をスキップする。

#### K-03 `stop()` が PTS ワーカーを待ちきれず再起動不能になる — Medium / RACE / root — **修正済み（sleep 部分）**

- 所在: `services/realtime/engine.py:555`（`time.sleep(SCRAPER_REQUEST_STAGGER_SEC)`）、`:627`（`join(timeout=5.0)`）、`:601-610`（`start()` の `RuntimeError`）
- 事実: PTS ループは銘柄ごとに**割り込み不可**の `time.sleep` を実行する。`stop()` は 5 秒しか join しないため、銘柄数 × stagger が 5 秒を超えるとスレッドが残る。`start()` は残存スレッドを見て `RuntimeError("previous PTS worker is still running")` を投げ、`restart()` は「deferred」で戻る。
- 影響: 銘柄が多い環境で再起動（ウォッチドッグの crash recovery）が機能せず、リアルタイム更新が恒久的に止まる。
- 判定: **root**（停止可能性の欠落）。
- 修正方向: `_interruptible_sleep(lambda: self.running and self._pts_epoch == my_epoch, stagger)` に置換。`stop()` は join 後に生存を確認し、残存時は明示的に失敗を返す。

#### K-04 `except (RuntimeError, Exception)` は後者が前者を包含 — Low / DESIGN / symptomatic — **修正済み**

- 所在: `services/realtime/engine.py:276`, `:312`
- 影響: 到達しないタプル要素。lint で検出されないため同種が増える。
- 判定: **symptomatic**。`except Exception` に統一（意図が「送信失敗を握る」なら `RuntimeError` のみに絞る）。

#### K-05 同一クォートを 2 回ディスパッチ — **誤検知・取り下げ**

- 所在: `services/realtime/tv_client.py:245-258`
- **確認結果**: dash 変種の送出は `if "." in bare_sym:` の内側にあり、`NASDAQ:AAPL` では full と bare の 2 回だけ。`NYSE:BRK.B` は 3 形態すべて異なるため 3 回で正しい。重複は存在しない。
- 判定: **誤検知**（初回パスの読み違い）。`tests/test_review_fixes_2026_10_21.py` に挙動ロックを残した。

#### K-06 `_scraper_market_state` が app_state を永久キャッシュ — Low / DESIGN / 要確認

- 所在: `services/realtime/utils.py:186-202`
- 事実: モジュールグローバル `_scraper_market_ref` に `app_state.market` を保持し、無効化手段がない。
- 影響: プロセス内で app_state が差し替わる経路（テスト、再初期化）では古いオブジェクトを参照し続ける。本番では実害が薄い。
- 判定: **要確認**（U14）。

#### K-07 拡張のトークン取得がネイティブホストのレート制限を超える — Medium / EDGE / root — **修正済み（サイクル 4）**

- 所在: `chrome_extension/background.js:620-688`（`getContext` で 2 回、`startBackend` の `queryTokens` が最大 5 回 × 2 回）と `native_host/native_host.py:243-260`（秘密返却は 3 回 / 30 秒）
- 事実: ポップアップを開いて `getContext` を呼ぶと 2 回消費し、続けて「起動」を押すと 1 回目で残り 1 回を消費、2 回目以降は拒否される。`queryTokens` は 600ms 間隔で 5 回リトライするが、ウィンドウが 30 秒なので**リトライの大半が構造的に失敗する**。
- 影響: `get_extension_api_token` が取れず、`/api/stocks/add_ext` が 401/403 → 1 回だけ再取得 → 失敗 → ユーザーには「追加できない」と見える。
- 判定: **root**（クライアント側リトライ方針とサーバ側予算の不整合）。
- 修正方向: 拡張はトークンを 1 回だけ取得して `storage.session` に保持し、再取得は 401/403 時のみ。`queryTokens` のポーリングは「バックエンド起動待ち」に限定し、トークン取得は成功するまで 30 秒間隔にする。ネイティブホスト側の予算は緩めない。
- 変更禁止: `NATIVE_HOST_TOKEN_ACTION_MAX=3/30s` はローカル攻撃面の防御。緩和しない。
- **修正済み（サイクル 4）**: `getContext` / `startBackend` の先行取得を削除し、add と stop が必要時に 1 回だけ取得。詳細と回帰テストは §0「修正サイクル 4」を参照。

#### K-08 `addStockViaExtension` にタイムアウトがない — Low / EDGE / symptomatic — **修正済み（サイクル 5）**

- 所在: `chrome_extension/background.js:187-205`
- 影響: バックエンドが接続を受けて応答しない場合、`fetch` が無期限に待ち、ポップアップのボタンが「処理中」のまま固まる。
- 判定: **symptomatic**。`AbortController` + 10 秒程度の上限を追加。
- **修正済み（サイクル 5）**: 拡張の add/indices/poll を 10 秒、health を 3 秒、shutdown を 5 秒に。同じ無期限 fetch を `csrfFetch`（120 秒 = gunicorn 上限）と生 fetch にも適用。詳細は §0「修正サイクル 5」。

#### K-09 `_build_mistral_cache_key` の `json.dumps` に `default=` がない — Low / EDGE / 要確認

- 所在: `services/ai_service.py:465-478`
- 事実: `messages` に `datetime`/`bytes`/カスタムオブジェクトが混ざると `TypeError` がリクエスト前に送出される。ルート経由の入力は JSON 由来なので通常は到達しない。
- 判定: **要確認**（U14）。到達するなら 500 ではなく 400 に変換する。

#### K-10 OpenAI ストリームの例外がすべてサーキット失敗に計上される — Low / EDGE / symptomatic

- 所在: `services/openai_service.py:866-880`（`except Exception` → `report_circuit_result(success=False)`）
- 影響: JSON パース失敗などアプリ側の不具合でもサーキットが開き、プロバイダ障害と誤判定する。内側のパース例外は握っているが、外側は一律。
- 判定: **symptomatic**。通信系例外のみ計上。

#### K-11 ネイティブホスト / 拡張の認証は堅牢（変更禁止） — intended

- 事実: `_require_valid_extension_id` は (1) 32 文字 ID 形式、(2) 許可リスト、（3) 親プロセスが許可ブラウザ、（4) `sys.argv[1]` のプロセスオリジンとメッセージ ID の一致、をすべて要求し、不一致は fail-closed。トークン返却は別予算で制限。
- 判定: **intended**。この 4 段検証を簡略化してはならない。

---

## 5. 横断検索の結果（同種アンチパターンの全件）

| パターン                                      |                     件数 | 主な所在                                                                                                                                                                                                                                                  | 扱い                                                                                                                           |
| --------------------------------------------- | -----------------------: | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `except Exception`                            |                  **405** | `services/stock_provider.py` 28、`services/ai_service.py` 23、`routes/api_analysis.py` 23、`services/fallback_provider.py` 20、`services/realtime/scrapers.py` 17、`services/openai_service.py` 16、`config_store.py` 16、`native_host/native_host.py` 15 | 全部が悪ではない。**リクエスト経路（routes）と設定/鍵経路（config_store）は Narrow 化対象**、バックグラウンドの防御は intended |
| bare `except:`                                | 0（`node_modules` 除く） | —                                                                                                                                                                                                                                                         | 良好                                                                                                                           |
| `pylint: disable` / `# noqa` / `type: ignore` |             本番で約 120 | `config_store.py` 18、`crypto_utils.py` 17、`native_host.py` 14、`mistral_compat.py` 14、`search_service.py` 13、`credential_manager.py` 11、`storage.py` 9                                                                                               | 新規追加禁止。既存は理由コメントの有無を監査（U14）                                                                            |
| `sys.modules.get()` ディスパッチ              |                       30 | A-04 参照（`realtime/utils.py` の 6 箇所を追加確認）                                                                                                                                                                                                      | **root 修正対象**                                                                                                              |
| ロック保持中の別ロック待ち（最大 1s）         |                        2 | `services/realtime/tv_client.py:119-132`, `:138-152`                                                                                                                                                                                                      | K-01（逆順ではなく、デッドロックではないことを確認済み）                                                                       |
| クライアント再試行 > サーバ側予算             |                        1 | `background.js`（修正前 660-688） vs `native_host.py:243-260`                                                                                                                                                                                             | K-07（サイクル 4 で修正済み）                                                                                                  |
| `"pytest" in sys.modules`                     |                        3 | A-05                                                                                                                                                                                                                                                      | **root 修正対象**                                                                                                              |
| `innerHTML` 実使用                            |                        0 | —                                                                                                                                                                                                                                                         | 良好                                                                                                                           |
| テンプレート inline handler                   |                        0 | —                                                                                                                                                                                                                                                         | 良好                                                                                                                           |
| `secrets.compare_digest`                      |                       10 | G-04                                                                                                                                                                                                                                                      | 良好                                                                                                                           |
| 有界でないモジュールキャッシュ                |               0 件を確認 | 主要キャッシュは全て上限あり                                                                                                                                                                                                                              | 良好                                                                                                                           |
| `any`（TypeScript）                           |                        0 | `api_client.ts` は `unknown`                                                                                                                                                                                                                              | 良好                                                                                                                           |
| `@ts-ignore` / `eslint-disable`               |                        0 | —                                                                                                                                                                                                                                                         | 良好                                                                                                                           |
| `assert` を本番ロジックに使用                 |   未検出（U14 で再確認） | —                                                                                                                                                                                                                                                         | —                                                                                                                              |
| 追跡されたログ/デバッグファイル               |                0（解消） | `scratch/debug_announce.py` を削除済み                                                                                                                                                                                                                    | J-01 完了                                                                                                                      |

---

## 6. 変更してはいけないもの（意図的仕様・後方互換）

1. **マスターキーの fail-closed**（`config_store.py:1209-1218`）。復号不能時に新鍵を生成して上書きしてはならない。
2. **`user_stocks.json` の原子的書き込み**（`utils/storage.py:574-660`）。`os.replace` + `fsync` + ファイルロックを簡略化しない。
3. **CSP**（`security_config.py:109`）と nonce 連携。`unsafe-inline` を入れない。
4. **定数時間比較**（`secrets.compare_digest`）。`==` に置き換えない。
5. **`workers = 1` の強制**（`gunicorn.conf.py:34,86-92`）。インメモリ状態前提の設計であり、増やすと壊れる。
6. **`error_response` のキー集合**（`ok/error/error_flag/code/error_code/message/details`）。フロントが依存。
7. **`static/js/api_client.js` の生成体制**（`tsc` → `verify-generated`）。手編集しない。
8. **`schemas/` の制約値**（`constants.py` 由来の上限）。緩めない。
9. **エラーコードの数値**（`error_codes.py`）。既存の対応を変えない。
10. **`Last-Event-ID` によるリプレイ**と `bg/common.py` の世代キャッシュ。SSE の正しさの根幹。

---

## 7. 修正ユニットの優先順位（依存関係順）

> 原則: **共通基盤・型・API 境界 → サービス → ルート → フロント**。各ユニットは「修正 → テスト追加（異常系・回帰）→ 静的解析 → ビルド」で閉じる。

| #       | ユニット                                                                                                                                                                           | 含む課題                                                                                         | 根拠                                                                                                          |
| ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------- |
| **U1**  | API 契約の単一化（`schemas/` 採用 or 削除、`AppConfigSchema` 重複解消、成功封筒統一）                                                                                              | A-01, A-02, A-03, A-06, A-09, J-02                                                               | 全ての境界修正の前提。ここを決めずにルートを触ると二度手間。**A-02 と A-03（portfolio）は完了、残りは進行中** |
| **U2**  | `sys.modules` ディスパッチ撤去（引数注入へ）                                                                                                                                       | A-04, C-06, J-03                                                                                 | mypy の穴を塞ぐ。テスト 257 箇所の移行を伴う                                                                  |
| **U3**  | テスト検出の一本化（`MNS_SKIP_BOOTSTRAP` のみ）                                                                                                                                    | A-05                                                                                             | U2 の後。テストが本番経路を検証するようになる                                                                 |
| **U4**  | サーキットブレーカのキー一元化                                                                                                                                                     | D-01                                                                                             | 独立して直せる。外部 API 負荷に直結                                                                           |
| **U5**  | `market_data_service` の None/0 混同・時価総額フォールバック・部分失敗契約                                                                                                         | D-02, D-03, D-04, D-05                                                                           | U4 と独立。UI の「静かな嘘」を消す                                                                            |
| **U6**  | 認可述語の単一実装化                                                                                                                                                               | C-02, G-01                                                                                       | セキュリティ境界。挙動は変えず構造のみ                                                                        |
| **U7**  | 書き込みロックのスコープ縮小（I/O をロック外へ）                                                                                                                                   | C-03                                                                                             | RACE/PERF。回帰テストで並行 add/SSE を固定                                                                    |
| **U8**  | 安全側デフォルト + SSE スナップショット共有                                                                                                                                        | A-08, E-04                                                                                       | セキュリティと性能を同時に改善                                                                                |
| **U9**  | 巨大モジュールの機械的分割（振る舞い不変）                                                                                                                                         | B-01, B-02, C-01, D-06, E-01, H-01, H-03, I-01                                                   | 分割は差分ゼロで行い、テストで固定                                                                            |
| **U10** | フロントのリアルタイム経路統合と生成物ビルド修正                                                                                                                                   | H-02, H-05, H-07                                                                                 | 実使用箇所の確定が先                                                                                          |
| **U11** | リポジトリ衛生（`scratch/` 削除、`.review-out/` 削除、`.gitignore`）                                                                                                               | J-01, J-07, J-08, E-02                                                                           | **J-01/J-07/J-08 は完了**。E-02（テスト用グローバル）は未着手                                                 |
| **U12** | 性能: チャート生成のスライス順序                                                                                                                                                   | `utils/stock_payload.py:501-502`（`to_dict("records")` してから末尾 365 件に切る）→ 先にスライス | 全 DataFrame の dict 化は同期サイクルの CPU を浪費                                                            |
| **U13** | ディスクキャッシュ eviction の意味決定（LRU か mtime か）                                                                                                                          | F-03                                                                                             | 仕様判断が必要                                                                                                |
| **U14** | **未精読領域の第 2 パス**（`ai_tools.py`, `utils/validators.py` の JSON 修復, `native_host.py` の Windows 署名検証, `content.js`, `ui.js`/`chart.js` の全描画関数, `settings.js`） | D-07, B-04, C-05, G-05, I-03, H-02, H-07, K-06, K-09                                             | 本パスの限界。ここで新規課題を追加する                                                                        |
| **U15** | リアルタイムの停止・再起動・フォールバック修正                                                                                                                                     | K-01, K-02, K-03, K-04, K-05                                                                     | **K-02/K-04 完了、K-03 は sleep 部分のみ、K-05 誤検知**。K-01 は Low で据え置き                               |
| **U16** | 拡張のトークン取得をネイティブ予算に適合させる                                                                                                                                     | K-07, K-08                                                                                       | **K-07 はサイクル 4、K-08 はサイクル 5 で完了**。サーバ側予算は変えない                                       |

**修正サイクル 1 の完了状況**: U4（D-01）・U5 の D-03 ・U15 の K-02/K-03/K-04 は完了。K-05 は誤検知として取り下げ（修正なし）。U15 の残りは K-01（Low）、K-03 の in-flight fetch 待ち、K-06。

**修正サイクル 3（照合・剪定）**: 全 72 件を §9 の台帳で修正済み / 一部修正 / 据え置き / 誤検知 / 仕様どおり / 該当なしに分類。修正が残した重複（`validate_portfolio_input` シム、`AppConfigSchema` 別名）を削除し、追跡デバッグスクリプト `scratch/debug_announce.py` を削除、`scratch/` を `.gitignore` に追加。旧ルート実装との差分照合を 603 ペイロードで実施し、エラーコード・文言の乖離がないことを確認（一時スクリプト、worktree から削除済み）。

**修正サイクル 2 の完了状況**: U1 のうち A-02 と A-03（`/api/stocks/portfolio`）を完了。A-01 の他エンドポイント（AI ポートフォリオ、chat、analyze 系、credentials、stocks add/delete/add_ext）と A-06（成功封筒）は未移行。これらは各エンドポイント固有のエラー写像を持つため、次パスで 1 エンドポイントずつ「回帰テスト → 移行 → 全エラー本文の突合」で進める。**修正サイクル 4 の完了状況**: U16 の K-07 を完了。収集・起動時の先行取得を削除し、add / stop が必要時に 1 回だけ取得する。

**修正サイクル 5 の完了状況**: U16 の K-08 と同型の無期限 fetch を完了。拡張・フロントの全生 fetch を `fetchWithTimeout` に通し、タイムアウトは `TimeoutError`、呼び出し元 abort は `AbortError` のまま区別する。

**U12 の根拠（実読）**: `utils/stock_payload.py:501` は `recent_df.to_dict("records")` で全行を Python dict に変換した後、`:502` で `[-ohlc_data_limit:]` に切っている。365 行 × 全銘柄 × 同期周期の無駄な変換。`recent_df.iloc[-ohlc_data_limit:].to_dict("records")` にすれば同一結果で削減できる（ただし `num_records` を使った `chart_data_limit` の判定ロジックがあるため、単純な置換ではなくオフセット計算が必要）。

---

## 8. 未確認事項（次パスで必ず潰す）

1. `static/js` が `code`（文字列）を名前比較しているか（A-07）— **解決済み（比較なし、誤検知）**。
2. `logging_config.py` の秘匿値 redaction の実装方式（B-04）— **未解決**。
3. `routes/stocks/common.py:185` の `Last-Event-ID` 異常値処理（C-05）— **解決済み（不正値は 0 にクランプ）**。
4. `utils/disk_cache.py` の `get` が mtime を更新するか（F-03）— **未解決**。
5. `MNS_ALLOW_CLIENT_API_KEY` の本番ガード有無（G-05）— **未解決**。
6. フロントのリアルタイム経路の実使用関係（H-02）— `api.js:234` の `sseApiClient` が SSE を所有し、`api_client.js` の既定インスタンスは HTTP 用、と読めるが、`realtime_client.js` の使用箇所は未確定。
7. `JSON.parse` 19 箇所の try 有無（H-07）— `api_client.ts:312` と `openai_service` 側は確認済み、フロント其余は未確認。
8. `native_host` の Windows プロセス署名検証（`_get_windows_authenticode_signature` 等）の実効性 — 未精読。
9. `chrome_extension/content.js` の DOM 抽出がページ由来文字列をどうサニタイズするか — 未精読。
10. `pyrefly` の 20 suppressed の内訳（J-06）。
11. AI メッセージ列に JSON 非互換オブジェクトが混入しうるか（AI-01）。

### 本パスで「欠陥なし」と確認したもの（再調査不要）

- `native_host/native_host.py` のフレーム読み書き: 4 バイトヘッダの断片読み・過大フレームの有界 drain・テキスト/バイナリ混在検出・UTF-8 境界検証まで実装され、失敗時は `FATAL_FRAME` で閉じる。**欠陥なし。**
- `native_host/start_backend.py` の多重起動: OS のファイルロック（`msvcrt`/`fcntl`）で直列化され、プロセス死で自動解放。PID ファイルは `os.replace` で原子的。ポーリングで起動完了を待つ設計。**欠陥なし。**
- `stream_mistral_chat` / `stream_openai_chat`: クライアント切断時の `finally` で SDK ストリームを閉じ、サーキットのプローブを解放。**リークなし。**
- 応答キャッシュ: `TTLCache(maxsize=128, ttl=240)`、クライアントは `LRUCache(128/32)`。**無界リークなし。**
- `chart.js` の履歴プリフェッチ: TTL・in-flight 重複排除・件数上限・`AbortController`・タイマー解放が揃っている。**欠陥なし。**
- `ui.js` の詳細パネル開閉: 世代番号で古いコールバックを無効化し、`transitionend` とフォールバックタイマーの両経路でリスナーを解除。**リークなし。**

---

## 9. 課題ステータス台帳（全 72 件の照合結果）

本文の各節と実 diff・テストを 1 件ずつ突き合わせた結果。**「修正済み」は diff とテストの両方があるものだけ**、それ以外は据え置き理由（ユニット）または誤検知の根拠を記す。

内訳: 修正済み 9、一部修正 3、誤検知/仕様確認 3、仕様どおり 14、該当なし 1、据え置き 42（合計 72）。

| ID   | 状態                      | 根拠 / 残余                                                                                                                                                                                                                                |
| ---- | ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| A-01 | 一部修正                  | ポートフォリオと config は本番経路が schema を使用（`route_helpers.py`, `routes/pages.py`）。AI ポートフォリオ・chat・analyze 系のボディは手書きパーサのまま。                                                                             |
| A-02 | **修正済み**              | `schemas/config.py` の `RuntimeConfigSchema` / `TemplateConfigSchema` に分離。旧名の別名を削除。`test_runtime_config_schema_defaults`、`TemplateConfigSchemaTestCase`、`test_r7_app_config_schema_preserves_ephemeral_fields`。            |
| A-03 | **修正済み（portfolio）** | `PortfolioUpdateRequest` が唯一の規則所有者。`PortfolioInputSchema` / `MnsValidationError` / `validate_portfolio_input` シムを削除。`test_schema_contract_2026_10_21.py` 21 ケース + 旧実装との 603 ペイロード差分照合（一時スクリプト）。 |
| A-04 | 据え置き（U2）            | `sys.modules` ディスパッチ 24+ 箇所とテスト 257 patch の移行は独立リファクタ。closeout では触っていない。                                                                                                                                  |
| A-05 | 据え置き（U3）            | `"pytest" in sys.modules` の 3 箇所は U2 後の明示スイッチ移行が必要。挙動未変更。                                                                                                                                                          |
| A-06 | 据え置き（U1 残）         | 成功封筒の統一はフロントの `ok`/`success` 併読を同時に変える必要があり、単独では回帰リスク。                                                                                                                                               |
| A-07 | **誤検知（影響なし）**    | `static/js` は `error_code` のみ参照（`api.js:1516`, `api_client.js:240`）。`code` の名前比較は存在しない。                                                                                                                                |
| A-08 | 据え置き（U8）            | `resolve_stocks_for_response` のデフォルト反転は全呼び出しの監査と SSE 回帰テストが必要。現時点の呼び出しは全て明示。                                                                                                                      |
| A-09 | 据え置き（U2）            | レスポンス型契約は U2 の依存注入後に導入する。                                                                                                                                                                                             |
| B-01 | 据え置き（U9）            | `config_store.py` の機械的分割。振る舞い不変の大規模移動で独立ユニット。                                                                                                                                                                   |
| B-02 | 据え置き（U9）            | `app.py` の分割。同上。                                                                                                                                                                                                                    |
| B-03 | 据え置き（U9）            | 状態所有者の整理は起動・テスト双方に影響。独立ユニット。                                                                                                                                                                                   |
| B-04 | 据え置き（U14）           | `logging_config.py` の redaction は未精読。欠陥の有無を確定していない。                                                                                                                                                                    |
| C-01 | 据え置き（U9）            | `api_system.py` の機能分割。                                                                                                                                                                                                               |
| C-02 | 据え置き（U6）            | 認可述語 5 実装の統一はセキュリティ境界の挙動等価テストが必要。                                                                                                                                                                            |
| C-03 | 据え置き（U7）            | ロック保持中の I/O 分離は並行 add/SSE 回帰テストとセット。                                                                                                                                                                                 |
| C-04 | 据え置き（U14）           | `total` の意味はフロントの参照確定後。今回は未変更。                                                                                                                                                                                       |
| C-05 | **仕様確認（欠陥なし）**  | `_parse_last_event_id` は不正値を 0、負値を 0 にクランプ（`routes/stocks/common.py:183-192`）。500 にならない。                                                                                                                            |
| C-06 | 仕様どおり                | `routes/api_stocks.py` の再エクスポート層はテスト互換の intended。U2 で畳む。                                                                                                                                                              |
| D-01 | **修正済み**              | `history_circuit_key()` を route/service/provider の全 6 呼び出しで使用。`test_history_circuit_key_is_market_qualified`、`test_provider_reads_the_same_circuit_key_the_service_reports`。                                                  |
| D-02 | 据え置き（U5）            | `or` 連鎖の 0.0 欠損扱いは `market_data_service` の契約変更。独立ユニット。                                                                                                                                                                |
| D-03 | **修正済み**              | `price * volume` の代用を削除。`services/market_data_service.py`。`test_heatmap_excludes_symbols_without_a_market_cap`、`test_heatmap_accepts_snake_case_shares_outstanding`。                                                             |
| D-04 | 据え置き（U5）            | 部分失敗を 200 で返す契約は API 利用側の変更を伴う。                                                                                                                                                                                       |
| D-05 | 据え置き（U14）           | `market_is_open_cached` の命名/キャッシュは性能のみ。                                                                                                                                                                                      |
| D-06 | 据え置き（U9）            | `stock_provider.py` の分割。                                                                                                                                                                                                               |
| D-07 | 据え置き（U14）           | AI プロバイダ分岐の統合は未精読領域。                                                                                                                                                                                                      |
| D-08 | 仕様どおり                | 有界キャッシュは適切。変更禁止。                                                                                                                                                                                                           |
| E-01 | 据え置き（U9）            | `sync_worker.py` の分割。                                                                                                                                                                                                                  |
| E-02 | 据え置き（U11）           | テスト用グローバルの本番経路露出は U2/U3 と同時に解消。                                                                                                                                                                                    |
| E-03 | 仕様どおり                | `workers=1` 前提のリーダー選出。変更禁止。                                                                                                                                                                                                 |
| E-04 | 据え置き（U8）            | SSE スナップショット共有は性能改善ユニット。                                                                                                                                                                                               |
| F-01 | 仕様どおり                | 原子的永続化は堅牢。変更禁止。                                                                                                                                                                                                             |
| F-02 | 据え置き                  | `APP_DATA_DIR` 移行は既存データの移行手順が必要。                                                                                                                                                                                          |
| F-03 | 据え置き（U13）           | ディスクキャッシュ eviction の意味決定（LRU か mtime か）は仕様判断。                                                                                                                                                                      |
| F-04 | 該当なし                  | リレーショナル DB は存在しない。                                                                                                                                                                                                           |
| G-01 | 据え置き（U6）            | C-02 と同一。認可述語の単一実装化。                                                                                                                                                                                                        |
| G-02 | 仕様どおり                | `/api/csp-report` は有界。変更禁止。                                                                                                                                                                                                       |
| G-03 | 仕様どおり                | CSP は厳格。`unsafe-inline` を入れない。                                                                                                                                                                                                   |
| G-04 | 仕様どおり                | `secrets.compare_digest` を維持。                                                                                                                                                                                                          |
| G-05 | 据え置き（U14）           | `MNS_ALLOW_CLIENT_API_KEY` の本番ガードは未確認。CI 設定の実読が必要。                                                                                                                                                                     |
| G-06 | 仕様どおり                | `/api/shutdown` の別系統トークンは intended。                                                                                                                                                                                              |
| H-01 | 据え置き（U9）            | `ui.js` / `index.css` の分割。                                                                                                                                                                                                             |
| H-02 | 据え置き（U10/U14）       | リアルタイム経路の実使用関係は未確定。                                                                                                                                                                                                     |
| H-03 | 据え置き（U9）            | `api.js` の分割。                                                                                                                                                                                                                          |
| H-04 | 仕様どおり                | TS 型安全と生成物検証は良好。変更禁止。                                                                                                                                                                                                    |
| H-05 | 据え置き（U10）           | `npm run compile` の追跡ファイル書換はビルド体制の変更。                                                                                                                                                                                   |
| H-06 | 仕様どおり                | XSS 対策は良好。                                                                                                                                                                                                                           |
| H-07 | 据え置き（U14）           | `JSON.parse` 19 箇所の try 有無は未監査。                                                                                                                                                                                                  |
| H-08 | 仕様どおり                | `setInterval` のクリーンアップは intended。                                                                                                                                                                                                |
| I-01 | 据え置き（U9）            | `native_host.py` の分割。                                                                                                                                                                                                                  |
| I-02 | 据え置き（U14）           | 起動ロック残留は未精読。多重起動自体は §8 で欠陥なし確認済み。                                                                                                                                                                             |
| I-03 | 据え置き（U14）           | 拡張のトークン保存先・接続元制限は未確認。                                                                                                                                                                                                 |
| I-04 | 仕様どおり                | ネイティブログは追跡外。                                                                                                                                                                                                                   |
| J-01 | **修正済み**              | `scratch/debug_announce.py` を worktree から削除（`git status` は `D`）。                                                                                                                                                                  |
| J-02 | 一部修正                  | portfolio/config は本番 schema を検証。他エンドポイントの schema テストは未使用モデルのまま。                                                                                                                                              |
| J-03 | 据え置き（U2）            | `routes.api_stocks` の 257 patch は U2 の依存注入と同時に移行。                                                                                                                                                                            |
| J-04 | 仕様どおり                | CI は充実。水準を落とさない。                                                                                                                                                                                                              |
| J-05 | 据え置き（Low）           | flake8 の構文チェックは ruff と重複するが、CI 互換のため維持。統合は独立のツール変更。                                                                                                                                                     |
| J-06 | 据え置き（U14）           | pyrefly の suppressed 20 件の内訳は未確認。                                                                                                                                                                                                |
| J-07 | **修正済み**              | `.review-out/` は worktree に存在しない。                                                                                                                                                                                                  |
| J-08 | **修正済み**              | `.gitignore` に `scratch/` を追加。                                                                                                                                                                                                        |
| K-01 | 据え置き（Low）           | デッドロックではないが、`self.lock` 保持中の `_lifecycle_lock` 最大 1 秒待ちは残存。U15 の Low 項目。                                                                                                                                      |
| K-02 | **修正済み**              | 一次ソースの一時停止を `use_primary=False` に分離し、フォールバックは継続。`test_paused_primary_still_uses_fallback`、`test_engine_pts_fallback_runs_when_primary_paused`。                                                                |
| K-03 | 一部修正                  | 銘柄間 `sleep` を `_interruptible_sleep` に置換。`test_pts_worker_stops_promptly_during_inter_symbol_sleep`。in-flight HTTP fetch の完了待ちは残る。                                                                                       |
| K-04 | **修正済み**              | `except (RuntimeError, Exception)` を 2 箇所とも `except Exception` に。grep で残存 0。                                                                                                                                                    |
| K-05 | **誤検知**                | dash 変種は `"." in bare_sym` の内側のみ。`test_tv_quote_without_dot_dispatches_once_per_distinct_symbol` と `test_tv_quote_with_dot_keeps_all_three_forms` でロック。                                                                     |
| K-06 | 据え置き（U14）           | `_scraper_market_state` の永久キャッシュはテスト/再初期化経路のみ影響。                                                                                                                                                                    |
| K-07 | **修正済み（U16）**       | `getContext` / `startBackend` の先行取得を削除。add / stop が必要時に 1 回だけ取得し、401/403 時のみ再取得。`tests/test_extension_token_lifecycle.py` の 6 ケース（実予算モック・add_ext 403 検証を含む）で固定。予算値は不変。            |
| K-08 | **修正済み（U16）**       | `fetchWithTimeout` を拡張・フロントの全生 fetch（add/indices/poll/csrfFetch 等）に適用。`tests/test_fetch_timeouts_2026_10_21.py` の 3 ケースで固定。旧コードは同ハーネスで watchdog 失敗。                                                |
| K-09 | 据え置き（U14）           | `_build_mistral_cache_key` の非 JSON 値到達性は未確認。                                                                                                                                                                                    |
| K-10 | 据え置き（U14）           | OpenAI ストリームの例外分類は未精読。                                                                                                                                                                                                      |
| K-11 | 仕様どおり                | ネイティブホストの 4 段認証は堅牢。変更禁止。                                                                                                                                                                                              |

---

## 10. 付録 A — 全ソースファイル目録（テスト除く）

本節は `git ls-files` と `wc -l` の実測値。**「一覧に載っている＝精読済み」ではない**。深さは §2 の表を参照。

| ファイル                                             | 行数 |
| ---------------------------------------------------- | ---: |
| `ai_state.py`                                        |  335 |
| `app.py`                                             |  919 |
| `app_bg.py`                                          |  216 |
| `app_state.py`                                       |  434 |
| `bg/__init__.py`                                     |  153 |
| `bg/common.py`                                       |  285 |
| `bg/leader_election.py`                              |  250 |
| `bg/sse_interpolator.py`                             |  242 |
| `bg/sync_worker.py`                                  | 1377 |
| `config_store.py`                                    | 1248 |
| `config_utils.py`                                    |  312 |
| `constants.py`                                       |  507 |
| `credential_manager.py`                              |  801 |
| `crypto_utils.py`                                    |  582 |
| `error_codes.py`                                     |  134 |
| `error_handlers.py`                                  |  191 |
| `execution_state.py`                                 |  118 |
| `gunicorn.conf.py`                                   |  120 |
| `logging_config.py`                                  |  352 |
| `market_state.py`                                    |  530 |
| `messaging.py`                                       |  364 |
| `mistral_compat.py`                                  |  262 |
| `native_host/__init__.py`                            |    0 |
| `native_host/native_host.py`                         | 1578 |
| `native_host/start_backend.py`                       |  429 |
| `route_helpers.py`                                   |  977 |
| `routes/__init__.py`                                 |    0 |
| `routes/api_analysis.py`                             | 2238 |
| `routes/api_stocks.py`                               |  249 |
| `routes/api_system.py`                               | 1411 |
| `routes/pages.py`                                    |  114 |
| `routes/stocks/__init__.py`                          |  124 |
| `routes/stocks/ai_portfolio.py`                      |  933 |
| `routes/stocks/common.py`                            |  329 |
| `routes/stocks/portfolio.py`                         |  287 |
| `routes/stocks/quotes.py`                            |  508 |
| `routes/stocks/stream.py`                            |  417 |
| `routes/stocks/views.py`                             |  796 |
| `schemas/__init__.py`                                |   62 |
| `schemas/ai_portfolio.py`                            |  161 |
| `schemas/analysis.py`                                |  138 |
| `schemas/config.py`                                  |   39 |
| `schemas/stocks.py`                                  |  243 |
| `scratch/debug_announce.py`                          |   15 |
| `sectors.py`                                         |  264 |
| `security_config.py`                                 |  195 |
| `services/__init__.py`                               |    0 |
| `services/ai_portfolio_service.py`                   |  927 |
| `services/ai_service.py`                             | 2258 |
| `services/ai_tools.py`                               |  611 |
| `services/embeddings_service.py`                     |  297 |
| `services/fallback_provider.py`                      | 1078 |
| `services/market_data_service.py`                    |  325 |
| `services/news_formatter.py`                         |  225 |
| `services/news_service.py`                           |  423 |
| `services/openai_service.py`                         | 1214 |
| `services/preview_responses.py`                      |  163 |
| `services/realtime/__init__.py`                      |   94 |
| `services/realtime/engine.py`                        |  639 |
| `services/realtime/scrapers.py`                      | 1232 |
| `services/realtime/tv_client.py`                     |  497 |
| `services/realtime/utils.py`                         |  438 |
| `services/realtime_engine.py`                        |  107 |
| `services/search/__init__.py`                        |    1 |
| `services/search/ddgs.py`                            |  476 |
| `services/search/langsearch.py`                      |  582 |
| `services/search/tavily.py`                          |  203 |
| `services/search_service.py`                         |  478 |
| `services/stock_provider.py`                         | 1814 |
| `services/stock_service.py`                          |  398 |
| `session_manager.py`                                 |  920 |
| `shutdown_manager.py`                                |  294 |
| `trend_sources.py`                                   | 1003 |
| `utils/__init__.py`                                  |    0 |
| `utils/caching.py`                                   |  320 |
| `utils/chat_history.py`                              |  677 |
| `utils/disk_cache.py`                                |  732 |
| `utils/env_helpers.py`                               |  138 |
| `utils/formatting.py`                                |   63 |
| `utils/http_utils.py`                                |   84 |
| `utils/market_utils.py`                              |  402 |
| `utils/networking.py`                                |  584 |
| `utils/normalization.py`                             |  212 |
| `utils/stock_payload.py`                             | 1229 |
| `utils/storage.py`                                   |  684 |
| `utils/text_utils.py`                                |  193 |
| `utils/threading.py`                                 |  152 |
| `utils/tradingview_mapper.py`                        |  587 |
| `utils/validators.py`                                | 1468 |
| `utils/worker_validation.py`                         |  284 |
| `wsgi.py`                                            |   96 |
| `chrome_extension/background.js`                     |  825 |
| `chrome_extension/content.js`                        |  453 |
| `chrome_extension/popup.css`                         |  464 |
| `chrome_extension/popup.html`                        |  170 |
| `chrome_extension/popup.js`                          |  697 |
| `eslint.config.mjs`                                  |  230 |
| `scripts/verify_generated_frontend.mjs`              |   47 |
| `static/css/colors.css`                              |  505 |
| `static/css/experimental-orbit.css`                  | 1179 |
| `static/css/heatmap.css`                             |  782 |
| `static/css/index.css`                               | 5543 |
| `static/css/screener.css`                            |  471 |
| `static/css/settings.css`                            | 1002 |
| `static/css/setup.css`                               |  485 |
| `static/js/ai_portfolio.js`                          | 1586 |
| `static/js/api.js`                                   | 2684 |
| `static/js/api_client.js`                            |  671 |
| `static/js/api_client.ts`                            |  841 |
| `static/js/chart.js`                                 | 1971 |
| `static/js/config_init.js`                           |   23 |
| `static/js/experimental/accessibility-controller.js` |  487 |
| `static/js/experimental/ai-dive-controller.js`       |  611 |
| `static/js/experimental/constellation-controller.js` |  453 |
| `static/js/experimental/data-adapter.js`             |  310 |
| `static/js/experimental/gesture-controller.js`       |  195 |
| `static/js/experimental/orbit-entry.js`              |  565 |
| `static/js/experimental/orbit-renderer.js`           |  891 |
| `static/js/experimental/orbit-state.js`              |  278 |
| `static/js/experimental/temporal-controller.js`      |  542 |
| `static/js/heatmap.js`                               | 1220 |
| `static/js/index_main.js`                            | 1026 |
| `static/js/realtime_client.js`                       |  273 |
| `static/js/screener.js`                              |  705 |
| `static/js/settings.js`                              | 1443 |
| `static/js/setup.js`                                 |  529 |
| `static/js/state.js`                                 |  741 |
| `static/js/tradingview_manager.js`                   |  833 |
| `static/js/ui.js`                                    | 3793 |
| `static/js/utils.js`                                 |  772 |
| `templates/base.html`                                |   58 |
| `templates/experimental_orbit.html`                  |  542 |
| `templates/heatmap.html`                             |  203 |
| `templates/index.html`                               | 1058 |
| `templates/screener.html`                            |  278 |
| `templates/settings.html`                            |  646 |
| `templates/setup.html`                               |  282 |

テストファイル 228 件（`tests/`、60,175 行）は個別列挙を省略。`pytest` 実行結果は §1。
