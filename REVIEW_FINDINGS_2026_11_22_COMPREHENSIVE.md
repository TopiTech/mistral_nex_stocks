# 包括的コードレビュー 全課題リスト (2026-11-22)

- 対象: `mistral_nex_stocks_complete_fixed_v3` / branch `master` / HEAD `f72e297` (2026-11-21 22:47:55 +0900)
- 手法: スライス分割による全ファイル精読 + 静的検証コマンド実測 + 子エージェント所見の現物反証
- 環境: Windows 11 / pwsh / Python 3.14.7 (`.venv`) / Node v24.22.1 / npm 11.19.1
- 本ドキュメントは**課題リスト**であり、ソースコードは一切変更していない(読み取り・検索・検査のみ)

---

## 1. エグゼクティブサマリ

| 重要度 | 件数 | 内容 |
|---|---|---|
| BLOCKER | 0 | — |
| MAJOR | 2 | ファンダメンタル完全性判定の誤りによるディスクキャッシュ汚染 (M-01)、no-key 分岐が保存済み AI ポートフォリオをスタブで上書き (AIF-1) |
| MINOR | 19 | サーキットブレーカー会計 4 件、AI 応答契約 3 件、データ層 2 件、フロント非同期/ライフサイクル 6 件、フロント UI/可視化 3 件、AI フォールバック判定 1 件 |
| NIT | 42 | 死コード、非対称な定数、入力検証の粒度、運用上の陳腐化、硬化余地 |

最重要は **M-01**(`utils/stock_payload.py` の完全性判定)。quote-only の軽量 payload が「完全なファンダメンタル」と誤判定され、24 時間ディスクキャッシュに永続化されたうえ `get_info` の再取得まで抑止される。UI 側は「財務データあり」として描画するため、**画面に出る数値が 24 時間 stale になる**。回帰テストの fake payload が判定を通過する形になっているため、テストでは検出できない。

次点は AI 経路のサーキットブレーカー会計 4 件(AI-01〜AI-04)。いずれも「健全なのに 503」「異常なのに成功記録」という逆向きの誤判定で、単発では顕在化しにくいが、同時要求時に可用性と課金の両方へ効く。

フロントの非同期/ライフサイクル 6 件(FE-01〜FE-06)はすべて「成功したのに失敗表示」「離脱復帰後に永久ローディング」という**ユーザーに見える誤報**で、データ破壊はない。UI/可視化の 3 件(UI-1, OBS-01, OBS-02)も同じく表示・操作の問題だが、OBS-02 は描画ループが恒久停止するため再読込するまで画面が固まる。

もう 1 件の MAJOR は **AIF-1**(`services/ai_portfolio_service.py:916-928`)。API キー未設定または preview モード時のフォールバック経路だけが「既存保存を守る」ガード (`:1079-1084`) を持たず、**プリセットテーマではユーザーの保存済みポートフォリオをハードコードのスタブで上書き**し、カスタムテーマでは**重複レコードを毎回追加**して `MAX_SAVED_AI_PORTFOLIOS = 20` の切り捨て(`:485-486`)で他テーマの保存分を追い出す。同ファイルのコメント自身がこの事象を禁じており、LLM 失敗経路には回帰テスト(`tests/test_audit_track_d_services_bg.py:1207`)まである。no-key 経路だけが取り残されている。

---

## 2. 実行した検証と実測値

### 2.1 テスト

```
.\.venv\Scripts\python.exe -m pytest -o addopts="" -q -n auto --dist=loadfile \
  --color=no -p no:cacheprovider --junitxml=<tmp>\junit.xml
→ 4730 passed, 5 skipped, 47 subtests passed in 33.91s / EXIT=0
→ junit: tests=4782 failures=0 errors=0 skipped=5 time=33.890
```

- 収集 4,735 テスト / 385 ファイル。失敗・エラー 0。skip 5 件は環境依存(OS 分岐)で、いずれも Windows 上での想定内。
- 単体で 2 回、junit 付きで 1 回、計 3 回実行し、すべて EXIT=0 で再現。

### 2.2 静的解析・ビルド

| コマンド | 結果 |
|---|---|
| `ruff check .` | exit 0 |
| `mypy` (pyproject 設定 / 126 files) | exit 0 |
| `bandit -c pyproject.toml` (medium 以上) | exit 0 |
| `npm run build` | exit 0 |
| `coverage` (omit `tests/*`, `.mimocode/*`) | **83.52%** (29,511 stmts / 4,862 miss / 126 files) |

参考(CI 非対象のため合否には使わない):
- `flake8`: `.agent/scripts/pre_command_guard.py:15:5: F841`(`.agent` は未追跡、CI は `--select=E9,F63,F7,F82` のみ)
- `pylint`: `utils/storage.py:236: E0401 Unable to import 'fcntl'` ほか Windows 限定 import-error(exit 2。CI は Ubuntu かつ `--disable=import-error`)

### 2.3 カバレッジ下位ファイル(未テスト領域の手がかり)

`app_factory/warmup.py` 44.0% / `config/atomic_write.py` 46.0% / `config/paths.py` 50.0% / `services/preview_responses.py` 57.5% / `app_factory/early.py` 61.1% / `services/stock/retry.py` 65.2% / `routes/system/shutdown.py` 65.6% / `bg/sync/fetch.py` 67.5% / `config/io.py` 70.5%

このうち `config/atomic_write.py`(46.0%)は**書き込みの原子性**を担う層であり、未テスト率が最も危険度の高い組み合わせ。この層には個別の不具合を起票できなかったため、テスト追加を TEST-01 として起票している。

### 2.4 API 境界の突き合わせ

- バックエンド route 定義: 46 件(`@<bp>.route(...)`)。
- フロント URL リテラル: 43 件。
- フロント未対応は `/api` のみ(基底 URL 用途で正常)。
- `/api/cache-stats`, `/api/metrics`, `/api/system/ai-usage` はテストのみが参照(OPS-02)。
- エラー形式: **統一エンベロープは存在する** — `utils/stock_payload.py:1334 ok_response` / `:1388 error_response` が単一 owner。`routes/*.py` に素の `jsonify({"error"` は 0 件(実測)。残る所見は owner の層の不自然さ(OPS-01)、`static/js/api.js:139` の `errorBody?.message` フォールバックが実質死コード(OPS-01)、および `_analyze_v2_error_response` 内での固定文言/例外 echo の混在(API-01)である。

### 2.5 横断スイープ

| 項目 | 実測 |
|---|---|
| `except Exception:` | 134 箇所 / 39 ファイル(最多 `services/ai_service.py` 15、`services/openai_service.py` 12、`utils/chat_history.py` 12) |
| `verify=False` (TLS 無効化) | 0 件 |
| `Thread(daemon=False)` | 0 件 |
| 素の `requests.get/post`(セッション/リトライ層を迂回) | 2 箇所 |
| `requires-python` | `>=3.11,<3.15` (`pyproject.toml:30`) |

**「Python バージョンガード欠如」は成立しない**。`requires-python = ">=3.11,<3.15"` が `pyproject.toml:30` で宣言され、CI も 3.11〜3.14 のマトリクスで実行している。当初「`app_factory/factory.py` に空コメント残骸」としていた所見は**事実誤認のため撤回**(同ファイルに該当行なし。`app_factory/hooks.py:146` の素の `#` はコメントブロック内の段落区切りで、残骸ではない)。

---

## 3. 課題一覧(索引)

### MAJOR

- **M-01** ファンダメンタル完全性判定が quote-only payload を「完全」と誤判定 → 24h キャッシュ汚染 + `get_info` 抑止
- **AIF-1** no-key/preview 分岐が `existing_saved` ガードを迂回し、保存済み AI ポートフォリオをスタブで上書き破壊 / 重複増産

### MINOR

