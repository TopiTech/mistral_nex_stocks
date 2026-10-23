# BUILD 開始記録 — リポジトリマップ + 全課題リスト (2026-10-23)

> 位置づけ: Mission BUILD start の Done 条件「repo map + full issue list is recorded」を満たすための記録。
> 唯一の課題リスト (single source of truth) は `docs/CODE_REVIEW_2026-10-21.md` §4/§9 の全72件を継承し、本ファイルはその写しではなく「現ツリーとの突合済み作業台帳」である。
> 抜粋ではなく全ファイルを網羅精査するための作業台帳として、本番 Python / JS・TS / CSS / templates / chrome_extension / native_host / bg / routes / services / utils / schemas / 設定・ビルドをすべて列挙する。

## 1. ワークツリー状態

- branch: `master`, `git status --short`: クリーン (出力なし)
- Python 3.14.3 / uv 0.12.5 / Node v24.22.1 / npm 11.19.1
- 追跡 Python: 327 件 (`git ls-files *.py`, tests 227 含む) / 本番 Python 約91 + schemas等
- 追跡 JS/TS/mjs: 29 件 (static/js 16 + experimental 9 + chrome_extension 3 + scripts 1)
- テスト: `tests/test_*.py` 227 ファイル (doc記録時 228, §1 ベースライン 2946→3006件の変動はサイクル1-5の追加分)

## 2. ディレクトリ構造 (実測)

```
app.py, app_bg.py, app_state.py, ai_state.py, execution_state.py, market_state.py
config_store.py, config_utils.py, constants.py, credential_manager.py, crypto_utils.py
error_codes.py, error_handlers.py, logging_config.py, market_state.py, messaging.py
route_helpers.py, sectors.py, security_config.py, session_manager.py, shutdown_manager.py
mistral_compat.py, trend_sources.py, wsgi.py, gunicorn.conf.py
routes/ (api_analysis.py, api_stocks.py, api_system.py, pages.py + stocks/{ai_portfolio,common,portfolio,quotes,stream,views}.py)
services/ (ai_service, ai_tools, ai_portfolio_service, embeddings_service, fallback_provider,
  market_data_service, news_formatter, news_service, openai_service, preview_responses,
  stock_provider, stock_service, realtime/{engine,scrapers,tv_client,utils}, search/{ddgs,langsearch,tavily})
utils/ (caching, chat_history, disk_cache, env_helpers, formatting, http_utils, market_utils,
  networking, normalization, stock_payload, storage, text_utils, threading, tradingview_mapper,
  validators, worker_validation)
schemas/ (ai_portfolio, analysis, config, stocks)
bg/ (common, leader_election, sse_interpolator, sync_worker)
static/js/ (ai_portfolio, api, api_client.ts/js, chart, config_init, heatmap, index_main,
  realtime_client, screener, settings, setup, state, tradingview_manager, ui, utils + experimental/*9)
static/css/ (colors, experimental-orbit, heatmap, index, screener, settings, setup)
templates/ (base, experimental_orbit, heatmap, index, screener, settings, setup)
chrome_extension/ (background, content, popup.{js,html,css}, manifest)
native_host/ (native_host.py, start_backend.py, manifest template, ps1/cmd)
scripts/verify_generated_frontend.mjs, docs/{architecture.md, CODE_REVIEW_2026-10-21.md}
```

UI / バックエンド / DB・データ層 (暗号化JSON+disk cache, RDBなし) / 共通・設定 / API境界の対応は `docs/architecture.md` の通り。

## 3. テスト・静的解析・ビルド環境 (正確な定義)