| ID | 領域 | 位置 | 一言 |
|---|---|---|---|
| AI-01 | AI/埋め込み | `services/openai_service.py:1876-1901` | embeddings がセマフォを通らず、プローブをバッチ全体で保持 |
| AI-02 | AI/サーキット | `services/ai_service.py:1332-1349` | HALF_OPEN claim の TOCTOU で健全回路に 503 |
| AI-03 | AI/サーキット | `services/openai_service.py:1879-1883` | 非 JSON 200 で先に success を報告 |
| AI-04 | AI/フォールバック | `services/ai_service.py:2780`, `:3153-3166` | `_is_fallback` が多重意味でサーキットゲートを迂回 |
| AI-05 | AI/キャッシュ | `services/openai_service.py:685-694` ↔ `:743-745` | 温度正規化前にキャッシュキーを生成 |
| AI-06 | AI/ストリーム | `services/ai_service.py:3030-3052`, `:3159-3166` | 切断時にプローブを解放するが成功報告せず HALF_OPEN 残留 |
| AI-07 | AI/ニュース | `services/news_service.py:378-397` | プレースホルダが `status=success` のまま UI に緑✓ |
| DATA-01 | データ/株価 | `services/stock/provider.py:936-944`, `:1000` | `_fast_get` が NaN を素通し(対照 `:1113` は sanitize 済み) |
| DATA-02 | データ/リアルタイム | `services/realtime/engine.py:827-832` | `_handle_pts_update` に `registration=` を渡していない |
| FE-01 | フロント/ヒートマップ | `static/js/heatmap.js:1399-1423` | bfcache 復帰で 3D キャンバスが空白のまま |
| FE-02 | フロント/スクリーナ | `static/js/screener.js:371-380`, `:427-433`, `:756-765` | bfcache 復帰で「データをロード中...」が永久残留 |
| FE-03 | フロント/設定 | `static/js/settings.js:1356-1358` | 遅延 GET 応答が入力中のプロンプトを上書き |
| FE-04 | フロント/AI PF | `static/js/ai_portfolio.js:1052-1080` | 反映成功後に再取得失敗で失敗トースト |
| FE-05 | フロント/API | `static/js/api.js:2611-2621` ↔ `static/js/ui.js:3112-3121` | チャット送信に `signal` が渡らず最大 170s 入力ブロック |
| FE-06 | フロント/状態 | `static/js/state.js:300-310` | `replaceStock` が `"stocks"` 通知を発火しない |
| UI-1 | フロント/UI | `static/js/ui.js:202-203` | 詳細取得失敗バナーを全銘柄分無条件削除、再試行導線が消える |
| OBS-01 | フロント/Observatory | `static/js/experimental/orbit-entry.js:551-553` | サイレント更新がユーザー読込を中断し、オーバーレイが無表示・無リトライで残留 |
| OBS-02 | フロント/Observatory | `static/js/experimental/orbit-renderer.js:93-108` | `render()` 例外で rAF ループが恒久停止(再予約が後置、try 不在) |
| AIF-2 | AI/ポートフォリオ | `services/ai_portfolio_service.py:535` | フォールバックのテーマ判定が部分文字列一致で `retail`/`airline` 等に `ai` が誤マッチ |

### NIT

| ID | 領域 | 位置 | 一言 |
|---|---|---|---|
| AI-08 | AI/agents | `services/ai_service.py:3021-3025` | `item.get("usage")` 分岐が到達不能(防御コード) |
| AI-09 | AI/定数 | `services/ai_service.py:1795` ↔ `services/openai_service.py:1484` | ツール反復上限が 10 / 8 で非対称 |
| AI-10 | AI/埋め込み | `services/embeddings_service.py:60-66` | `texts=None` で `len(None)` → TypeError |
| AI-11 | AI/埋め込み | `services/embeddings_service.py:96-116`, `:205-222` | キャッシュ内リストを参照共有で返す |
| DATA-03 | データ/市場時間 | `utils/market_utils.py:470` | fail-open `return True`(現状到達不能) |
| DATA-04 | データ/市場時間 | `utils/market_utils.py:373-376` | フォールバックが `-5h` 固定で EDT に 1 時間ずれる |
| DATA-05 | データ/祝日 | `utils/market_utils.py:78-91` | 固定日テーブルが過去の五輪特例・即位の日・旧山の日を扱わない |
| FE-07 | フロント/設定 | `static/js/settings.js:1278-1292` | 接続テスト失敗時に旧モデルチップが残留 |
| FE-08 | フロント/スクリーナ | `static/js/screener.js:418-421` | 取得失敗時に件数ヘッダが前回値のまま |
| FE-09 | フロント/設定 | `static/js/settings.js:933-935`, `:1441-1443`, `:1591-1593` | `setTimeout` ハンドル未保持で状態表示が消される |
| FE-10 | フロント/アラート | `static/js/index_main.js:984-992` | 描画毎に `JSON.parse(localStorage.userAlerts)` |
| RT-01 | リアルタイム | `services/realtime/scrapers.py:1252` | 未使用変数 `workers` |
| RT-02 | リアルタイム | `services/realtime/utils.py:514-521` | 非有界のブレース走査 |
| RT-03 | リアルタイム | `trend_sources.py:62-63` | 6 ワーカー非有界 executor + `TrendReq` timeout なし |
| MAP-01 | マッパー | `utils/tradingview_mapper.py:410-417` | `BTC-USD` を `NYSE:` に割り当て |
| API-01 | API 境界 | `routes/api_analysis.py:2360-2411` | 同一関数内で固定文言と例外文言 echo が混在 |
| CACHE-01 | キャッシュ | `utils/disk_cache.py:466`, `:476` | 掃除の経過判定が `max(default_ttl, 86400)` で 24h 超 TTL を打ち切る |
| STOCK-01 | データ/銘柄 | `services/stock/provider.py:1337`, `:1352-1360` | 企業概要を 200 文字で無言切り詰め |
| MDS-01 | データ/市場 | `services/market_data_service.py:148-152` | `sector`/`name` の型を検証しない |
| CFG-01 | 設定 | `schemas/config.py:250` | `32` が `MISTRAL_API_KEY_MIN_LENGTH` を参照せずリテラル重複 |
| CFG-02 | 設定 | `config/io.py:90-96` | 読み取り毎に tmp ファイルを掃引 |
| LOG-01 | ログ | `logging_config.py:364-366` | ログディレクトリだけ mode 無指定(ファイルは 0o600) |
| DOC-01 | ドキュメント | `pyproject.toml:21` | 「2100+ tests」が実測 4,735 と乖離 |
| EXT-01 | Chrome 拡張 | `chrome_extension/content.js:390-393` | 重複条件と全角括弧の未認識 |
| EXT-02 | Native host | `native_host/install_host_windows.cmd:3` | 非修飾の `PowerShell` 呼び出し |
| CI-01 | CI | `.github/workflows/dependabot-sync.yml` | `pull_request_target` + `contents: write` の硬化不足 |
| OPS-01 | API | `utils/stock_payload.py:1334`, `:1388` | 応答エンベロープの owner が payload 整形モジュール内にある |
| OPS-02 | API | `/api/cache-stats`, `/api/metrics`, `/api/system/ai-usage` | UI 未消費の運用エンドポイント |
| TEST-01 | 品質 | 第 2.3 節の 9 ファイル | カバレッジ 44〜70% 帯 |
| UI-2 | フロント/UI | `static/js/ui.js:140-153` | `typeof` ガードが TDZ で機能しない(宣言は `:2902`/`:3461`) |
| UI-3 | フロント/UI | `static/js/ui.js:190-192` | `findDetailEl` のドロワーフォールバックに所有銘柄判定がない |
| UI-4 | フロント/UI | `static/js/ui.js:1399-1401`, `:4726`, `:4734` | `e.target.closest` に Element 型ガードがない |
| OBS-03 | フロント/Observatory | `experimental/temporal-controller.js:122`, `:130` | 匿名 listener が `destroy()`(`:222`)で解除されない |
| OBS-04 | フロント/Observatory | `experimental/orbit-state.js:64-65`, `:251-253` | `_historyAbortControllers` 未使用、`_aiAbortController` は代入なしの死フィールド |
| OBS-05 | フロント/Observatory | `experimental/orbit-entry.js:799` | teardown が `beforeunload` のみで `pagehide` がない |
| OBS-06 | フロント/Observatory | `experimental/ai-dive-controller.js:158` ほか 3 箇所 | `data.stocks.get()` の型ガードが 1 箇所だけ非対称 |
| OBS-07 | フロント/Observatory | `experimental/orbit-state.js:44` | `timeCursor` の極性コメントが実装と逆 |
| OBS-08 | フロント/a11y | `experimental/accessibility-controller.js:373-382` | modal 復帰フォーカスの単一スロット共有(確度 低) |
| VER-01 | 検証スクリプト | `scripts/verify_ui_safety_contracts.mjs:141-156`, `:170-183` | origin 検証を「60 行以内に `origin` の語があるか」で判定する近接ヒューリスティック |
| VER-02 | 検証スクリプト | `scripts/verify_stock_update_signature.mjs:111-134` | 純関数を同入力で 2 回呼ぶ恒真アサーション。抑制ロジックを検証していない |
| VER-03 | 検証スクリプト | `scripts/verify_generated_frontend.mjs:50-60` | 不一致で exitCode=1 を立てた後も後続検証を続行 |
| NH-1 | Native host | `native_host/native_host.py:488-504` | 許可拡張 ID キャッシュが `st_mtime` のみをキーにする |

---

## 4. 詳細

### 4.1 MAJOR

#### M-01 ファンダメンタル完全性判定の誤り → 24h キャッシュ汚染と `get_info` 抑止

**位置**
- 判定: `utils/stock_payload.py:211-223` `_FUNDAMENTAL_KEYS`
- 判定使用: 同 `:335-339` (`prior_is_full`)、`:365` (ディスクキャッシュ永続化)、`:391` (`get_cached`)
- 生産側: `services/stock/provider.py:1000` (`get_fast_info` の返却値)

**症状**
`get_fast_info` だけが返した quote-only payload が「ファンダメンタル完備」と判定される。結果として:

1. `:365` で **24 時間ディスクキャッシュに書き込まれる**
2. `:391` の `get_cached(f"info_{symbol}", ..., duration=86400, valid_func=_is_complete_info)` が **24 時間そのまま受理**する
3. `:335-339` の `prior_is_full` が真になり、**`get_info` による本物の取得がスキップされる**

**根拠**(すべて現物コード)

```python
# utils/stock_payload.py:211-223
_FUNDAMENTAL_KEYS = (
    "trailingPE", "dividendYield", "sector", "industry",
    "targetMeanPrice", "marketCap", "fiftyTwoWeekHigh",
)

def _is_complete_info(payload: Any) -> bool:
    return isinstance(payload, dict) and bool(payload) and (
        symbol.startswith("^") or any(k in payload for k in _FUNDAMENTAL_KEYS)
    )
```

生産側 `services/stock/provider.py:995-1001`:

```python
mapped_info = {
    "shortName": None,
    "regularMarketPreviousClose": prev_close,
    "previousClose": prev_close,
    "currency": currency,
    "marketCap": _fast_get(["market_cap", "marketCap"]),   # ← ここ
    "exchange": exchange,
    "quoteType": _fast_get(["quote_type", "quoteType"]),
    "symbol": symbol,
}
cleaned = {k: v for k, v in mapped_info.items() if v is not None}
```

`marketCap` は **`get_fast_info` 自身が返すキー**であり、`any(...)` 判定なので `marketCap` 1 個の存在で「完全」になる。つまり「本物の quoteSummary を通ったか」を判別できていない。

**決定的な補強材料**: 同じ関数の `:362-364` に、実装者自身がこの危険を明示するコメントが残っている。

```python
# Only persist to the 24-hour disk cache when we hold complete
# fundamentals (or an index symbol). Writing partial fast-only
# data into the 24h store would suppress quoteSummary enrichment for
# an entire day after a transient get_info() failure.
if _is_complete_info(merged):
```

**設計意図は正しく、判定キーの選定だけが誤っている**。したがって意図的仕様ではなく実装欠陥と断定できる。対照的に `:1113` の別経路は `sanitize_fundamental_dict` を通しており、非対称。

**発生条件**
quoteSummary 呼び出しが一時的に失敗 / レート制限 / タイムアウトした際に `get_fast_info` へフォールバックした場合。以降 24 時間、`sector` / `trailingPE` / `dividendYield` / `targetMeanPrice` などが欠落したまま「完全」として扱われる。

**なぜテストで検出できないか**
回帰テストの fake が `{"regularMarketPrice": 185.0}` のみ(`tests/test_comprehensive_code_review_2026_11_21_backend.py:142-145`)で、判定を通過する形になっていないため、`_FUNDAMENTAL_KEYS` の内容と実 payload の乖離を突けない。

**修正方針**
- `marketCap` / `fiftyTwoWeekHigh` を `_FUNDAMENTAL_KEYS` から除外する。
- quoteSummary 専用キー(`sector` / `trailingPE` / `dividendYield` / `targetMeanPrice` のいずれか)を要求する。
- 指数(`^` 始まり)の例外扱いは維持する。
- 回帰テストの fake を「quote-only payload は不完全と判定される」ことを assert する形に直す。

**確度**: 高(コード経路は閉じている)/ 発生頻度は未計測(quoteSummary 障害率に依存)

---

#### AIF-1 no-key / preview 分岐が保存済み AI ポートフォリオをスタブで上書きする

**位置**: `services/ai_portfolio_service.py:916-928`(欠陥箇所)↔ `:1074-1096`(守っている側)↔ `:827-840`(`existing_saved` 算出)↔ `:465-495`(ID 置換と上限切り捨て)↔ `:747`(fallback の ID 採番)

**何が起きるか**

```python
# :916  no-key / preview-key 分岐
if not api_key or is_preview_api_key(api_key):
    portfolio = _generate_fallback_custom_portfolio(search_theme, preset_id=preset_id)
    if preset_config:
        portfolio["id"] = preset_config["id"]     # ← 保存済みレコードと同一 ID
    canonical_portfolio = sanitize_ai_portfolio(portfolio)
    _persist_generated_ai_portfolio(canonical_portfolio)   # ← 無条件 persist
    return canonical_portfolio

# :1079  LLM 失敗分岐(正しい側)
if existing_saved is not None:
    warned = dict(existing_saved)
    warned["fallback_warning"] = "AIの再生成に失敗したため、以前保存したポートフォリオを表示しています。"
    return sanitize_ai_portfolio(warned)          # ← 保存しない
```

`save_custom_ai_portfolio`(`:475-486`)は `item.get("id") == target_id` で**同一 ID のレコードを丸ごと置換**する。フォールバックの ID は `:747` の `preset_id or f"custom-{uuid.uuid4().hex[:8]}"` で決まるため:

- **プリセットテーマ**(例 `tech`): `preset_id` がそのまま ID になり、`:929` が `preset_config["id"]` で上書きするので、保存済みの同 ID レコード(銘柄・rationale・目標価格)がスタブ 5 銘柄に置換される。ユーザーの編集結果が**無告知で消える**。
- **カスタムテーマ**(例 `宇宙関連`): `preset_id=None` なので毎回新しい `custom-XXXXXXXX` を採番 → 同テーマのレコードが**実行のたびに増える**。`MAX_SAVED_AI_PORTFOLIOS = 20` に達すると `:485-486` の `portfolios[-(20-1):]` で**古い順に切り捨て**られるため、他テーマの保存分が巻き添えで消える。

**設計意図は明文化されている**。`:1074-1084` のコメントは「Never overwrite an existing saved portfolio with the hardcoded fallback stub: doing so silently replaces the user's data (items, rationale, target prices)」と明記し、回帰テスト `tests/test_audit_track_d_services_bg.py:1207 test_svc010_preset_rebalance_failure_preserves_existing_saved` が `mock_persist.assert_not_called()` まで検証している。**守られているのは LLM 失敗経路だけで、no-key 経路は同じ関数内で取り残されている**。

**発生条件**
`POST /api/ai-portfolio/rebalance`(`routes/stocks/ai_portfolio.py:547` が `force_rebalance=True` 固定)を、Mistral/OpenAI キー未設定または `preview_mode: true`(`credential_manager.py:130-137`)の状態で実行する。preview モードはキーなしで動かす**正規のサポート構成**であり、例外状態ではない。`:838` の `if not force_rebalance and existing_saved is not None: return existing_saved` もルートが常に `True` を渡すため成立しない。

**既存テストが検出できない理由**
`_generate_fallback_custom_portfolio` を直接叩くテストと `fallback_warning` のサニタイズテスト(`tests/test_ai_portfolio.py:2212`、`tests/test_code_review_audit_2026_11_comprehensive.py:116-142`)はあるが、**no-key 分岐の persist 呼び出しを観測するテストが存在しない**。

**修正方針**
1. `:916` 分岐の `_persist_generated_ai_portfolio` 直前に、LLM 失敗側と同じ `if existing_saved is not None:` 再利用 + `fallback_warning` 付与を置く。
2. 新規生成時は `preset_id` / `existing_custom_id` を `fallback_target_id` として渡し、`custom-*` の新規採番で重複しないようにする(`:1085-1090` と同じ配線)。
3. 回帰テスト: キー空 + `force_rebalance=True` を 2 回呼び、保存件数が増えないこと・既存 items が保持されることを assert する。

**確度**: 高(コード経路・ID 置換・上限切り捨て・UI 到達性・設計コメントの全矛盾を実物で確認済み)

---

### 4.2 MINOR(AI 経路)

#### AI-01 embeddings 経路がスロット機構とプローブ会計から外れている

**位置**: `services/openai_service.py:1863-1873`(claim)、`:1876-1901`(バッチループ)。対照は同 `:782`。

**症状**: 埋め込みは `openai_call_semaphore` を一切通らない(ファイル内で `openai_call_semaphore` の出現は `:782` の chat 経路のみ)。さらに HALF_OPEN プローブを**ループの外・スロット取得前**にクレームし、全バッチ完了まで保持する。

**帰結**:
1. 並列に複数機能が埋め込みを要求すると、バッチ数 × バッチ timeout だけ無制限に並走する。
2. HALF_OPEN 中は embdding がプローブを握ったままなので、同時に来た chat/埋め込みは `try_claim_circuit_probe` 失敗で 503 になる。

**発生条件**: サーキット HALF_OPEN 中に、未キャッシュテキストの多い埋め込みバッチが走ったとき。

**根本原因**: chat 側で確立した「プローブはスロット保持後に取る」不変条件を、embeddings 経路だけが守っていない。

**修正方針**: クレームをバッチ 1 回分のスロット取得後に移す。あるいは埋め込みをプローブ対象外にし、回復は `call_openai_chat` に委ねる。バッチ単位で chat と同じセマフォを取る。

**確度**: 高(コード)/ 影響 中

#### AI-02 HALF_OPEN プローブ claim の TOCTOU → 健全回路に 503

**位置**: `services/ai_service.py:1332-1349`、`market_state.py:341-357`(`try_claim_circuit_probe`)

**根拠コード**

```python
circuit_state = app_state.market.get_circuit_state("mistral")
if circuit_state.get("status") == "HALF_OPEN":
    if not app_state.market.try_claim_circuit_probe("mistral"):
        ... return {"error": {"message": "... probe in progress", "status_code": 503}}
```

```python
# market_state.py:341-357
if not target or target.get("status") != "HALF_OPEN":
    return False
if target.get("probing"):
    return False
target["probing"] = True
return True
```

**症状**: `try_claim_circuit_probe` は `status != "HALF_OPEN"` でも False を返す。`get_circuit_state` で HALF_OPEN を見た直後(ミリ秒窓)に別スレッドが成功して CLOSED へ戻ると、**健全な回路なのに 503** が返る。