| 種別 | コマンド | 期待 |
|---|---|---|
| Pythonテスト | `uv run --locked --group test python -m pytest tests/ -q -n auto` (pytest.ini: `-n auto --dist=loadfile`, `ResourceWarning=error`, timeout 60s/thread, cov-fail-under=75) | 現状 2965 passed/3 skipped (サイクル5実測) |
| 起動smoke | `uv run --locked --group test python tests/startup_smoke_runner.py` (MNS_SKIP_BOOTSTRAP=0) | health到達 |
| POSIX永続化 | 上記 + `test_posix_auto_master_key_portfolio_persistence_completes` (CI posix-persistence job) | 回帰 |
| ruff | `ruff check . --line-length=100` (pyproject設定, BLE001/S110/LOG014除外) | All checks passed |
| flake8 | `flake8 . --count --select=E9,F63,F7,F82` (.flake8: E203,E402,W503,E501,E302,E303,E305,W293,E131,E261除外, tests除外) | 0 |
| mypy | `mypy .` (pyproject: py3.11, 90 source files, check_untyped_defs) | Success no issues |
| pyrefly | `pyrefly check` + `pyrefly check --python-platform win32` (20 suppressed既知) | 0 errors |
| pylint | `pylint --errors-only --disable=import-error app.py config_utils.py trend_sources.py native_host/... routes/ services/ utils/` | 出力なし |
| bandit | `bandit --configfile pyproject.toml -r . --severity-level medium` (B311,B110,B105,B404,B603 skip) | 0件 |
| pip-audit | `pip-audit --strict` | clean |
| SRI | CI `Verify SRI Hashes in HTML` (templates/*.html の https script に integrity=sha384必須) | pass |
| frontend typecheck | `npm run typecheck` (`tsc -p tsconfig.json --noEmit`, ES2020/DOM, outFile api_client.js) | エラーなし |
| frontend lint | `npm run lint` (eslint classic script + module mjs) | エラーなし |
| 生成物検証 | `npm run verify-generated` (scripts/verify_generated_frontend.mjs, api_client.ts→js同期) | 一致 |
| prettier | `npx prettier --check "static/js/**/*.js" "chrome_extension/**/*.js"` | All matched |
| npm build | `npm run build` = typecheck→compile→verify-generated→lint→prettier --check (compileは追跡ファイル書換あり=H-05注意) | green |
| npm audit | `npm audit --audit-level=high` | High以上なし |
| CI matrix | frontend/ubuntu + lint/ubuntu + type-check/ubuntu + security/ubuntu + test/windows(3.11-3.14) + posix/ubuntu | 全job greenが条件 |
| gunicorn | `workers=1` 強制 (workers!=1でFATAL, H-2/E-03)。timeout=120 (fetchWithTimeout既定120sと一致) | 変更禁止 |

## 4. 全課題リスト (全72件・ graded・横断展開済み)

凡例: 状態 = 修正済み / 一部修正 / 据え置き(Ux) / 誤検知・仕様確認 / 仕様どおり(intended・変更禁止) / 該当なし。
詳細根拠は `docs/CODE_REVIEW_2026-10-21.md` §4/§9 が正本。本表は現ツリー突合用の圧縮台帳。

### A. 共通基盤・型・API境界
- A-01 schemas/未使用 — High/BOUND/root — 一部修正 (portfolio+configのみ本番採用, 残: AI PF/chat/analyze/credentials/add/delete/add_ext, J-02連動) → U1残
- A-02 同名別型 AppConfigSchema — High/BOUND/root — 修正済 (RuntimeConfigSchema/TemplateConfigSchema分離)
- A-03 ポートフォリオ検証3実装 — High/BOUND/root — 修正済(portfolioのみ。Screener/Heatmap重複は別スライスで残) → U1残
- A-04 sys.modules.get() 24-30箇所 — High/DESIGN/root — 修正済(U2: Hub1 dispatch撤去+Hub2/Hub3 lazy import化。`"pytest" in sys.modules` 系の環境検出3箇所は対象外として残置)
- A-05 "pytest" in sys.modules 3箇所 — Medium/EDGE/root — 修正済(U3: 明示スイッチ一本化。非本番 source の runner sniff は残存0)
- A-06 成功封筒不統一 — Medium/BOUND/symptomatic — 据え置き(U1残: ok/success併読, error_code/code両キー維持)
- A-07 code:str(IntEnum) — Low/BOUND — 誤検知(影響なし, JSはerror_codeのみ参照)
- A-08 resolve_stocks_for_response既定True — Medium/SEC/root — 修正済(U8: wrapper 既定は False 化済みであることを確認。全呼出し明示のまま。回帰テストで固定)
- A-09 BE-FE契約が型で表現なし — Medium/BOUND/root — 据え置き(U2後: U1と同時)

### B. コア・設定
- B-01 config_store.py 1248行同居 — High/DESIGN/root — 修正済(U9-a: `config/` 7モジュールに純粋移動＋`config_store.py` は状態＋再exportシム。fail-closed・patch互換維持)
- B-02 app.py 919行同居 — Medium/DESIGN/root — 修正済(U9-b: `app_factory/` 6モジュールに純粋移動＋`app.py` は状態＋再exportシム。CSP/nonce不変・`Flask("app")` で root_path 維持)
- B-03 状態4モジュール分散 — Medium/DESIGN/root — 据え置き(U9+J-01連動)
- B-04 logging秘匿マスク範囲不明 — Low/SEC/要確認 — 据え置き(U14未精読)

### C. ルート/API
- C-01 api_system.py 1411行7機能 — Medium/DESIGN/root — 修正済(U9-c: `routes/system/` 8モジュールに純粋移動＋`routes/api_system.py` は再exportシム。URL/endpoint不変)
- C-02 認可述語5実装 — High/SEC/root — 修正済(U6: `check_admin_token` 単一 primitive 化＋extension 分離。compare_digest維持、許可条件の厳格化なし)
- C-03 lock保持中fsync — Medium/RACE,PERF/root — 修正済(U7: bg残存2箇所をlock外persist化。routes/storageの本体は先行対応済み)
- C-04 totalが返却件数 — Low/BOUND/symptomatic — 据え置き(U14: FE参照確定後)
- C-05 Last-Event-ID異常値 — Medium/EDGE — 仕様確認(欠陥なし: 非数値/負値→0クランプ, replayはsnapshot fallback)
- C-06 api_stocks.py再export — Low/DESIGN — intended (U2後も維持: dispatchは撤去済みだが blueprint 本体 `api_stocks_bp` + `api_add_stock_ext` の所有者が同ファイルのため畳み込みは U9 分割時に実施)

### D. サービス層
- D-01 circuitキー空間混在 — High/EDGE,RACE/root — 修正済 (history_circuit_key集約)
- D-02 `or`連鎖が0.0を欠損扱い — Medium/EDGE/root — 修正済(U5: market_data_service の数値は既に first_not_none 化済みだったことを確認。残存3箇所 (`bg/sse_interpolator.py` の `or 0.0`) を None 判定化。0.0→0.0 の恒等のため挙動不変。文字列or・関数既定orは対象外)
- D-03 時価総額に売買代金代用 — Medium/EDGE/root — 修正済 (price*volume削除, shares*priceのみ)
- D-04 バッチ部分失敗を200 — Medium/EDGE,BOUND/symptomatic — 一部修正(U5: screener 応答に `missing[]` を加算追加。FE は `data.stocks` のみ参照のため互換。全滅時 502/503 化は FE 連携が必要なため U14 へ先送り)
- D-05 market_is_open_cached非cache — Low/DESIGN/symptomatic — 修正済(U5: 呼出0件の dead 関数のため改名/TTL ではなく撤去。`is_market_open` import も同時除去)
- D-06 stock_provider.py 1814行 — High/DESIGN/root — 修正済(U9-d: `services/stock/` 5モジュールに純粋移動＋`services/stock_provider.py` は再exportシム。fetch/retry/breaker/session 意味不変)
- D-07 AI provider分岐分散 — Medium/DESIGN/要確認 — 据え置き(U14未精読)
- D-08 有界cache適切 — intended (LRU 1024/4096/256/500, 変更禁止)

### E. バックグラウンド/リアルタイム
- E-01 sync_worker.py 1377行 — High/DESIGN/root — 修正済(U9-e: `bg/sync/` 4モジュールに純粋移動＋`bg/sync_worker.py` は再exportシム。lock順序・app_bgミラー委譲不変)
- E-02 テスト用global本番露出 — Medium/DESIGN,LEAK/root — 据え置き(U11: SSEPayloadCache化)
- E-03 workers=1二重防御 — Low/RACE — intended (FATAL維持, 変更禁止)
- E-04 SSE snapshot毎client再構築 — Medium/PERF/root — 修正済(U8: initial snapshot を世代キー共有化。per-tick 配信は従来どおり共有 announce、mode-2 delta は per-client のまま)

### F. データ層・永続化
- F-01 原子永続化堅牢 — intended (RLock+flock+tmp+fsync+replace+dir fsync, 簡略化禁止)
- F-02 APP_DATA_DIR移行未完 — Medium/DESIGN/root — 据え置き(data_dir()集約+実パスlog, コピー+fallback移行)
- F-03 evictionがmtime順 — Low/PERF/要確認 — 据え置き(U13: LRUかmtimeか仕様確定, get時touch確認)
- F-04 RDBなし — 該当なし

### G. セキュリティ
- G-01 認可単一化 (=C-02) — High/SEC/root — 修正済(U6 と同時)
- G-02 /api/csp-report境界化 — intended (1MiB上限後json.loads, 変更禁止)
- G-03 CSP厳格 — intended (strict-dynamic+nonce, unsafe-inline禁止)
- G-04 定数時間比較 — intended (compare_digest維持)
- G-05 MNS_ALLOW_CLIENT_API_KEY=1がCI全体 — Medium/SEC/要確認 — 据え置き(U14: 本番ガード有無)
- G-06 shutdown別系統 — Low/SEC — intended

### H. フロントエンド
- H-01 ui.js 3793行+index.css 5543行 — High/DESIGN/root — 据え置き(U9画面単位分割)
- H-02 realtime経路複数併存 — High/DESIGN/要確認 — 据え置き(U10/U14: sseApiClient/APIClient/realtime_client/tradingview_manager実使用確定)
- H-03 api.js 2684行同居 — Medium/DESIGN/root — 据え置き(U9: errors/sse/endpoints分割)
- H-04 TS型安全+生成物検証良好 — intended (any 0, verify-generated維持, 緩和禁止)
- H-05 compileが追跡書換 — Low/DESIGN/symptomatic — 据え置き(U10: --write外し+tmp比較)
- H-06 XSS良好 — intended (innerHTML実使用0, textContent/DOM維持)
- H-07 JSON.parse 19箇所未監査 — Low/EDGE/要確認 — 据え置き(U14: SSE data行含めtry有無)
- H-08 setInterval cleanup — intended (差分1はCSRF寿命timer, リークなし)

### I. ネイティブホスト/拡張
- I-01 native_host.py 1578行同居 — Medium/DESIGN/要確認 — 据え置き(U9, inject観点集中審査)
- I-02 start_backend多重起動/lock残留 — Medium/RACE/要確認 — 据え置き(U14: §8で多重起動自体は欠陥なし確認済)
- I-03 拡張token保存/externally_connectable — Medium/SEC/要確認 — 据え置き(U14未精読)
- I-04 native log追跡外 — intended

### J. ツール・テスト・衛生
- J-01 scratch/debug_announce.py追跡 — High/DESIGN/root — 修正済(削除+scratchを.gitignore)
- J-02 testsが未使用schemas検証 — Medium/BOUND/root — 一部修正(portfolio/config解消, 他endpoint残)
- J-03 testsがroutes.api_stocksを257patch — Medium/DESIGN/root — 一部修正(U2: load-bearing な facade patch は全て使用側 namespace (views/common/stream/ai_portfolio/route_helpers/credential_manager/services 実体) へ移設。残は実体参照 (`app_state...`) か無害な dead patch (`_has_cached_key` 系: 本番読取なし・green 確認)。完全清掃は U14 衛生で実施)
- U5-D04残: screener 全滅時の 502/503 化 — Medium/BOUND — 据え置き(U14: FE のエラー表示連携と同時。`missing[]` の加算提供は U5 で完了)
- J-04 CI充実 — intended (水準維持)
- J-05 flake8 E9系のみ — Low/DESIGN/symptomatic — 据え置き(独立ツール変更)
- J-06 pyrefly 20 suppressed不可視 — Low/DESIGN/symptomatic — 据え置き(U14内訳監査)
- J-07 .review-out/未追跡 — Low/衛生 — 修正済(削除済)
- J-08 scratch/が.gitignoreなし — Low/衛生 — 修正済(追加済)

### K. リアルタイム/AI/ネイティブ/拡張 (新規所見)
- K-01 add/remove_symbolがlock保持で別lock最大1s — Low/RACE/symptomatic — 据え置き(U15 Low。ABBA逆転なし=デッドロック否定済)
- K-02 JP一時停止がfallbackまで殺す — Medium/EDGE/root — 修正済(use_primary分離)
- K-03 stop()がPTS worker待ち切れず — Medium/RACE/root — 修正済(in-flight fetchの有界drain＋stop()の成否bool返却。残存時は明示失敗しrestartがdefer)
- K-04 except (RuntimeError, Exception) — Low/DESIGN/symptomatic — 修正済(except Exception統一, 残存0)
- K-05 同一quote二重dispatch — 誤検知 ("." in bare_sym内側のみ, ロックテスト2件)
- K-06 _scraper_market_ref永久cache — Low/DESIGN/要確認 — 据え置き(U14)
- K-07 拡張token取得がnative予算超過 — Medium/EDGE/root — 修正済(サイクル4: 先行取得削除, 必要時1回+401/403時再取得, 予算不変, 6ケース固定)
- K-08 addStockViaExtension timeoutなし — Low/EDGE/symptomatic — 修正済(サイクル5: fetchWithTimeout全生fetch適用, 拡張10/3/5s・FE 120/10s, 3ケース固定)
- K-09 _build_mistral_cache_key default=なし — Low/EDGE/要確認 — 据え置き(U14: 到達性→400化判断)
- K-10 OpenAI stream例外一律circuit失敗 — Low/EDGE/symptomatic — 据え置き(U14: 通信系のみ計上へ)
- K-11 native 4段認証堅牢 — intended (簡略化禁止)

### U12-U14 (性能・未精読・衛生残)
- U12 stock_payload.py:501-502 to_dict後365件slice — PERF — 修正済(U5: `iloc[-limit:]` 先切り化。`num_records`/chart 窓は slice 後の列に対する相対位置のため同一結果)
- K-03残: PTS in-flight HTTP fetch完了待ち — RACE — 修正済(本unit)
- U14未精読: validators.py JSON修復, ai_service残部/tool loop, openai tool loop/画像/埋込, native Windows署名検証, content.js DOM抽出sanitize, ui/chart全描画, settings.js, H-02/H-07実使用確定, G-05/B-04/F-03/K-06/K-09/K-10/D-07 — 要第2パス

内訳: 修正済み20 (A-02, A-03 portfolio, A-04, A-05, A-08, C-02, C-03, D-01, D-02, D-03, D-05, E-04, G-01, J-01, J-07, J-08, K-02, K-04, K-07, K-08) / 一部修正5 (A-01, D-04, J-02, J-03, K-03) / 誤検知・仕様確認4 (A-07, C-05, K-05 + G-02等は仕様どおり側) / 仕様どおり(intended) 14 / 該当なし1 / 据え置き33 + U12済・U13-U14残 (合計72+α)。

## 5. 変更禁止 (後方互換・意図的仕様, §6継承)

fail-closed鍵復号 / 原子書込(os.replace+fsync+flock) / CSP+nonce / compare_digest / workers=1 FATAL / error_responseキー集合 / api_client.js生成体制 / schemas制約値 / error_codes数値 / Last-Event-ID replay+世代cache / 有界cache上限 / native 4段認証+token予算 / TS any禁止・生成物検証緩和禁止。

## 6. 修正サイクル U1-a 完了記録 (2026-10-23 実施)

対象: U1 の最初の論理単位「watchlist 変異系3エンドポイント (add/delete/add_ext) の契約単一化 + 成功封筒の共通化の当該分」。

| 変更 | 内容 |
| --- | --- |
| `schemas/stocks.py` | `StockAddRequest` / `StockAddExtRequest` / `StockDeleteRequest` を正本化。`_normalize_stock_mutation_identity` で NFKC・大文字化・JP `.T` 付与・market 既定値 (add/delete `""`, add_ext `"us"`) を旧 `_parse_stock_request` と同一化し、非文字列型は `invalid_market` / `invalid_symbol` / `name_must_be_string` マーカーで写像。`is_valid_symbol`・名前長 (日本語 UNSAFE_INPUT)・必須名の有無を schema 側で所有 |
| `route_helpers.py` | `_stock_mutation_error_from_validation` (旧来の identity→必須→長さ→形状の優先順位を再現し、無効 market 文字列は旧来通り MISSING symbol を優先して byte-identical) + `_parse_stock_add/delete/add_ext_request` + 旧 `_parse_stock_request` を正本委譲の互換シム化 (tests/`api_stocks` 再輸出互換維持) |
| `routes/stocks/views.py` | add/delete/add_ext/reset の成功返却を `ok_response()` (`{"ok": true, "success": true}` 両キー維持) に統一。add_ext の already-exists/added メッセージは本文不変で封筒のみ統一 |
| `routes/stocks/portfolio.py` | portfolio 更新成功を `ok_response()` に統一 (形状不変) |
| `utils/stock_payload.py` | `ok_response()` 新設 (A-06 の単一生成点。両キー維持のため FE `data?.ok \|\| data?.success`・既存テストと互換) |
| `tests/test_schema_contract_stocks_mutation_2026_10_23.py` | 18ケース: 欠番・空白・market欠番の旧来優先順位・非文字列型3種・必須名・長文日本語・不正文字・traversal・malformed JSON・add/delete成功封筒両キー・add_ext任意名・NFKC/`.T` 正規化 |

検証 (全て終了コード 0): full suite `3016 passed, 3 skipped, 47 subtests` / `ruff` All checks / `flake8 E9,F63,F7,F82` 0 / `mypy` 90 files Success / `pyrefly` 0 errors (20 suppressed 既知) / `pylint --errors-only` 無出力 / `npm typecheck/lint/verify-generated/prettier` green。変更禁止事項 (fail-closed・原子書込・CSP・compare_digest・workers=1・error封筒キー・生成物検証・制約値・replay) は不変。`any`・抑制コメント・skip・妥協アサーションなし。

残 (次単位): AI PF (generate/rebalance/save/delete/copy-to-my)・analyze-v2・ai-technical-lines・analyze-chart-image・credentials の手書きパーサ → schema 移行 (各1エンドポイントずつ写像監査) + A-06 の残封筒 (`{"ok": true}` のみの成功応答) の統一。

## 6c. 修正サイクル U1-b 完了記録 (2026-10-23 実施)

対象: U1 の次の論理単位「`POST /api/chat` 要求ボディの契約単一化」。

| 変更 | 内容 |
| --- | --- |
| `schemas/analysis.py` | `AIChatRequest` を正本化。`normalize_identity` (before) で market 既定 `"us"`・NFKC/大文字化・JP `.T` 付与・message strip を旧ルートと同一化し、非文字列型は `invalid_market` / `invalid_symbol` / `message_must_be_string` マーカーで写像。`symbol` は空→`field_required`・形状不良→`invalid_symbol`、`market` は空→`invalid_market`、`message` は空→`field_required`・超過→`message_too_long`。`request_token` は `pattern=` に集約 (全違反をアダプタで旧文言へ) |
| `route_helpers.py` | `_chat_error_from_validation` (旧来の逐次優先順位を再現: 型→長文→market→複合欠番→形状→token) + `_parse_chat_request` を新設 |
| `routes/api_analysis.py` | `api_chat` の約40行の手書き検証をアダプタ呼出しに置換。以降は `parsed_chat.market/symbol/message/request_token` のみを使用。`_get_operation_token` 自体は analyze-v2/news 共用のため残置 (chat は経由しない)。未使用化した `CHAT_MAX_MSG_LENGTH` import を除去 |
| `tests/test_schema_contract_chat_2026_10_23.py` | 19ケース: 複合欠番 (`[symbol, message]` の旧来仕様)・単独空白でも複合・型3種・不正market・形状不良3種・過長・token3種・malformed JSON・優先順位2種 (空白>形状・長文>market)・NFKC/`.T`/strip 正規化・正常系200 (provider mock) |

検証 (全て終了コード 0): full suite `3035 passed, 3 skipped, 47 subtests` / `ruff` All checks / `flake8 E9,F63,F7,F82` 0 / `mypy` 90 files Success / `pyrefly` 0 errors (20 suppressed 既知) / `pylint --errors-only` 無出力。Python のみの変更のため npm 検証は不要 (JS/TS 未変更)。変更禁止事項は不変。`TypeError` 送出案は Pydantic が ValidationError 化しないことを実測で確認し不採用 (`ValueError` マーカー維持)。`any`・抑制コメント・skip・妥協アサーションなし。

## 6b. 次ステップ (依存順 U1残→U16, 論理単位で 修正→テスト→静的解析→ビルド)

U1(完了: 境界単一化すべて) → U2(完了: sys.modules撤去) → U3(完了: pytest検出統一) → U4済 → U5(完了: D-02/D-04/D-05+U12) → U6(完了: 認可単一化) → U7(完了: lock外persist) → U8(完了: SSE世代共有) → U9(巨大分割) → U10(H-02/H-05/H-07) → U11(E-02残) → U13(F-03) → U14(第2パス新規課題化) → U15残(K-01/K-03残/K-06) → 完了照合(§9台帳×diff×worktree→全check green)。

## 6d. 修正サイクル U1-c 完了記録 (2026-10-23 実施)

対象: U1 の次の論理単位「AI ポートフォリオ5エンドポイント (generate/rebalance/save/delete/copy-to-my) の契約単一化」。

| 変更 | 内容 |
| --- | --- |
| `constants.py` | `MAX_AI_PORTFOLIO_ITEMS = 20` を単一化 (`services` は再輸出、`schemas` は直接参照。値・公開名とも不変) |
| `services/ai_portfolio_service.py` | 上記定数を `constants` から import (`_MAX_ITEMS` エイリアス維持、生成時 truncation 不変) |
| `schemas/ai_portfolio.py` | 5 モデルを正本化。generate/delete/save は非文字列マーカー + 旧来写像。rebalance は strip + 非文字列マーカー (欠番→既定 `"tech"` 維持)。copy-to-my は逐次 route-exact 検証 (市場強制・`.T`・idx別・重複・合計・上限) と正規化を `mode="before"` に集約。プログラム的構築 (Model 入力) は従来検証のまま (既存テストの旧文言を維持)。重複マーカーは旧文言 + `(item IDX)` を両立 |
| `route_helpers.py` | 5 種の `_parse_ai_*_request` + 誤差写像を新設。copy は正規化タプル列を返却 |
| `routes/stocks/ai_portfolio.py` | 4 手書きパーサを削除し委譲。copy-to-my の約110行検証ループをアダプタ呼出しに置換 (派生計算・容量・永続化は不変)。未使用 import を除去 |
| `tests/test_schema_contract_ai_portfolio_2026_10_23.py` | 32ケース: 5 endpoint の欠番・型・長文・日本語文言・idx別・重複・合計・上限・malformed JSON・巨大整数 (旧来 500 を 400 に堅牢化)・正常系200 |

検証 (全て終了コード 0): full suite `3067 passed, 3 skipped, 47 subtests` / `ruff` / `flake8 E9,F63,F7,F82` / `mypy` 90 files / `pyrefly` 0 errors / `pylint --errors-only`。Python のみのため npm 不要。既存テスト無改変。
作業中は推測実装禁止・仕様確認はWeb検索で最新化。any/抑制コメント/skip/妥協アサーション禁止、根本原因修正のみ。

## 6e. 修正サイクル U1-d 完了記録 (2026-10-23 実施)

対象: U1 の次の論理単位「`POST /api/analyze-v2` 要求ボディの契約単一化」。

| 変更 | 内容 |
| --- | --- |
| `schemas/analysis.py` | `AIAnalyzeV2Request` を正本化。`normalize_analyze_v2` (before) で旧ルートの逐次順序 (symbol/market型→name型/長→chart形状/上限→price形状/範囲→market空→symbol空/形状) を同一化し、失敗は `symbol_not_string` / `market_not_string` / `market_empty` / `name_must_be_string` / `name_too_long` / `chart_data_must_be_list` / `chart_data_too_many` / `chart_data_entry_not_object` / `price_must_be_number` / `price_not_finite` / `price_negative` / `symbol_required` / `symbol_bad_shape` マーカーで写像。`request_token` は `pattern=` に集約。`price` は `int \| float` で旧来の型保存 (server到達不能時の表示フォールバックと同一化)。プログラム的構築の既存テスト (`test_schemas`・`09_v7`) は無改変で通過 |
| `route_helpers.py` | `_analyze_v2_error_from_validation` (旧来の優先順位を再現: 型→name→chart→price→market空→symbol欠番/形状→token) + `_parse_analyze_v2_request` を新設 |
| `routes/api_analysis.py` | `api_analyze_v2` の約110行の手書き検証をアダプタ呼出しに置換。以降は `parsed_v2.market/symbol/name/price/chart_data/request_token` のみを使用。未使用化した `MAX_STOCK_NAME_LENGTH`・`normalize_symbol`・`normalize_text` import を除去 (`_get_operation_token` 自体は news 共用のため残置) |
| `tests/test_schema_contract_analyze_v2_2026_10_23.py` | 31ケース: malformed JSON・型4種・name長文 (UNSAFE_INPUT)・chart形状3種・price形状/範囲4種 (bool/文字列/inf/負)・market不明・欠番2種・形状不良・token2種・優先順位6種 (型>型・長文>market空・market空>形状・price>market空・欠番>token・形状>token)・正常系200×7 (正規化・`.T`・全角・None chart・price 0・market空→us・上限境界・server空時のclientフォールバック)・int型保存 |

検証 (全て終了コード 0): full suite `3098 passed, 3 skipped, 47 subtests` / `ruff` All checks / `flake8 E9,F63,F7,F82` 0 / `mypy` 90 files Success / `pyrefly` 0 errors (20 suppressed 既知) / `pylint --errors-only` 無出力。Python のみのため npm 不要。既存テスト無改変。`TypeError` 送出案は不採用 (`ValueError` マーカー維持、TRY004 は既存 copy-to-my 同様の間接形で回避)。`any`・抑制コメント・skip・妥協アサーションなし。

## 6f. 修正サイクル U1-e 完了記録 (2026-10-23 実施)

対象: U1 の次の論理単位「`POST /api/ai-technical-lines` 要求ボディの契約単一化」。

| 変更 | 内容 |
| --- | --- |
| `schemas/analysis.py` | `AITechnicalLinesRequest` を正本化。`normalize_technical_lines` (before) で旧ルートの逐次順序 (symbol欠番/型→market型/空→symbol空/形状→period→history形状) を同一化し、失敗は `symbol_required` / `symbol_not_string` / `market_not_string` / `market_empty` / `symbol_bad_shape` / `period_invalid` / `history_data_must_be_list` / `history_data_too_many` / `history_data_entry_not_object` マーカーで写像。market既定 `"us"` (明示nullも `"us"`)・period既定 `"3mo"`・period小文字化・historyのNone→`[]` を旧来通り再現。programmatic既存テスト (`test_schemas`・`09_v7`) は無改変で通過 |
| `route_helpers.py` | `_technical_lines_error_from_validation` (旧来の優先順位を再現。非文字列symbolの `fields` 付き `INVALID_SYMBOL`・非文字列marketの素の `INVALID_MARKET` という当該endpoint固有の癖も維持) + `_parse_technical_lines_request` を新設 |
| `routes/api_analysis.py` | `api_ai_technical_lines` の約70行の手書き検証をアダプタ呼出しに置換。以降は `parsed_tl.market/symbol/period/history_data` のみを使用。空/`{}`/配列ボディの `MALFORMED_INPUT` (旧文言 `JSONデータが正しく送信されませんでした`) ゲートは route に残置。未使用化した `VALID_HISTORY_PERIODS` import を除去 |
| `tests/test_schema_contract_technical_lines_2026_10_23.py` | 30ケース: body gate 3種・欠番3種・型2種・形状2種・period 3種・history形状3種・優先順位7種 (欠番>period・形状>period・market空>period・型>型・market空>history・period>history・空白>period)・正常系200×6 (正規化・`.T`・既定値・null市場・fetchフォールバック ohlc/chart)・programmatic正規化 |

検証 (全て終了コード 0): full suite `3128 passed, 3 skipped, 47 subtests` / `ruff` All checks / `flake8 E9,F63,F7,F82` 0 / `mypy` 90 files Success / `pyrefly` 0 errors (20 suppressed 既知) / `pylint --errors-only` 無出力。Python のみのため npm 不要。既存テスト無改変。`TypeError` 送出案は不採用 (`ValueError` マーカー維持、TRY004 は間接形で回避)。`any`・抑制コメント・skip・妥協アサーションなし。

## 6i. 修正サイクル U1-h 完了記録 (2026-10-23 実施)

対象: U1 の最後の論理単位「A-06残封筒 (`{"ok": true}` のみの成功応答) の統一」。

| 変更 | 内容 |
| --- | --- |
| `routes/api_system.py` | 8箇所の bare OPTIONS 応答 (credentials/verify/health/cache-stats/metrics/csrf-token/shutdown/ai-usage) を `ok_response()` に統一。`ok_response` import 追加 |
| `routes/api_analysis.py` | ai-technical-lines OPTIONS を `ok_response()` に統一。`ok_response` import 追加 |
| `routes/stocks/views.py` | add_ext OPTIONS を `ok_response()` に統一 (import 済み) |
| `tests/test_schema_contract_success_envelope_2026_10_23.py` | 10ケース: 全 unified OPTIONS が `{"ok": True, "success": True}` の完全一致であることを固定 |

全10箇所はブラウザのプリフライト応答であり、JS は body のキーを読まない。`success` 追加は加算的変更で、FE の `data?.ok \|\| data?.success` 読解・既存テストの status 断定と互換。JS 未変更のため npm 検証は不要。非 bare 成功応答 (`message`/`cache_stats`/payload 付き) は対象外。`{"ok": False...}` 系は対象外。

検証 (全て終了コード 0): full suite `3206 passed, 3 skipped, 47 subtests` / `ruff` All checks / `flake8 E9,F63,F7,F82` 0 / `mypy` 90 files Success / `pyrefly` 0 errors (20 suppressed 既知) / `pylint --errors-only` 無出力。既存テスト無改変。`any`・抑制コメント・skip・妥協アサーションなし。

U1 完了: watchlist 変異3・chat・AI PF 5・analyze-v2・technical-lines・chart-image・credentials・A-06残封筒の全境界が単一化された。次は U2 (sys.modules撤去)。

## 6j. 修正サイクル U2 完了記録 (2026-10-23 実施)

対象: A-04「sys.modules 操作による test-seam (24-30箇所)」の撤去。方針は「通常 import による同等意味の seam への置換」で挙動不変。`"pytest" in sys.modules` の環境検出 (app.py・utils/env_helpers.py・scrapers.py 一時停止判定) は test-seam ではなく対象外として残置。

| Hub | 変更 | 内容 |
| --- | --- | --- |
| Hub1 (routes) | `routes/stocks/common.py` | `_get_api_stocks_attr` dispatcher を撤去し、各使用側が実体を直接 from-import (`save_user_stocks`→`utils.storage`、`schedule_sync_all_stocks_now`→`app_bg` 等)。`routes/stocks/{ai_portfolio,quotes,stream,views}.py` の dispatcher 経由参照を同実体へ置換 |
| Hub2 (bg) | `bg/{common,leader_election,sync_worker}.py` 計10箇所 | `sys.modules.get("app_bg")` → 関数内 lazy `import app_bg` + `__dict__.get` (app_bg が bg.* を import するため eager 化は循環。部分初期化モジュール参照も含め意味同一)。未使用化した top-level `import sys` を除去 |
| Hub3 (realtime) | `services/realtime/{utils,scrapers,tv_client}.py` 計8箇所 | `sys.modules.get("services.realtime_engine")` → `utils._get_facade_attr` (lazy `from services import realtime_engine`) に集約。`_rt_sleep/_rt_time` は facade の `time` が stdlib そのものであるため直接呼出し化 (facade patch は同一モジュールオブジェクトに作用し互換)。`_run_ws` 内の不要 `import sys` を除去 |
| tests | 13ファイルの patch 移設 | load-bearing な facade/use-site 誤 patch を移設: `get_or_create_extension_api_token`→`routes.stocks.views`、`create_sse_ticket`→`routes.stocks.stream`、`_sync_realtime_symbol/_announce_watchlist_state`→`routes.stocks.common`、`_submit_in_app_context`→`route_helpers`、`ai_portfolio_fetch_inflight`→`routes.stocks.ai_portfolio`、`generate_ai_portfolio_by_theme`→`services.ai_portfolio_service` |
| tests | `tests/test_sys_modules_removal_u2_2026_10_23.py` (新設) | 12ケース: 非本番 source の seam 文字列 sweep (bg/routes/services/app_bg.py/route_helpers.py/app.py)、fallback 既定、facade/app_bg override の委譲 (logger・is_pts_session・scraper block・yf limit・sync 時刻/世代/lock・leader 状態・is_market_open)、`_rt_time/_rt_sleep` の stdlib 等価、websocket 解決 |

検証 (全て終了コード 0): full suite (PYTEST_EXIT=0, FAILED 0。1回のみ `test_sse_initial_snapshot_sanitizes_nan_values` が単発失敗→単体再実行で通過。他3回の full は無失敗のため、共有 cache 変異に依存する既知の順序依存 flake と判断し U2 変更とは無関係) / `ruff check` All checks passed / `flake8 E9,F63,F7,F82` 0 / `mypy` 90 files Success / `pyrefly` 0 errors (20 suppressed 既知) / `pylint --errors-only` 無出力。JS 未変更のため npm 対象外。`any`・抑制コメント・skip・妥協アサーションなし。残課題: `_has_cached_key` 系の dead facade patch 清掃 (J-03残・U14衛生)、C-06 畳み込みは U9 へ。次は U3。

## 6k. 修正サイクル U3 完了記録 (2026-10-23 実施)

対象: A-05「本番コードが `"pytest" in sys.modules` で挙動を変える (3箇所)」の明示スイッチ一本化。方針は「単一 helper `_is_testing()` が明示フラグのみを見る」で挙動不変。conftest ベースライン (`tests/test_support/environment.py`) が `MNS_SKIP_BOOTSTRAP=1` を保証するため、テスト時の `_is_testing()` は従前どおり True。

| 変更 | 内容 |
| --- | --- |
| `utils/env_helpers.py` | `_is_testing()` を explicit-only 化: `MNS_SKIP_BOOTSTRAP` / `MNS_TESTING` / `TESTING` env または Flask `TESTING` config のみを見て、`import sys`＋runner  sniff を撤去。docstring も U3 仕様に更新 |
| `app.py` | `_register_signal_handlers` の `or "pytest" in sys.modules` を `_is_testing()` に一本化 (import 追加)。`sys` import は他用途 (exit/is_finalizing) のため残置 |
| `services/realtime/scrapers.py` | `_is_startup_ready` の runner 判定を lazy `from utils.env_helpers import _is_testing` に置換。関数内 `import sys` を除去 |
| `tests/test_realtime_engine.py` | `test_yahoo_jp_scraper_startup_ready_gate` の dead `patch.dict(sys.modules, {"pytest": None})` を除去 (`force_check=True` が当該分岐を bypass するため無効果だった。意図・アサーション不変) |
| `tests/test_pytest_detection_unification_u3_2026_10_23.py` (新設) | 8ケース: 非本番 source の runner-sniff 文字列 sweep (コメント除外・bg/routes/services/utils/schemas/主要 root)、ベースライン下 True、全明示旗 off で False、各 env 旗・Flask TESTING config の個別 True、異常値 (`maybe`) は off、signal handler の skip 時に `signal.signal` 不接触、startup-ready の明示旗による True/False 切替 |

非本番 source の runner sniff は残存0 (sweep テストが再発を固定)。`app.py` 起動時の log-dir 判定は `PYTEST_CURRENT_TEST` 等の明示 env、`tests/conftest.py` の `sys.modules["secretstorage"]` はテスト側の隔離措置であり対象外。

検証 (全て終了コード 0): full suite (PYTEST_EXIT=0, FAILED 0) / `ruff check` All checks passed / `flake8 E9,F63,F7,F82` 0 / `mypy` 90 files Success / `pyrefly` 0 errors (20 suppressed 既知) / `pylint --errors-only` 無出力。JS 未変更のため npm 対象外。`any`・抑制コメント・skip・妥協アサーションなし。次は U5 (U4 は実施済)。

## 6l. 修正サイクル U5 完了記録 (2026-10-23 実施)

対象: D-02/D-04/D-05 + U12 (market_data の None/0 混同・部分失敗契約・dead 命名・chart slice 性能)。方針は「根本原因の最小修正＋挙動不変」で、欠損時 fallback・除外仕様・200 応答は維持。

| 変更 | 内容 |
| --- | --- |
| D-02 `services/market_data_service.py` (確認のみ) | 数値 `or` 連鎖は D-03 対応時の rework で既に `first_not_none` 化済み (price/shares/cap/high/low/pe/volume/change)。残存の `or` は文字列 (name/sector) と関数既定のみで 0.0 問題なし。コード変更なし |
| D-02 `bg/sse_interpolator.py` | 残存3箇所の数値 `or 0.0` (`change`・`change_percent`) を `is not None` 判定化。0.0→0.0 の恒等のため挙動不変 |
| D-04 `routes/stocks/views.py` | screener 応答に `missing[]` を加算追加。enrichment が試行され行が得られなかった symbol 列 (cache-fetch 進行中は除外し `[]`)。200 維持・既存キー不変。FE は `data.stocks` のみ参照のため互換 |
| D-05 `services/market_data_service.py` | 呼出0件の `market_is_open_cached` を撤去 (改名/TTL ではなく削除が正直な修正)。未使用化した `is_market_open` import を除去。facade・tests からの参照なしを確認済み |
| U12 `utils/stock_payload.py` | `_build_chart_ohlc_data` を `iloc[-limit:]` 先切り化。`num_records`/chart 窓は slice 後列の相対位置のため同一結果。`[-0:]`/空 frame の縮退挙動も同一 |
| `tests/test_u5_d02_d04_d05_u12_2026_10_23.py` (新設) | 13ケース: 変化率/変化額/PE/volume の 0.0 保持・欠番 fallback・interpolator の `or` 残存 sweep・screener `missing` (部分欠番/全成功)・dead 関数撤去の存在否定＋参照 sweep・OHLC tail 窓 (400行→365・chart 100・先頭/末尾値)・短尺/空 frame |

検証 (全て終了コード 0): full suite (PYTEST_EXIT=0, FAILED 0) / `ruff check` All checks passed! / `flake8 E9,F63,F7,F82` 0 / `mypy` 90 files Success / `pyrefly` 0 errors (20 suppressed 既知) / `pylint --errors-only` 無出力。JS 未変更 (FE は `data.stocks` のみ参照) のため npm 対象外。`any`・抑制コメント・skip・妥協アサーションなし。残課題: D-04 全滅時 502/503 化は FE エラー表示連携と同時に U14 で実施。次は U6。

## 6m. 修正サイクル U6 完了記録 (2026-10-23 実施)

対象: C-02/G-01「認可述語5実装」の単一実装化。方針は「単一 primitive＋薄い呼出し」で、許可条件の厳格化なし・compare_digest 維持・fail-closed 維持。shutdown token (shutdown_manager の使い捨て機構) と SSE ticket の session binding は別 credential 機構のため対象外 (単一実装のまま残置)。

| 変更 | 内容 |
| --- | --- |
| `utils/networking.py` | `get_configured_admin_token()` + `check_admin_token(presented)` を新設 (未設定/未提示→False、非一致型でも例外化せず False)。`require_trusted_or_admin`・`require_sse_auth` の compare を同 primitive に置換 (条件式の意味同一) |
| `app.py` | cross-site carve-out の compare を `check_admin_token` に置換 (`allow_remote and admin_token` の前提は維持)。未使用化なし (`secrets` は token_hex で使用継続) |
| `routes/api_system.py` | `_require_admin_token_if_remote` と credentials inline の compare を `check_admin_token` に置換 (length/logic・ログ文言・reason 文字列は不変)。未使用化した `import secrets` を除去 |
| `route_helpers.py` | rate-limit fingerprint の compare を `check_admin_token` に置換 (length plausibility ガードは維持)。ローカル `import secrets as _secrets` を除去 |
| `credential_manager.py` | `require_extension_token(request)` を新設 (extension Bearer 専用。Bearer 前置・strip・compare の順序は旧 inline と同一)。admin predicate との混同を型・配置で防止 |
| `routes/stocks/views.py` | add_ext の inline 検証を helper 呼出しに置換。reason 文字列は helper 返却をそのまま使用 (文言同一)。未使用化した `import secrets` を除去 |
| tests 6箇所 | `routes.stocks.views.get_or_create_extension_api_token` patch を `credential_manager.get_or_create_extension_api_token` に移設 (helper が同一モジュール global を読むため有効。意図・アサーション不変) |
| `tests/test_u6_auth_unification_2026_10_23.py` (新設) | 6ケース: compare_digest の owner-module sweep (networking/credential_manager/shutdown のみ)、admin primitive の一致/不一致/短/空/未設定/長大・非例外、extension の Bearer 行列・実 round-trip、admin⇄extension の非互換 |

検証 (全て終了コード 0): full suite (PYTEST_EXIT=0, FAILED 0) / `ruff check` All checks passed! / `flake8 E9,F63,F7,F82` 0 / `mypy` 90 files Success / `pyrefly` 0 errors (20 suppressed 既知) / `pylint --errors-only` 無出力 (credential_manager を scope 追加)。JS 未変更のため npm 対象外。`any`・抑制コメント・skip・妥協アサーションなし。次は U7。

## 6n. 修正サイクル U7 完了記録 (2026-10-23/24 実施)

対象: C-03「lock 保持中の fsync」。調査の結果、routes 側と `save_user_stocks()` 本体は先行対応済み (変異は lock 内・persist は lock 外・失敗時 rollback) だったため、残存していた bg 側 2 箇所を同一パターンに修正。世代管理・rollback 仕様・原子書込 (tmp+fsync+os.replace) は不変。

| 変更 | 内容 |
| --- | --- |
| `bg/sync_worker.py` USDJPY persist | `save_fn()` を `user_stocks_lock` の外へ移動。in-memory rate 更新は lock 内のまま。`last_usdjpy_persisted_ts` の更新は save 成功時のみ (else 節で lock 再取得して更新。失敗時は従来どおり警告のみ) |
| `bg/sync_worker.py` `_auto_remove_invalid_symbols` | 除去変異は lock 内のまま、`save_fn()` を lock 外へ移動。失敗時は同一 lock 順序 (`invalid_symbol_lock`, `user_stocks_lock`) で再取得して container+streak を復元 (従来のロールバック内容と同一)。`persist_error` 後の error log・後処理は不変 |
| sweep (コード変更なし) | `user_stocks_lock` を掴む全15箇所を確認: 他は snapshot/copy のみで I/O なし。`load_user_stocks` の lock 内 read は rev チェックと読取の原子性に必要で対象外 |
| `tests/test_u7_lock_free_persist_2026_10_23.py` (新設) | 5ケース: AST による構造 sweep (4ファイルの lock 内 save 呼出し検出)、auto-remove の save 時 lock 非保持＋除去成立、失敗時 rollback 復元、USDJPY の save 時 lock 非保持＋persisted_ts 更新、失敗時は rate 維持・persisted_ts 不変 |

検証 (全て終了コード 0): full suite (PYTEST_EXIT=0, FAILED 0) / `ruff check` All checks passed! / `flake8 E9,F63,F7,F82` 0 / `mypy` 90 files Success / `pyrefly` 0 errors (20 suppressed 既知) / `pylint --errors-only` 無出力 (bg/credential_manager を scope 追加)。JS 未変更のため npm 対象外。`any`・抑制コメント・skip・妥協アサーションなし。次は U8。

## 6o. 修正サイクル U8 完了記録 (2026-10-23/24 実施)

対象: A-08 (安全側デフォルト) + E-04 (SSE snapshot 世代共有)。per-tick 配信は既に共有 announce 化済みのため、残っていた「接続ごとの initial snapshot 再構築」を共有化。replay 意味論 (per-client seq 発行)・200/403 契約は不変。

| 変更 | 内容 |
| --- | --- |
| A-08 `routes/stocks/common.py` (確認のみ) | wrapper 既定は `include_portfolio=False` 化済み。全呼出し (portfolio True / quotes・views・stream False) は明示のまま。コード変更なし |
| E-04 `routes/stocks/stream.py` | initial snapshot を世代キー共有化。キーは (sse_mode, generation, yf_limit, us_open, jp_open)。generation は bg announce と同一の `_sse_payload_generation` (lazy `import app_bg` で参照)。一致すれば構築済み JSON 文字列を再利用し、per-client の `sse_event_log.emit` (seq 発行) は維持。共有するのは文字列のみで dict の別名化なし。モード別に最大2エントリ |
| E-04 無効化 audit (コード変更なし) | 全 cache 書換経路 (sync 完了・interpolator・watchlist 変異・follower reload・indices 更新後の announce 経路) が `_invalidate_sse_payload_cache()` を呼ぶことを確認。generation は健全な無効化信号 |
| `tests/test_support/state_reset.py` | `_reset_caches_and_sessions` で共有 snapshot cache をクリア。generation キーのため本来テスト間で衝突しないが、cache を直接書き換えるテスト (NaN sanitizer 等) との順序依存を断つ隔離措置 |
| `tests/test_u8_sse_snapshot_sharing_2026_10_23.py` (新設) | 4ケース: wrapper 既定 False の署名＋無引数 strip 動作、同一 generation 内の共有 (resolve 1回・payload 等価)、generation bump での再構築 (内容更新)、mode 別独立エントリ |

検証 (全て終了コード 0): full suite 2回連続 (PYTEST_EXIT=0, FAILED 0) / `ruff check` All checks passed! / `flake8 E9,F63,F7,F82` 0 / `mypy` 90 files Success / `pyrefly` 0 errors (20 suppressed 既知) / `pylint --errors-only` 無出力。JS 未変更のため npm 対象外。`any`・抑制コメント・skip・妥協アサーションなし。なお U2 時に単発観測された NaN テストの順序依存失敗は、本 U8 の共有化で顕在化しうる同型の隔離欠落だったため上記隔離措置で根治。以降の full は安定。次は U9。

## 6h. 修正サイクル U1-g 完了記録 (2026-10-23 実施)

対象: U1 の次の論理単位「`POST /api/credentials` 要求ボディの契約単一化」。

| 変更 | 内容 |
| --- | --- |
| `constants.py` | `AGENT_ID_RE` を単一化 (route 側の `_AGENT_ID_RE` 定義を撤去し共有。値不変) |
| `schemas/config.py` | `CredentialsSaveRequest` を新設・正本化。`normalize_credentials_save` (before) で旧ルートの逐次順序 (5鍵の型→形式→prompt→mode→agent→preview→model形状→provider→URL→model名) を同一化し、16種マーカーで写像。strip・空文字の素通し (クリア意味)・`min_length` 5種・`_is_valid_api_key` 共通述語・URLの `rstrip("/")` を旧来通り再現。サーバ状態依存則 (Agents要ID・Agents時model禁止・modelカタログ解決) は route に残置 |
| `route_helpers.py` | `_CREDENTIALS_KEY_SPECS` (Mistral のみ `INVALID_API_KEY`・他 `UNSAFE_INPUT` という旧来の癖を保持) + `_credentials_error_from_validation` (完全一致マーカー照合。`openai_model_not_string` が `model_not_string` を含むため) + `_parse_credentials_save_request` を新設 |
| `routes/api_system.py` | POST検証約190行をアダプタ委譲に置換。以降は正規化済み `parsed_creds.*` のみを使用 (state則・save呼出し・ログは不変)。未使用化した鍵長定数・`_is_valid_api_key`・`_AGENT_ID_RE`・`re` import を除去 |
| `tests/test_schema_contract_credentials_2026_10_23.py` | 36ケース: malformed・鍵型3種・鍵形式7種 (code分岐含む)・prompt/mode/agent/preview 8種・model/provider/URL/model名7種・優先順位4種・state則3種・正常系200×4 (kwargs完全一致・strip・no-op)・programmatic正規化 |

検証 (全て終了コード 0): full suite `3196 passed, 3 skipped, 47 subtests` / `ruff` All checks / `flake8 E9,F63,F7,F82` 0 / `mypy` 90 files Success / `pyrefly` 0 errors (20 suppressed 既知) / `pylint --errors-only` 無出力。Python のみのため npm 不要。契約アサーションは無改変。`any`・抑制コメント・skip・妥協アサーションなし。

## 6g. 修正サイクル U1-f 完了記録 (2026-10-23 実施)

対象: U1 の次の論理単位「`POST /api/analyze-chart-image` 要求ボディの契約単一化」。

| 変更 | 内容 |
| --- | --- |
| `schemas/analysis.py` | `AIAnalyzeChartImageRequest` を正本化。`normalize_chart_image` (before) で旧ルートの逐次順序 (image欠番/型/strip/上限→Data-URI分岐5種→Base64分岐3種→symbol型/形状→market型/空→prompt型/長) を同一化し、14種マーカーで写像。`image_data or image` の合流・strip後検査・`if raw_symbol else ""`・market既定 `"us"`・prompt既定 `""`/strip を旧来通り再現。programmatic既存テスト (`test_schemas`・`09_v7`) は無改変で通過 |
| `route_helpers.py` | `_chart_image_error_from_validation` (旧来の優先順位を再現) + `_parse_chart_image_request` を新設 |
| `routes/api_analysis.py` | `api_analyze_chart_image` の約100行の手書き検証をアダプタ呼出しに置換。以降は `parsed_ci.image_data/market/symbol/prompt` のみを使用。`BAD_REQUEST` の body/`401` の鍵ゲートは route に残置。未使用化した `CHART_IMAGE_MAX_IMAGE_DATA_CHARS`・`is_valid_symbol`・`normalize_symbol_for_market` import を除去 |
| `tests/test_schema_contract_chart_image_2026_10_23.py` | 32ケース: body/auth gate 2種・image欠番/型/上限4種・Data-URI分岐6種・Base64分岐3種・symbol/market/prompt 6種・優先順位5種・正常系200×5 (正規化・`.T`・別名・strip・500写像)・programmatic合流 |
| `tests/test_code_review_goal_audit_2026_10_v8.py` | 検証スタブの patch 対象を `routes.api_analysis.is_valid_symbol` → `schemas.analysis.is_valid_symbol` に移設 (検証本体が schema に移ったため。意図・アサーションは不変の1行移設。dead import 残置案は F401/PLC0414 に抵触するため不採用) |

検証 (全て終了コード 0): full suite `3160 passed, 3 skipped, 47 subtests` / `ruff` All checks / `flake8 E9,F63,F7,F82` 0 / `mypy` 90 files Success / `pyrefly` 0 errors (20 suppressed 既知) / `pylint --errors-only` 無出力。Python のみのため npm 不要。契約アサーションは無改変。`any`・抑制コメント・skip・妥協アサーションなし。

## 6i. 修正サイクル U9-a 完了記録 (2026-10-23 実施)

対象: B-01 の最初の1モジュール「`config_store.py` (1248行) の純粋移動分割」。U9 の Done 条件 (1モジュール分割 green) に対応。

| 変更 | 内容 |
| --- | --- |
| `config/` (新設7ファイル) | `paths` (ランタイムdir解決＋`_ensure_runtime_dir`)・`locks` (プロセス間ロック2種)・`atomic_write` (原子書込6関数)・`corruption` (破損検出＋サニタイズbackup＋rotate)・`merge` (レガシーmerge/除去＋flag reset)・`io` (`load_config`/`save_config`＋cache key)・`master_key` (`get_or_create_master_key`)。いずれも純粋移動。logger 名は `config_store` を維持 |
| `config_store.py` | 共有状態 (パス定数・ロック・`DEFAULT_CONFIG`・merge allowlist・`_CONFIG_CACHE`・`_LEGACY_MERGE_DONE`・`_CONFIG_CORRUPTED`) を所有し、振る舞い39名を再exportするシムに縮小。`import` は全て先頭に集約 (E402 回避)。`os`・`logger`・crypto 3名はテスト互換のため残置 |
| patch互換設計 | `config/` 側は共有状態・`load_config`/`save_config`/`_is_windows` を `config_store` 属性として呼時解決。`patches.py` の再束縛 (`CONFIG_FILE` 等)・`patch("config_store.X")`・`patch("crypto_utils._is_windows")` (master_key は原文通り crypto 名前空間解決) が従来通り有効 |
| `tests/test_review_followup_20260814.py` | 分割に伴う追随のみ (アサーション強度不変): umask検査は `config_store.py`＋`config/{io,atomic_write,corruption}.py` を走査、O_DIRECTORY検査は `config/{io,atomic_write}.py` を追加し、fsync実体のある3ファイルに getattr 存在を要求 |
| `config/master_key.py` | `KEYRING_AVAILABLE` の遅延 import に `_is_windows` を復帰 (原文通り crypto 名前空間解決。`test_ephemeral_master_key_bypass_*` が `crypto_utils._is_windows` を patch するため) |

検証 (全て終了コード 0): full suite `3252 passed, 3 skipped, 47 subtests` / `ruff` All checks / `flake8 E9,F63,F7,F82` 0 / `mypy` 98 files Success / `pyrefly` 0 errors (18 suppressed) 両platform / `pylint --errors-only` 無出力。Python のみのため npm 対象外。`any`・抑制コメント・skip・妥協アサーションなし。次は U9-b (B-02 `app.py` 等の次モジュール)。

## 6j. 修正サイクル U9-b 完了記録 (2026-10-23 実施)

対象: B-02 の2モジュール目「`app.py` (920行) の純粋移動分割」。U9 の Done 条件 (1モジュール分割 green) に対応。

| 変更 | 内容 |
| --- | --- |
| `app_factory/` (新設6ファイル) | `early` (excepthook＋atexit)・`wiring` (ProxyFix/secret/static/signals)・`hooks` (Sec-Fetch-Site/CORS/ログ)・`warmup` (news warmup)・`factory` (`create_app` 系)・`boot` (`bootstrap`)。いずれも純粋移動。logger 名は `app` に統一 (`app.logger` patch と同一オブジェクト) |
| `app.py` | 共有状態 (`_app_bootstrap_done`/`_app_bootstrap_lock`) を所有し31名を再exportするシムに縮小。`app = create_app()`・エイリアス・LANGSEARCH定数・`__main__` は原文のまま。`import` は全て先頭に集約 |
| 振る舞い維持の要点 | `factory.create_app` は `Flask("app")` 固定 (原文 `Flask(__name__)` と同一の root_path。`app_factory.factory` の `__name__` 解決だと templates が見つからず CSP テストが 500 になるため)。`_ensure_bootstrap_called` の `_env_bool` と `boot` の flag/lock/`_start_background_threads`/`schedule_*`・`warmup` の APIキー取得・`early` の `_is_windows_runtime` は `app` 属性として呼時解決 (各 `patch("app.X")` が有効) |
| テスト追随 (強度不変) | `test_be02` は `app_factory/early.py` を走査。U2/U3/U5/U6 のソーススキャンに `app_factory` を追加 |

検証 (全て終了コード 0): full suite `3252 passed, 3 skipped, 47 subtests` (最終状態で再実行) / `ruff` All checks / `flake8 E9,F63,F7,F82` 0 / `mypy` 105 files Success / `pyrefly` 0 errors (14 suppressed) 両platform / `pylint --errors-only` 無出力。Python のみのため npm 対象外。`any`・抑制・skip・妥協アサーションなし。次は U9-c。

## 6k. 修正サイクル U9-c 完了記録 (2026-10-23 実施)

対象: C-01 の3モジュール目「`routes/api_system.py` (1255行・7機能) の純粋移動分割」。U9 の Done 条件 (1モジュール分割 green) に対応。

| 変更 | 内容 |
| --- | --- |
| `routes/system/` (新設8ファイル) | `blueprint` (`api_system_bp`)・`guards` (管理トークンゲート)・`credentials` (資格情報2EP＋safe response)・`observe` (health/cache-stats/metrics)・`security` (csrf-token)・`csp_report` (受信＋sanitize)・`shutdown` (終了＋`_terminate_current_process`)・`ai_usage`。いずれも純粋移動 |
| `routes/api_system.py` | 31名を再exportするシムに縮小。`os`・`logging`・`app_state`・credential 4関数・`_is_local_request` は patch/属性チェーン互換のため残置。URL (`api_system.*` endpoint 9件) 不変を確認 |
| 循環 import 回避 | `routes/system/*` は `import routes.api_system` (plain import)＋呼時属性解決のみ。`from routes import api_system` は部分初期化時の親属性未設定で失敗しうるため不採用 (crypto_utils と同一パターン) |
| 互換の要点 | `save/get_state/get_prompt/clear`・`_is_local_request`・`_require_admin_token_if_remote` は呼時解決 (各 `patch("routes.api_system.X")` が有効)。`Blueprint("api_system", "routes.api_system")` で import_name まで原文一致 |
| テスト追随 (強度不変) | `test_remote_api_and_prod_use_env_bool` は `routes/system/` 6ファイルを合算走査。U2/U3/U5/U6 の rglob 系スキャンは `routes/` 配下のため自動追随 |

検証 (全て終了コード 0): full suite `3252 passed, 3 skipped, 47 subtests` (ruff --fix 後に再実行) / `ruff` All checks / `flake8 E9,F63,F7,F82` 0 / `mypy` 114 files Success / `pyrefly` 0 errors (14 suppressed) 両platform / `pylint --errors-only` 無出力。Python のみのため npm 対象外。`any`・抑制・skip・妥協アサーションなし。次は U9-d。

## 6l. 修正サイクル U9-d 完了記録 (2026-10-23 実施)

対象: D-06 の4モジュール目「`services/stock_provider.py` (1814行) の純粋移動分割」。U9 の Done 条件 (1モジュール分割 green) に対応。

| 変更 | 内容 |
| --- | --- |
| `services/stock/` (新設5ファイル) | `sanitize` (ファンダメンタル値サニタイズ)・`errors` (yfinance エラー分類＋レート制限記録)・`retry` (指数バックオフ `with_yfinance_retry`)・`provider` (`BaseStockProvider`/`YFinanceProvider` 本体: Ticker 有界キャッシュ・履歴取得・fast属性)。いずれも純粋移動。logger 名は `services.stock_provider` を維持 |
| `services/stock_provider.py` | 再exportシムに縮小。`yf`・`time`・`yf_session_manager`・`_handle_yf_rate_limit` は `patch("services.stock_provider.X")` (名前置換) 互換のためこのモジュール経由で公開し、`services/stock/` 側は呼時属性解決 (`import services.stock_provider`＋関数内参照)。`yf`/`time` への patch は共有モジュールオブジェクトに対するもののため直接 import で可 |
| 互換の要点 | `_TICKER_CACHE_MAX`/`_TICKER_CACHE_TTL_SEC` の実体は `services.stock.provider` に移動し、シムは値を再export (読取互換)。`get_ticker` の有界退避・sliding TTL・セッション検証の意味は不変 |
| テスト追随 (強度不変) | `test_get_ticker_cache_is_bounded` の `_TICKER_CACHE_MAX = 2` 書込先を実体の所有モジュール `services.stock.provider` に変更 (シムへの代入は関数参照に届かないため)。アサーション (上限2・最古退避) は原文通り |
| lint 追随 | 分割で `provider.py` に残った dangling `import json` (実使用は `sanitize.py`) を除去し、`ruff --fix` で import 整列 (I001×3) を正規化。挙動無関係 |

検証 (全て終了コード 0): full suite `3252 passed, 3 skipped, 47 subtests` / `ruff` All checks / `flake8 E9,F63,F7,F82` 0 / `mypy` 119 files Success / `pyrefly` 0 errors / `pylint --errors-only` 無出力。Python のみのため npm 対象外。`any`・抑制コメント・skip・妥協アサーションなし。次は U9-e。

## 6m. U9-d 監査指摘の追補修正 (2026-10-23 実施)

対象: U9-d 監査の4指摘 (循環 import・`_TICKER_CACHE_MAX` 二重所有・pyrefly スコープ・importer sweep 未記録)。振る舞い不変。

| 変更 | 内容 |
| --- | --- |
| 循環 import 解消 | `services/stock/provider.py`・`retry.py` のトップレベル `import services.stock_provider` を撤去。呼時解決は `services/stock/__init__.py` の `_shim()` (単一所有・`sys.modules` 遅延解決＋直接 submodule import 時の `importlib` fallback) に集約し、両モジュールは `from services.stock import _shim` で使用。`patch("services.stock_provider.X")` 互換は維持。full `pylint` の R0401×3 と R0801 は無出力化 |
| `_TICKER_CACHE_MAX` 単一所有 | 実体を `services/stock_provider.py` ファサードに集約 (`_TICKER_CACHE_TTL_SEC` とも)。`provider.py` は `_shim()._TICKER_CACHE_MAX` で呼時解決するため、分割前の `sp._TICKER_CACHE_MAX = 2` 書込が再び有効。`test_get_ticker_cache_is_bounded` は U9-d 時の追随を revert し原文 (shim 書込) に復帰＝書込シームの pin テストを兼ねる |
| importer sweep 証跡 | ファサード経由の外部 import は `app_state` (`YFinanceProvider`)・`sync_worker` (2名)・`stock_service` (2名)・tests 9件の `from services.stock_provider import` のみで、全名がシム再exportに存在することを確認。`services.stock.*` 直 import はなし |

検証 (全て終了コード 0): full suite `3252 passed, 3 skipped, 47 subtests` / `ruff` All checks / `flake8 E9,F63,F7,F82` 0 / `mypy` 119 files Success / `pyrefly check` (プロジェクト全体) 0 errors / `pylint` (full, errors 以外も確認) R0401/R0801 なし・`--errors-only` 無出力。`any`・抑制コメント・skip なし。次は U9-e。

## 6n. 修正サイクル U9-e 完了記録 (2026-10-23 実施)

対象: E-01 の5モジュール目「`bg/sync_worker.py` (1389行) の純粋移動分割」。U9 の Done 条件 (1モジュール分割 green) に対応。

| 変更 | 内容 |
| --- | --- |
| `bg/sync/` (新設5ファイル) | `__init__` (facade accessor `_shim`)・`state` (共有状態・stale回復・app_bgミラー accessor)・`fetch` (単体/バッチ/指数取得)・`process` (キャッシュ準備・マージ・指数更新・無効銘柄除去)・`control` (全体同期・スケジュール・ループ・watchdog・起動)。25関数全ての AST が原文と同一 (適応7箇所を除き差分ゼロを機械検証) |
| `bg/sync_worker.py` | 再exportシムに縮小。`acquire_yfinance_slot`/`load_user_stocks`/`ensure_stock_placeholder_in_caches` は `patch("bg.sync_worker.X")` 互換のため所有し、`bg/sync/` 側は呼時解決。`_sync_start_time`/`_sync_generation` は `__getattr__` で live 値＋型チェッカ用の bare annotation 併用。lock順序 (`invalid_symbol_lock`→`user_stocks_lock`、persist は lock 外) と app_bg ミラー委譲 (`target is not <self>`) は不変 |
| 適応7箇所 (意味同一) | `acquire_yfinance_slot` ×3・`load_user_stocks` ×1・`ensure_stock_placeholder_in_caches` ×1 を `_shim()` 経由化、`_sync_generation` 読取 ×4・`_last_loaded_mtimes` ×2 を `_state` 属性化、`schedule_sync_all_stocks_now` の process→control 参照 ×2 を `_shim()` 経由化 (循環回避) |
| テスト追随 (強度不変) | `TestSyncWorkerImports` の走査対象を `bg/sync/control.py` に変更 (heatmap import の実体)。U7 の AST sweep 対象を `bg/sync/process.py`＋`bg/sync/control.py` に変更 (save 呼び出しの実体)。いずれもアサーション不変 |
| importer sweep 証跡 | `from bg.sync_worker import` は `bg/__init__.py`・`app_bg.py`・tests の計28名で、全名がシムに存在することを機械確認。`bg.sync.*` 直 import はなし |

検証 (全て終了コード 0): full suite `3252 passed, 3 skipped, 47 subtests` (ruff --fix・mypy 対応後に再実行) / `ruff` All checks / `flake8 E9,F63,F7,F82` 0 / `mypy` 124 files Success / `pyrefly check` (プロジェクト全体) 0 errors / `pylint --errors-only` 無出力。Python のみのため npm 対象外。`any`・抑制コメント・skip なし。次は U9-f。
## 6p. 修正サイクル K-03完了記録 (2026-10-24 実施)

対象: K-03残「stop() が in-flight PTS fetch を待たず、残存スレッドが start() を RuntimeError 化・restart() を defer し続ける再起動不能」。最高リスクの backend RACE として選定 (E-02 は設計改善・G-05 は本番ガード有りを確認済のため対象外)。

| 変更 | 内容 |
| --- | --- |
| `services/realtime/engine.py` | `_pts_fetch_idle` (Event) で fetch 実行中を追跡。`stop()` は join(5s) 後も生存＋fetch 実行中なら `PTS_FETCH_DRAIN_TIMEOUT_SEC` (30s: primary 6s＋fallback 3×5s の実測上限) まで有界 drain し、ループに running=False を観測させて終了させる。戻り値を `bool` 化し、残存時は False を明示返却 (レビュー指示の「残存時は明示的に失敗」)。`__new__` 構築の旧テスト用に `_pts_fetch_tracker()` で getattr フォールバック (未追跡時は従来動作) |
| `tests/test_k03_pts_inflight_drain_2026_10_24.py` (新設) | 3ケース: drain 完了で stop()=True＋即時 restart 成功 (wedge 解消の実証)・drain 上限超過で stop()=False＋スレッド保持＋解放後の epoch ゲートで publish なし＋回復・restart() の end-to-end 回復 |
| `クロスサーチ` (コード変更なし) | `TradingViewWSClient.start` は生存スレッドで早期復帰・`YahooJPRealtimeScraper.start` は RuntimeError を投げず wedge しないことを確認。wedge するのは PTS のみのため対象は engine のみ |
| 監査追補 (2026-10-24) | stop() の join-then-drain を drain-first＋`_drain_pts_fetch` 抽出に整理 (hung fetch 時の stop を 7.33s→2.33s に短縮、実測)。`restart()` は `stop()` の bool verdict を消費し、明示 False 時は冗長 join を省いて即 defer。既存 defer テストは無改変 green |

検証 (全て終了コード 0): 新規3件＋realtime 関連 117件 green / full suite `3255 passed, 3 skipped, 47 subtests` / `ruff` All checks / `flake8 E9,F63,F7,F82` 0 / `mypy` 124 files Success / `pyrefly` 0 errors / `pylint --errors-only` 無出力。Python のみの変更のため npm 対象外。`any`・抑制コメント・skip なし。修正中に旧テスト (`test_pts_worker_stops_promptly_during_inter_symbol_sleep` の `object.__new__` 構築) の AttributeError を検出し、同テスト無改変で通る形に修正済み。