**付随**: `probing` フラグに TTL がない。保持側が異常終了してクレームを解放し損ねると、そのサービスは HALF_OPEN のまま永久に 503 を返し続ける(AI-06 の切断経路が現実的な引き金)。

**修正方針**: claim 失敗時に `get_circuit_state` を再読し、CLOSED なら続行する。`probing` に最終更新時刻を持たせ、閾値超過で自動解放する。

**確度**: 中(競合窓の長さは未計測)

#### AI-03 非 JSON 200 でサーキットに success を先行報告

**位置**: `services/openai_service.py:1879-1883`

```python
if resp.status_code == 200:
    app_state.market.report_circuit_result("openai_compatible", success=True)
    circuit_probe_claimed = False
    data = resp.json()
```

**症状**: 上流が 200 + 非 JSON(プロキシ/CDN の HTML エラーページ等)を返すと、`resp.json()` の ValueError は下の `except Exception`(`:1957-1993`)へ落ちる。そこでは `resp_status is None` のため provider failure として計上されず、debug ログのみで次バッチへ進む。**回路は「健全」と記録され続け、全埋め込みが None のまま失敗し続ける**。

**修正方針**: JSON パース成功後に success を報告する。パース失敗時は failure 報告または明示的な warning。

**確度**: 中

#### AI-04 `_is_fallback` の多重意味がサーキットゲートを迂回させる

**位置**: `services/ai_service.py:2780`(セマフォ)、`:3153-3166`(内部リトライ)、`:1691-1693`(tier 制限リトライ)

```python
sem_ctx = nullcontext() if _is_fallback else app_state.ai.mistral_stream_semaphore
```

**症状**: 1 個のフラグが「セマフォ再帰回避」「tier/400 時の内部リトライ」「サーキットゲート回避」「プローブ会計回避」の 4 つの意味を兼ねる。結果として、403 tier 制限・400 reasoning 拒否の直後の内部リトライは、**サーキットが OPEN/HALF_OPEN でも無条件にプロバイダへ送られる**。

**修正方針**: フラグを分割(`_skip_semaphore` / `_internal_retry`)。ゲート判定とプローブクレームは内部リトライでも維持する。

**確度**: 中(リトライは直前の呼び出しが 4xx で終わった直後のため、回路が OPEN である公算は低い)

#### AI-05 キャッシュキーが温度正規化前に生成される

**位置**: `services/openai_service.py:685-694`(キー生成)、`:743-745`(payload 正規化)

**症状**: キーは生の `temperature` で作り、送信 payload は `_normalize_openai_temperature` 後。`3.2` と `2.0` は同一リクエストになるのに別キーでキャッシュされ、ヒット率が落ちる。

**補足**: `:743` の `ValueError` は `try:` より前にあるが、呼び出し元の温度はすべてリテラル(0.0 / 0.2 / 0.7)で、現状到達不能。したがって「契約迂回」ではなく**キー正規化順序の不整合**として NIT 相当の実害。

**修正方針**: 正規化をキー生成前に移す。

**確度**: 高

#### AI-06 クライアント切断時にプローブを解放するが成功報告しない

**位置**: `services/ai_service.py:3030-3052`、`:3159-3166`

**症状**: SSE 切断(GeneratorExit)時は `finally` で `release_circuit_probe` が走るため**クレームのリークはない**(この点は健全)。ただし `report_circuit_result(success=True)` は正常終了パスにしかないため、回路は HALF_OPEN のまま残る。以後の各要求が順にプローブを奪い合い、AI-02 の窓と組み合わさると 503 が散発する。

**評価**: 「中断された呼び出しを健全性の証拠にしない」という設計判断としては妥当。ただしそれを意図とするなら、AI-02 の `probing` TTL と併せて**回復経路が閉じないこと**を明示すべき。

**修正方針**: 最初の delta 送信に成功した時点で success を報告する、または「プローブ消費済み」を finally で記録する。

**確度**: 中

#### AI-07 ニュースのプレースホルダが `status=success` のまま UI に届く

**位置**: `services/news_service.py:378-397`(プレースホルダ返却)、`:400-410`(`_is_valid_news_bundle`)、`routes/api_analysis.py:1391-1408`

**根拠コード**

```python
return {"us": "解析中...", "jp": "解析中...", "trends": "解析中..."}
# 例外時
return {"us": "解析エラー", "jp": "解析エラー", "trends": "解析エラー"}
```

**症状**: LLM 応答が dict でない/スキーマ不一致のとき、上記プレースホルダを返す。`_is_valid_news_bundle` はこれを **invalid と判定する設計**なのに、ルートは `ok_response(result)` で 200 素通しする。UI(`static/js/api.js:1843-1866`)は content を 1 行として描画し、`_getStatusBadge("success")` の緑チェックを付ける。

**発生条件**: LLM が JSON でない応答を返したとき。`_is_cacheable_news_bundle`(`routes/api_analysis.py:1391-1408`)はこのバンドルを**キャッシュしない**が、**返却は止めない**。

**修正方針**: プレースホルダ経路では section の `status` を `error`/`degraded` に落とすか `content` を空にする。もしくはルートで 502 を返す。

**確度**: 経路 高 / 頻度 中

---

#### AIF-2 フォールバックのテーマ判定が部分文字列一致で誤マッチする

**位置**: `services/ai_portfolio_service.py:535`

```python
if preset_id == "tech" or any(k in theme_lower for k in ["半導体", "tech", "テック", "ai"]):
```

**症状**: no-key / LLM 失敗時のフォールバックがテーマ文字列を部分一致で分類するため、`"ai"` が `retail` / `airline` / `chain` / `email` / `training`、`"tech"` が `biotech` に誤マッチし、**無関係なテーマに「AI・テック成長株」ポートフォリオ(NVDA/MSFT/AAPL/GOOGL/6857.T)を返す**。UI にはテーマ名だけが表示されるので、ユーザーは自分が指定したテーマの結果だと誤認する。

**発生条件**: キー未設定または preview モードで、テーマに上記部分文字列を含む語を指定した場合(`retail` は小売テーマとして現実的な入力)。

**修正方針**: ASCII キーワードは語境界 `re.search(rf"\b{re.escape(k)}\b", theme_lower)`、CJK は従来の `in` を併用するハイブリッド。または `"ai"` を `"ai株"` / `"人工知能"` / `"ai "` に置換。

**確度**: 高(式は 1 行で閉じており、`\b` 不在は明白)

---

### 4.3 MINOR(データ層・フロント)

#### DATA-01 `_fast_get` が NaN を素通し

**位置**: `services/stock/provider.py:936-944`(取得)、`:1000`(整形)。対照 `:1113`。

**症状**: `:1000` の `cleaned` は `is not None` のみで判定するため、`float('nan')` がそのまま下流へ流れる。`:1113` は `sanitize_fundamental_dict` を通しており非対称。NaN は JSON シリアライズで `NaN`(非標準)になり、フロント側の数値整形が壊れる。

**修正方針**: `:1000` 側も `sanitize_fundamental_dict` に通す、または `math.isfinite` で落とす。

**確度**: 高

#### DATA-02 `_handle_pts_update` に `registration=` を渡していない

**位置**: `services/realtime/engine.py:827-832`。兄弟は `:510`, `:540`, `:546`。

**症状**: 価格更新ハンドラだけが registration 情報を欠いたまま呼ばれる。計測・監査の属性が欠落し、どのクライアント購読に紐づく更新か追跡できない。

**修正方針**: 兄弟と同じく `registration=registration` を渡す。

**確度**: 高(ただし影響はテレメトリ)

#### FE-01 bfcache 復帰で 3D ヒートマップが空白のまま

**位置**: `static/js/heatmap.js:1399-1423`。対照 `:200-204`, `:228-229`。

**症状**: `pagehide` で `removeEventListener("resize", _resizeHandler)`、`stop3DAnimation()`、`controller.abort()`、`controls.dispose()`、mesh 全破棄、`renderer.dispose(); renderer = null` を無条件実行する。一方 `state.three.isInit` を戻さず `pageshow` も登録しない。復帰後 `switchViewMode("3d")` は `if (!state.three.isInit)` を偽で通過し、`sync3DViewportSize()`/`start3DAnimation()` は renderer 不在で no-op。`canvas3d` の `hidden` は外れるため、**空白キャンバスが無警告で表示される**。

**発生条件**: `pagehide` が `persisted === true` で発火し bfcache 復元される場合(iOS Safari の戻る/進む等)。

**修正方針**: `cleanupHeatmapResources = (e) => { if (e?.persisted) return; ... }` + `pageshow` で `state.three.isInit = false` にして再初期化。

**確度**: 中(コード破綻は確実、顕在化は bfcache 依存)

#### FE-02 bfcache 復帰したスクリーナが「データをロード中...」を永久表示

**位置**: `static/js/screener.js:371-380`, `:427-433`, `:756-765`

**症状**: 取得開始時に `tdLoading.textContent = "データをロード中..."`。`pagehide` で `screenerAbortController.abort()`。catch 冒頭で `AbortError` ならローディング行を置換せず `return`。`pageshow` ハンドラは無い。復帰後の画面は**永久にローディング行**.

**修正方針**: `pageshow` で `if (e.persisted) fetchScreenerResults();`、または catch でローディング行をプレースホルダへ戻す。

**確度**: 高(経路が閉じている)

#### FE-03 遅延 GET 応答が入力中のプロンプトを上書き

**位置**: `static/js/settings.js:1356-1358`。対照 `:1384-1390`。

```javascript
if (data.custom_ai_prompt) { promptInput.value = data.custom_ai_prompt; updatePromptCounter(); }
```

隣接フィールドは `if (oaUrlInput && data.openai_base_url && !oaUrlInput.value)` と明示的にガードし、コメントにも「遅れて到着した GET 応答で上書きしない(編集内容の喪失を防ぐ)」と書かれている。**プロンプト欄だけが例外**。

**修正方針**: `!promptInput.value` を条件に加える。

**確度**: 高

#### FE-04 「マイポートフォリオへ反映」成功後に失敗トースト

**位置**: `static/js/ai_portfolio.js:1052-1080`

**症状**: `if (data.ok) { showToast("📥 ... 反映しました"); await fetchInitialStocks(); await loadPortfolioSnapshot(); }` — どちらかの再取得が reject すると同一 `try` の catch が `showAiPortfolioFailure(...)` を出し、**成功直後に失敗表示**になる。ユーザーが再実行すると `added_count === 0` になり得る(未検証)。

**修正方針**: 反映成功トースト後に `try { 再取得 } catch { showToast("反映は完了しましたが一覧の更新に失敗しました", ...) }` と分離。

**確度**: 高(データ破壊ではなく誤報)

#### FE-05 チャット送信に `signal` が渡らず最大 170 秒入力ブロック

**位置**: `static/js/api.js:2611-2621`(呼び出し側)、`static/js/api.js:2538`(`if (input.disabled || sendBtn?.disabled) return;`)。対照 `static/js/ui.js:3112-3121`(第 3 引数 `{ signal: requestController.signal }` を渡している)。

**症状**: `streamChatReply(payload, cb)` / `chatPollingReply(payload)` に options が渡らないため、中断手段が入力欄の disabled しかない。応答が返らないケースでは**最大 170 秒、新規送信も中断もできない**。

**修正方針**: `ui.js` と同様に AbortController の signal を伝播させる。

**確度**: 高

#### FE-06 `replaceStock` が `"stocks"` 通知を発火しない

**位置**: `static/js/state.js:300-310`

**症状**: 銘柄を置換しても購読者(`_notify("stocks")` 購読側)が更新されず、UI が古い銘柄情報を保持する。

**修正方針**: 置換後に `_notify("stocks")` を発火。

**確度**: 高

---

### 4.4 MINOR(フロント UI・可視化)

対象: `static/js/ui.js`(4,745 行/非空 4,371)、`chart.js`(2,303/非空 2,143)、`tradingview_manager.js`(1,047/非空 992)、`experimental/*` 9 ファイル(計 5,630 行/非空 5,050)。**本スライスに BLOCKER / MAJOR は 0 件**。

#### UI-1 詳細取得失敗バナーの削除が全銘柄に及び、他カードの再試行導線を消す

**位置**: `static/js/ui.js:202-203`。対照は同 `:243-244`。

```javascript
// Remove existing error banner if any (both locations, in case the drawer
// moved between renders and a stale banner survived in the inline panel).
const staleBanners = document.querySelectorAll(".detail-error-banner");
staleBanners.forEach((el) => el.remove());
```

**症状**: コメントの「both locations」は**当該銘柄の 2 箇所**(自カードの `.detail-inner` とドロワー)を意図しているが、セレクタは `document` 全体。銘柄 A の詳細取得が失敗して再試行バナーが出ている状態で別カード B を開き `ensureStockDetails(B)` が走ると、**A のバナーと再試行ボタンが無条件に消える**。A のパネルは「取得失敗」プレースホルダだけを残し、再試行手段がカードの開閉し直しに限定される。

**呼出元**: `ui.js:1722`(他カード操作)、`:261`(再試行ボタン自身)、`:3709`(ドロワー更新タイマー)。

**根本原因**: スコープ限定版の `bannerHost.querySelector(".detail-error-banner")`(`:243-244`)を**同じ関数内に既に持っている**のに、初期化側だけ全体削除にしている。

**修正方針**: `wrapper.querySelectorAll` と `#drawerTabChartContent` 配下に限定する。または `el.closest(".stock-wrapper") === wrapper || el.closest("#drawerTabChartContent") === drawerHost` で絞る。

**確度**: 高(コード現物一致)。カードの開閉し直しで回復するため機能停止ではない → MINOR。

#### OBS-01 サイレント定期更新がユーザー由来の読込を中断し、ローディングオーバーレイが無表示・無リトライのまま残留し得る

**位置**: `static/js/experimental/orbit-entry.js:551-553`(中断)、`:566-567`(表示)、`:666`・`:672-679`・`:688-700`(解除されない経路)、`:682`(成功時のみ解除)。

```javascript
if (loadAbortController) {
  loadAbortController.abort();
}
...
if (!silent) setLoading(true);
```

**症状**: 中断は**無条件**で `silent` を見ない。中断されたユーザー由来の要求は `catch` の `err.name !== "AbortError"` 判定(`:687`)で全スキップされ、`setLoading(false)` に到達しない。引き継いだサイレント要求も、**失敗**時(`:688-700` は `console.error` のみ)、**空一覧**時(`:672-679` は `console.warn` して `return`)、**世代不一致**時(`:666`)には解除しない。

**結果**: `setLoading(true)` が外してい `hidden` を戻し `inert` も外しているため(`:704-719`)、**操作可能なオーバーレイが中央カードを覆ったまま**残る。`errorOverlay` は出ないので再試行ボタン(`:744`)も使えない。復帰は次のサイレント更新成功(最大 60 秒、`:782-783`)か再読込のみで、失敗が続けばそのまま。

**対照実装**: 同リポジトリの修正済み経路 `static/js/heatmap.js:555-558` は `finally` で解除している。**未修正側と修正済み側が同居**している。

**修正方針**: `catch` を `finally` にし、「自世代の所有者」または「ユーザー可視要求」だけが `setLoading(false)` を呼ぶ。

**確度**: 高(コードは現物一致)/ 到達はサーバ障害・空応答と重なる条件付き。

#### OBS-02 rAF ループが `render()` の例外で恒久停止する(再予約が後置)

**位置**: `static/js/experimental/orbit-renderer.js:93-108`。

```javascript
this._frameLoop = (now) => {
  this.animId = null;
  this.render(now);                       // ← 先に描画
  if (!this._isRunning) return;
  if (typeof document !== "undefined" && document.hidden) return;
  if (this._shouldPauseLoop()) { ... return; }
  this.animId = requestAnimationFrame(this._frameLoop);   // ← 後で再予約
};
```

**症状**: `render(now)` が投げると `animId` は `null` のまま再予約に到達せず、ループは二度と再開しない。`git grep` で確認したところ**このファイルには `try {` が 1 つも存在しない**(例外隔離ゼロ)。`render` はサーバ由来の stocks / history / springs を約 1,000 行描画するため、不正データ 1 件で「canvas が静止し、コンソールに 1 行だけ」という無言の機能停止になる。

**修正方針**: `try { this.render(now); } finally { /* 予約と停止判定 */ }`。

**確度**: 高(機構)/ 低(描画例外を起こすデータの実在証拠は未取得)。

---

### 4.5 NIT

- **AI-08** `services/ai_service.py:3021-3025`: ワーカーは `{"type":"done","result":res}` しか積まない(`:2898`)ため `item.get("usage")` は常に None。実害なし(`getattr(res, "usage", None)` 側で計上される)。防御コードとしては妥当だが、意図をコメントで書くべき。
- **AI-09** `services/ai_service.py:1795` = `min(..., 10)` ↔ `services/openai_service.py:1484` = `min(..., 8)`: 同一パラメータ名 `max_tool_iterations` のクランプがプロバイダで非対称。9〜10 を指定すると挙動が変わる。共通定数へ。
- **AI-10** `services/embeddings_service.py:60-66`: `texts=None` で `not texts` が真 → `[None] * len(None)` が TypeError。同関数内の他ガード(型検査・bool 除外)と粒度が非対称。`if not isinstance(texts, list): return []`。
- **AI-11** `services/embeddings_service.py:96-116`, `:205-222`: キャッシュ内リストを参照共有で返す。現状の呼び出し元は `cosine_similarity` のみで非破壊のため実害なし。`tuple(emb)` で格納するか返却時にコピー。
- **DATA-03** `utils/market_utils.py:470`: 末尾 `return True` は fail-open だが、`StockHistoryQueryRequest.market`(`schemas/stocks.py:445`)が `Literal["us","jp","idx"]` + `:455-462` の strip/lower 正規化のため**到達不能**。防御としては妥当、硬化余地のみ。
- **DATA-04** `utils/market_utils.py:373-376`: タイムゾーン情報が取れない場合のフォールバックが `-5h` 固定。EDT 期間は 1 時間ずれる。JP の `+9h` は妥当。
- **DATA-05** `utils/market_utils.py:78-91`: Happy Monday と振替/国民の休日は実装済みだが、固定日テーブルが 2020/2021 五輪特例・2019 即位の日・2016 以前の山の日・2020 以前の天皇誕生日を扱わない。**過去日付のみ**不正確。
- **FE-07** `static/js/settings.js:1278-1292`: `openaiDiscoveredModels.textContent = ""` が成功分岐の内側にのみ存在。接続テスト失敗時に旧モデルチップがエラー表示と併存し、誤ったモデル名入力へ誘導。
- **FE-08** `static/js/screener.js:418-421`: `countEl.textContent` は成功パスのみ更新。失敗時に「検索結果: 120 銘柄」とエラー行が併存。
- **FE-09** `static/js/settings.js:933-935`, `:1441-1443`, `:1591-1593`: 状態メッセージ消去の `setTimeout(..., 3000)` が 3 箇所とも匿名でハンドル破棄。3 秒以内の再保存で新しい表示が古いタイマーに消される。
- **FE-10** `static/js/index_main.js:984-992`: `getAlertsConfig()` が描画毎に `JSON.parse(localStorage.userAlerts)` を実行。
- **RT-01** `services/realtime/scrapers.py:1252`: 未使用変数 `workers`。
- **RT-02** `services/realtime/utils.py:514-521`: ブレース走査が非有界。
- **RT-03** `trend_sources.py:62-63`: 6 ワーカーの非有界 executor、`TrendReq` にタイムアウト指定なし。上流遅延時にスレッドが積み上がる。
- **MAP-01** `utils/tradingview_mapper.py:410-417`: `BTC-USD` を `NYSE:` に割り当て(正しくは暗号資産取引所)。
- **STOCK-01** `services/stock/provider.py:1337`, `:1352-1360`: `(content.get("summary") or "")[:200]` で企業概要を無言切り詰め。UI は「全文」として描画。
- **MDS-01** `services/market_data_service.py:148-152`: `sector`/`name` の型検証なし。上流が dict/list を返すと下流の文字列連結で壊れる。
- **API-01** `routes/api_analysis.py:2360-2411`(`_analyze_v2_error_response`): 同一関数内で方針が混在する。504 timeout と 503 queue.Full は**固定文言**(`:2361`, `:2379`)、503 network と 500 data は `f"AI分析内部エラー: {err_msg}"` / `f"AIサービス通信エラー: {err_msg}"` と**例外文字列を echo**(`:2403`, `:2410`)。
  - **漏洩リスクは低い**: `err_msg` は `_sanitize_error_message(str(job_err))`(`utils/text_utils.py:250-268`)を通り、`api_key` / `token` / `password` / `authorization` / `bearer` / `sk-...` / `tvly-...` が `[REDACTED]` 化される。さらに `error_response` は `_sanitize_error_value` で `details` のネスト文字列もサニタイズする(`utils/stock_payload.py:1424-1440`、API-10 修正済み)。
  - **残る問題**: サニタイズ後でもファイルパス・モデル名・例外クラス名(`error_type`)が UI に表示され、同じ関数の他分岐と挙動が食い違う。フロントは `details.reason` をそのまま描画する(`static/js/api.js:185-215`)。
  - **修正方針**: 全分岐を固定文言 + `error_code` + `request_id` に揃え、例外詳細はサーバログのみに残す。
- **OPS-01** `utils/stock_payload.py:1334`(`ok_response`), `:1388`(`error_response`): 応答エンベロープは**すでに単一 owner に集約されている**(`error_response` の docstring に「the **single owner** of the error envelope (H-3)」と明記、`error_handlers._build_error_response()` もここへ委譲)。したがって「集中ヘルパーなし」は成立しない。残る観察は 2 点のみ:
  - 配置が `utils/stock_payload.py`(株価 payload 整形モジュール)であり、HTTP 応答の owner としては層が不自然。
  - `static/js/api.js:139` は `errorBody?.error || errorBody?.message` と読むが、現行エンベロープは**両方を同一の `safe_message` で埋める**(`utils/stock_payload.py:1435-1437`)ため `||` の第 2 オペランドは実質死コード。
- **CACHE-01** `utils/disk_cache.py:466`, `:476`: `max_ttl = max(self._default_ttl, 86400)` は読み取り TTL のクランプではなく、**バックグラウンド掃除の経過判定**(`if age > max_ttl`)に使われる。コメントどおり「default_ttl(7200s)を超える custom TTL のファイルを早すぎる削除から守る」ための値。ただし上限も 86400 で**打ち切られる**ため、24 時間を超える TTL を指定した呼び出しがあれば、まだ有効なエントリが掃除で消える。現状の `stock_payload.py:365` は `disk_key` を 86400 で書くため**潜在**に留まる。
- **CFG-01** `schemas/config.py:250`: `mistral_api_key_min_length: int = 32` は、同ファイル `:14` で import し `:146` でバリデーションに使っている `MISTRAL_API_KEY_MIN_LENGTH` を**参照せずリテラルで再記述**している(二重定義ではなくリテラル重複)。定数側だけ変更すると応答フィールドが黙って古い値を返す。`langsearch_api_key_min_length: int = 20` / `tavily_api_key_min_length: int = 5` も同様。
- **CFG-02** `config/io.py:90-96`: ファイル読み取り毎に tmp ディレクトリを掃引。読み取りホットパスに I/O が増える。
- **LOG-01** `logging_config.py:364-366`: `log_dir.mkdir(parents=True, exist_ok=True)` に **mode 指定がなく**、ディレクトリは umask 依存の既定権限で作られる。対照としてファイル側は `_create_restricted_file_handlers` が `os.chmod(self.baseFilename, 0o600)` を強制し(`:22-35`)、`:371-374` のコメントも「create them 0o600 like the config / user_stocks / DB files」と明言している。**ファイルは守られ、それを入れる箱は守られていない**という非対称。ただし Windows では POSIX mode が実質無視されるため、実害は POSIX 配備時の他ユーザーによるディレクトリ列挙に限られる。
- **DOC-01** `pyproject.toml:21`: コメントが「the suite grew to 2100+ tests」とするが、実測は 4,735(collect 4,735 / junit tests=4,782)。2 倍以上の乖離。
- **EXT-01** `chrome_extension/content.js:390-393`: 重複条件と全角括弧の未認識。
- **EXT-02** `native_host/install_host_windows.cmd:3`: 非修飾の `PowerShell` 呼び出し(PATH 依存)。
- **CI-01** `.github/workflows/dependabot-sync.yml`: `pull_request_target` + `permissions: contents: write` + `ref: ${{ github.head_ref }}` + `git push`。ガードは `github.actor == 'dependabot[bot]' && startsWith(github.head_ref, 'dependabot/uv')` のみ。**actor は偽装不可**、dependabot は自リポジトリのブランチを使うため現状の実害はない。硬化として `head.repo.full_name == github.repository` を条件に加える。
- **OPS-02** `/api/cache-stats`, `/api/metrics`, `/api/system/ai-usage`: バックエンドに実装がありテストも参照するが、フロントからは未消費。意図的な運用面エンドポイントか dead code かは要判断。
- **TEST-01** 第 2.3 節の 9 ファイル(44.0〜70.5%)。特に `config/atomic_write.py`(46.0%)は書き込み原子性の要。
- **UI-2** `static/js/ui.js:140-153`: `beforeunload` のクリーンアップで `typeof stockDetailDrawerRefreshTimer !== "undefined"` と `typeof aiDrawerAbortController !== "undefined"` をガードにしているが、宣言は `let`(`:3461` / `:2902`)で**ハンドラ(`:130`)より後方**。`let` 束縛は TDZ 中の `typeof` 参照で `ReferenceError` を投げるため、**ガードとして機能しない**。到達条件は「スクリプト評価が `:3461` 前にトップレベル例外で中断し、その後 `beforeunload` が発火する」場合に限られ、通常運用では起きない。同ハンドラの `cardIntersectionObserver` だけは `:34` 宣言で正しく機能している非対称。宣言を前に移すか各枝を `try/catch` で隔離。
- **UI-3** `static/js/ui.js:190-192`: `findDetailEl` のフォールバック `document.querySelector(\`#stock-detail-drawer ${sel}\`)` に**所有銘柄の判定がない**。ドロワーに別銘柄が開いている状態で自カードの取得が走ると「取得中.../取得失敗」が他銘柄のドロワー要素へ書かれ得る。通常操作では到達しない(`openStockDetailDrawer` は切替時に必ず `closeStockDetailDrawer()` を先に実行し `:3561-3565`、close が owned 要素を `:scope >` 限定で復元 `:3758-3808`)。`isDrawerActive`(`:232-237`)を条件に加える。
- **UI-4** `static/js/ui.js:1399-1401`, `:4726`, `:4734`: `e.target.closest(...)` を無条件に呼ぶ。`target` が `Element` でない合成イベントでは `TypeError`。リポジトリ内に該当生産者は存在しない(`dispatchEvent` は `api.js:1983`/`:2055`、`index_main.js:804` のみ)。`if (!(e.target instanceof Element)) return;`。
- **OBS-03** `static/js/experimental/temporal-controller.js:122`, `:130`: `timeNowBtn` / `timePlayBtn` の click が**匿名関数**で登録され、`destroy()`(`:222`)は `_wheelHandler` 等のフィールド化されたものだけを解除する(`:233-234`)。destroy 後の押下で 250ms の `setTimeout` 鎖が再起動し得る。同型は `accessibility-controller.js:29,36,43,49`、`ai-dive-controller.js:73,79,87`、`constellation-controller.js:24,30,38`、`orbit-entry.js` の 12 箇所。ハンドラをフィールド化して `destroy` で対称解除。
- **OBS-04** `static/js/experimental/orbit-state.js:64-65`: `_historyAbortControllers = new Map()` は読み書きともゼロ、`_aiAbortController = null` は `:251-253` で `abort()` と `null` 代入を読むだけで**代入箇所が存在しない**(実際の中断は `ai-dive-controller.js:135-136`, `:196-198` が所有)。将来「abort している」と誤認させる罠。
- **OBS-05** `static/js/experimental/orbit-entry.js:799`: teardown が `beforeunload` のみ。60 秒 `setInterval`(`:782-783`)と rAF はここでしか止まらないため、bfcache / iOS Safari の back-forward では停止しない(`heatmap.js` は `pagehide` 対応済み)。
- **OBS-06** `experimental/ai-dive-controller.js:158`、`constellation-controller.js:154`, `:243`、`orbit-entry.js:235`: `data.stocks.get(...)` を無検査で呼ぶ。同ファイル `ai-dive-controller.js:114-117` だけが `typeof data.stocks.get === "function"` でガードしており非対称。`orbit-state.js:37` が常時 `new Map()` を入れるため現状到達不能。
- **OBS-07** `experimental/orbit-state.js:44`: コメントは `timeCursor: 0, // 0 = latest/live, negative integer = historical steps back` だが、`setTimeCursor`(`:179-184`、`:180` で `Math.max(num, 0)`)で負値を丸め、消費側は `> 0` を過去方向として扱う。実装は「0=live、正=過去」でコメントが stale。
- **OBS-08** `experimental/accessibility-controller.js:373-382`: `_modalReturnFocusTarget`(`:15` で初期化、`:375` で代入、`:380-381` で復帰)を help / search で共有しており、入れ子時に復帰先が失われる。`getOpenModal()`(`:272`)により入れ子は実質到達困難 → 確度 低。
- **VER-01** `scripts/verify_ui_safety_contracts.mjs:141-156`, `:170-183`: message リスナの origin 検証を「登録行から 60 行以内に `origin` という語があるか」で判定する(`let body = lines.slice(index, index + 60).join("\n");` / `if (!/\borigin\b/.test(body))`)。コメントや無関係な変数名でも通るため、**検証スクリプト自身が偽陰性を出し得る**(プロダクト挙動には影響しない)。section 4 も 40 行ウィンドウ内の `sender.id !== chrome.runtime.id` 完全一致前提。比較式(`!==`/`===` 付き)の要求か、関数抽出失敗時に fail させる形に。
- **VER-02** `scripts/verify_stock_update_signature.mjs:111-134`: 純関数を同一入力で 2 回呼んで等値比較する**恒真アサーション**。名前が示す「unchanged frame is suppressed(変化なしフレームは再描画しない)」という抑制ロジックを一切検証していない。実 `ui.js` の更新判定を fake DOM で呼び `textContent` の書換回数 0 を観測するか、アサーション名を「signature is deterministic」に正直に改める。
- **VER-03** `scripts/verify_generated_frontend.mjs:50-60`: 生成物不一致で `process.exitCode = 1` を立てた後も `verifySseReconnectGuard()` を続行し、失敗状態のまま後続チェックの成功ログを出す。終了コードは 1 のままなので **CI の偽成功は起きない**が、ログを読む人間を誤導する。
- **NH-1** `native_host/native_host.py:488-504`: 許可拡張 ID 集合のキャッシュを `st_mtime` のみで判定するため、mtime を保ったまま内容を書き換えられると古い許可集合を返し続ける(逆に同一 mtime 分解能内の編集は検知できない)。Windows の FAT/ネットワーク共有上の書換で成立し得るが、攻撃前提は「ホストのマニフェストファイル書換権限」であり、その権限があればプロセス起動引数も直接制御できるため実害は増えない → NIT 扱い。

---

## 5. 反証・棄却した所見(報告書に載せない)

子エージェントの自己申告のうち、以下は現物コードで反証したため**課題として採用しない**。

| 棄却した主張 | 反証根拠 |
|---|---|
| `bg/sse_interpolator.py` のインデント異常(BG-INT-01) | 実測で `still_ours` は同一 `if any(...)` 内。誤読。 |
| プローブ未解放(A-4 の当初解釈) | `services/ai_service.py:3212-3219` の `finally:` が `release_circuit_probe` を実行。リークなし(AI-06 として「成功報告なし」に再定義)。 |
| agents ストリームで usage 未計上(A-2) | `last_usage` は `:2836` 初期化・`:3028` 代入・`:3058-3059` 使用。計上経路は生きている(AI-08 として「死分岐」に格下げ)。 |
| 埋め込みキャッシュの無制限成長(A-8) | `_EMBEDDINGS_CACHE` は `LRUCache(maxsize=1024)`。読み取りのみ(AI-11 として参照共有のみ起票)。 |
| nonce 使い捨て(N-03) | `utils/networking.py:470-482` の docstring に「removed on any attempt so it cannot be replayed」と明記。意図的仕様。 |
| `disk_cache` の RLock 再入 | `utils/disk_cache.py:271` で再入可能。問題なし。 |
| gunicorn `-c <config>` による `num_workers` 上書き | `gunicorn.conf.py` の `on_starting()` が `num_workers > 1` で `sys.exit(1)`。 |
| `fetchWithTimeout` の事前 abort 未処理 | `static/js/utils.js:938-942` で処理済み。 |
| `markStreamAlive` の停止条件不成立 | `api_client.js:288-330` で成立。 |
| `options?.signal` はデッドコード | 誤り。`static/js/ui.js:3112-3121` が実際に渡している。 |
| `_missing_required_arguments` の欠落 | `git grep` 0 件。**幻の所見**。 |
| `analyzed_at` の `new Date()` 失敗 | バックエンドが ISO8601 を返すため成立しない。 |
| `news_formatter` の末尾切り詰め | `tests/test_news_formatter.py:64-72` が挙動を明示 assert。仕様。 |
| `load_saved_ai_portfolios()` が破損時に `[]` | docstring と save/delete 側の raise 分岐で意図的。 |
| `sanitize_ai_portfolio` の重みクランプ | `max(0.0, min(100.0, w))` は数学的に実害なし。 |
| `news_service` の部分成功時 `retrieve_status["trends"]="success"` | コメントで意図明記(部分成功)。 |
| `resp` 未 close(埋め込み) | httpx 非ストリームで接続返却。`_close_call_owned_openai_client` は call-owned のみ close する設計(`openai_service.py:230`)。 |
| `execute_mistral_tool_call(tool_name, arguments: dict \| str)` の str 許容 | 正規契約。欠陥なし。 |
| `api_tools` の索引写像崩れ | 破綻せず。 |
| langsearch 50 件 rerank index 写像 | 破綻せず。 |
| フロント XSS / CSP nonce 欠落 / TradingView postMessage | `insertAdjacentHTML`等の sink 0 件、全テンプレートで nonce 付与確認、`tradingview_manager.js:897-913` が origin 3 種許可リスト + `event.source === iframe.contentWindow` を検証。 |
| テンプレート↔JS の ID 契約不一致 | screener 12 ID + `data-colspan-full="8"`、heatmap の `heatmap-*`/`cam-*`/`view-*`/`size-*` を両側突合し欠落なし。`sort_by` は `schemas/stocks.py:34-44`、`totalFiltered` は `routes/stocks/views.py:280` と一致。 |
| API キー長のクライアント側未検証 | サーバ `schemas/config.py:139-152` が `cred_bad_key\|<field>` を返し UI が `details.reason` を表示。契約はサーバが正。 |

### 5.1 自分のドラフトから撤回・降格したもの

初稿に書いたが、現物裏取りで成立しないと判明したため修正した項目。記録として残す。

| 当初の記述 | 実際 | 現在の扱い |
|---|---|---|
| エラー応答に集中ヘルパーがなく、`jsonify({"error"...})` が散在(OPS-01) | `utils/stock_payload.py:1334 ok_response` / `:1388 error_response` が単一 owner。`routes/*.py` の素の `jsonify({"error"` は 0 件 | **撤回**。残る観察は owner の層の不自然さと `api.js:139` の死コードのみに縮小 |
| `disk_cache.py:466` の `max(default_ttl, 86400)` が読み取り TTL を打ち切る(CACHE-01) | 同値は `:476` のバックグラウンド掃除の経過判定に使われるのみ。読み取り時の有効性判定ではない | 記述を修正。24h 超 TTL 設定時のみ顕在化する**潜在**問題として起票 |
| `app_factory/factory.py` に空コメント残骸(DOC-02) | 同ファイルに該当行なし。`app_factory/hooks.py:146` の素の `#` はコメントブロック内の段落区切り | **撤回**(リストから削除) |
| `_analyze` のエラー文字列 echo が API キー漏洩につながる(API-01=MINOR) | `_sanitize_error_message` (`utils/text_utils.py:250-268`) と `error_response` の再帰サニタイズ(`utils/stock_payload.py:1424-1440`)を通る。漏洩リスクは低い | **NIT へ降格**。実問題は同一関数内の方針混在のみ |
| ログディレクトリが `0o755` で作られる(LOG-01) | `logging_config.py:364-366` は mode 無指定(`exist_ok=True` のみ)。ファイル側は `chmod 0o600` | 記述を修正(「箱は守られていない」) |
| `schemas/config.py:250` の API キー最小長が二重定義(CFG-01) | `MISTRAL_API_KEY_MIN_LENGTH`(`:14` import / `:146` 使用)を参照していないリテラル `32` の重複 | 記述を修正(「二重定義」→「リテラル重複」) |
| `ui.js` 4,747 行 / `orbit-entry.js` の匿名 listener 11 箇所 / `orbit-state.js:41` の `new Map()` | 実測 4,745 行 / 12 箇所 / `:37` | 数値を実測値に置換済み |

---

## 6. 既知レビュー文書との重複排除

本リポジトリには過去ラウンドのレビュー文書が存在する。以下は**重複起票を避けるために対照済み**で、上記リストには新規所見のみを載せている。

| 文書 | ID 体系 | 本リストとの関係 |
|---|---|---|
| `plans/current_head_code_review_report.md` | R1〜R48 | HEAD で修正済みのものを含む。重複なしを確認。 |
| `docs/CODE_REVIEW_2026-10-21.md` | G-01〜06 | G-05(`MNS_ALLOW_CLIENT_API_KEY`)・G-06(`/api/shutdown` 別系統トークン)は意図的仕様として除外継続。 |
| `ALL_FINDINGS_AND_FIX_PLAN.md` | ISSUE-01〜12 | ISSUE-10/11/12 は HEAD で修正済み。 |
| `REVIEW_FINDINGS_2026_11_20_COMPREHENSIVE.md` | N20-01〜06, N21-01/02 | N20-05(pagehide クリーンアップ)は**修正済みだが bfcache 復帰経路が欠落** → FE-01/FE-02 として残存欠陥を起票。N21-01(`storage.py:36-45`)・N21-02(`storage.py:571-593`)は修正済み。 |
| `REVIEW_FINDINGS_2026_11_20_RESIDUAL.md` | R20-01/02 | 修正済みを確認。 |
| `tests/test_comprehensive_code_review_2026_11_21_fixes.py` | AI-01 / UI-01 / UI-02 | 修正済みを確認(`#ai-portfolio-view`、`index_main.js` 世代ガード)。 |
| 既知修正の再確認 | — | `fallback_warning` サニタイズ、ニュース LLM キャッシュキーのモデルスコープ、`openai_service.py:1897` の埋め込み usage 計上 — いずれも HEAD で修正済み。 |
| AIF-1 の未報告確認 | — | `git grep -n "force_rebalance" -- plans docs` は 0 件、`existing_saved` / no-key 分岐に関する記述は既知文書に存在しない。`fallback_warning` 関連の既存修正(ISSUE-02 / R系)はすべて**サニタイズ層**の話で、persist 呼び出しの非対称には触れていない。 |
| VER-01〜03 / NH-1 | — | `scripts/verify_*.mjs` の内部品質と `native_host` のキャッシュ判定は、既知文書がいずれも「スクリプト自体」を対象にしていないため重複なし。 |

---

## 7. 精査カバレッジ台帳

「抜粋ではなく全ファイル」という要求に対する証跡。**読了範囲を正直に記載し、未了は未了と書く。**

| 領域 | 規模 | 状態 |
|---|---|---|
| `app_factory/` | 7 files / 929 行 | 全読(rev-infra) |
| `routes/` | 21 files / 7,601 行 | 全読(rev-api)。route 定義 46 件を機械的に突合 |
| `schemas/` | 5 files / 1,527 行 | 全読(rev-api) |
| `services/` | 30 files / 16,543 行 | スライス全読(rev-ai / rev-data / rev-stock-provider / rev-realtime / rev-gap)。**rev-ai は 5 ファイル未読を自己申告** → `ai_portfolio_service.py` / `news_formatter.py` / `search/__init__.py` は追加スライスで補完、`news_service.py` / `search/langsearch.py` は rev-gap が精読済み |
| `utils/` | 17 files / 9,283 行 | 全読(rev-utils + rev-data) |
| `config/` | 8 files / 1,807 行 | 全読(rev-infra) |
| `bg/` | 11 files / 2,921 行 | 全読(rev-realtime) |
| `static/js/` | 25 files / 36,668 行 | 4 スライスで分担(ui-core / ui-feature / fe-chart / fe-ui)。`ui.js` 4,745 行(非空 4,371)・`api.js` 3,119 行は全読 |
| `templates/` | 7 files / 3,291 行 | base / index / heatmap / screener は全読。**settings / setup / experimental_orbit の本文は追加スライスで補完** |
| `chrome_extension/` | 6 files / 3,284 行 | 全読(rev-native) |
| `native_host/` | 15 files / 45,047 バイト(実測 ~2,800 行) | 追加スライスで中核モジュールを精読 |
| `scripts/` | 6 files / 1,512 行 | `verify_*.mjs` 6 本を追加スライスで精読 |
| `tests/` | 397 files / 102,163 行 | **精査対象ではなく契約証拠として参照**。テスト自体の品質は付随所見のみ |

**未精読の残差**: `scripts/verify_*.mjs` 6 本、`templates/settings.html` / `setup.html` / `experimental_orbit.html`、`ai_portfolio_service.py` / `news_formatter.py` / `search/__init__.py` は追加スライス(rev-gap2)で精読完了。残る未精読は `native_host/diagnose_native_host_windows.ps1`(診断専用)と `static/js/ai_portfolio.js` の一部のみ。

---

## 8. 制約・未検証事項

1. **動的検証は未実施**。サーバ起動・ブラウザ操作・負荷試験・bfcache 実機再現はいずれも未実施。FE-01/FE-02 の顕在化は bfcache 実装依存で、コード上の経路は閉じているが実機確認は取れていない。
2. **M-01 の発生頻度は未計測**。quoteSummary の一時失敗率に依存するため、静的な経路検証のみ。
3. **AI-02 の競合窓の長さは未確定**。マイクロ秒か数百ミリ秒かで重要度が変わる。
4. **AI-07 の頻度は LLM 出力依存**で実測不能。頻発するなら MINOR → MAJOR へ昇格しうる。
5. **`tests/` 102k 行は精査対象外**(契約証拠として参照)。テストコード自体の空虚なアサーションは `scripts/verify_*.mjs` のみ対象にした。
6. **フロントの残差**: `ai_portfolio.js` の約 1,780 行は子エージェントの全読報告に依拠しており、私が直接読んだのは `1035-1084` と全体 grep のみ。
7. 過去ラウンドで意図的仕様・後方互換として除外された項目(G-05 / G-06)は再対象にしていない。F.3-2 の `err_msg` echo のみ「宣言と実装の矛盾」として API-01 で起票。
8. **AIF-1 は静的解読のみで実行再現していない**。`preview_mode: true` でリバランスを 2 回叩き保存件数が増えることの実測は未実施(テスト実行環境でサーバを起動していないため)。コード経路・ID 置換ロジック・上限切り捨て・設計コメントの四点が閉じているため確度は高いが、「実機でデータが消えるのを見た」わけではない。
9. **`scripts/verify_*.mjs` の指摘は「検証スクリプトの検出力」に対するもので、プロダクトの挙動には影響しない**。VER-01 は偽陰性の可能性、VER-02 は恒真アサーション、VER-03 はログの誤導のみ(終了コードは正しい)。

---

## 9. 推奨対応順序

1. **AIF-1**(`services/ai_portfolio_service.py:916-928`)— ユーザーの保存済みデータを無告知で破壊する唯一の所見。no-key 分岐に `existing_saved` ガードを入れ、ID 採番を `fallback_target_id` 経由に統一。回帰テスト 1 本(キー空で 2 回 → 件数不変・items 保持)。
2. **M-01**(`utils/stock_payload.py` + 回帰テストの fake 修正)— データの正しさに直結。
3. **AI-03 → AI-01 → AI-02 → AI-06**(サーキットブレーカーの会計)— 可用性と課金。`probing` TTL と claim 再読をまとめて 1 パッチに。
4. **API-01**(内部文言の echo 停止)— 小さく独立、情報漏洩面。
5. **FE-04 → FE-03 → FE-05**(ユーザーに見える誤報・操作不能)— 影響が明確で修正が小さい。
6. **FE-01 / FE-02**(bfcache)— N20-05 の残存欠陥として 1 パッチにまとめる。
7. **UI-1 / OBS-01 / OBS-02** — 誤報と永久停止。`OBS-02` は `try/finally` 化のみで直る。
8. **DATA-01 / DATA-02** — 下流の数値整形とテレメトリ。
9. **AI-04 / AI-05 / AI-07 / AIF-2** — フラグ分割、キャッシュキー正規化、語境界判定。
10. **NIT 群** — `config/atomic_write.py` のテスト追加(TEST-01)を先に。`OPS-01` のエラーエンベロープ統一は横断変更になるため最後。
11. **CI-01** は硬化のみ(実害なし)。`VER-01〜03` は検証スクリプトの検出力なので、上記修正の回帰網を強める意味で先行させる価値がある。

---

*本ドキュメントはコードを変更していない。すべての所見は HEAD `f72e297` の現物コードに行番号付きで紐づき、MINOR 以上はすべて私が該当行を直接読んで確認した。*
