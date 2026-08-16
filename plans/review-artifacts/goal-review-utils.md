# utils 領域 自律レビュー結果

- レビュー実施日: 2026-08-16
- 対象HEAD: 現在の作業ディレクトリ（`c:\Users\mibu0\Documents\develop\mistral_nex_stocks_complete_fixed_v3\mistral_nex_stocks_complete_fixed_v3`）
- モード: レビューのみ（コード変更なし）
- 未コミット差分（`static/css/index.css`, `static/js/ai_portfolio.js`, `templates/index.html`）は変更・破棄していない
- レビュー範囲: `utils/` 配下の全ファイル（`__init__.py`, `caching.py`, `chat_history.py`, `disk_cache.py`, `env_helpers.py`, `formatting.py`, `http_utils.py`）
- 参考確認: `app.py`, `services/`, `routes/`, `session_manager.py`, `market_state.py`, `ai_state.py`, `app_state.py` の該当箇所

## レビュー結果サマリー

### 問題候補数
- 確定候補: **3件**（内訳: Critical 0 / High 0 / Medium 3 / Low 0）
- 要確認: **1件**

### 実行テスト（裏取り）
指定されたテスト群のうち、存在するファイルのみ実行。全てパス（exit code 0）。

```bash
uv run --locked --group test python -m pytest \
  tests/test_disk_cache.py tests/test_disk_cache_extra.py tests/test_chat_history.py \
  tests/test_formatting.py tests/test_networking_extra.py tests/test_config_utils.py \
  tests/test_config_utils_extra.py tests/test_input_validation.py \
  tests/test_coverage_utils_extra.py tests/test_coverage_pure_extra.py \
  tests/test_rate_limiting.py -q --timeout=60
```

- 結果: 216 passed（内訳: `test_disk_cache.py` + `test_disk_cache_extra.py` + `test_chat_history.py` + `test_formatting.py` + `test_networking_extra.py` + `test_config_utils.py` + `test_config_utils_extra.py` + `test_input_validation.py` + `test_coverage_utils_extra.py` + `test_coverage_pure_extra.py` + `test_rate_limiting.py`）
- 注: 指示に挙げられた `tests/test_caching.py`, `tests/test_http_utils.py`, `tests/test_env_helpers.py` は **リポジトリに存在しない**（`pytest` が `file or directory not found` で終了コード4を返すため、存在ファイルのみで実行）
- 補足: 上記テストは全てグリーンであり、**以下に挙げる問題は既存テストでカバーされていない未検証の経路**であることを示唆する。

### 静的プローブによる実測確認（裏取り）
`uv run --locked --group test python` による実測で以下の挙動を確認:
1. `sanitize_cache_key("search_a!b") == sanitize_cache_key("search_a_b")` → 同一キー `search_a_b` に衝突（再現確認済み）
2. `parse_retry_after(SimpleNamespace(headers={"Retry-After": "inf"}))` → `inf` を返す。`int(max(300, inf))` は `OverflowError`（`session_manager.py` 経路でレート制限処理がスキップされる）
3. `StockDiskCache.get()` が正しいJSONだが list 形状のキャッシュファイル（`[]`）に対して `AttributeError: 'list' object has no attribute 'get'` を送出（例外ハンドラ `(json.JSONDecodeError, OSError, KeyError)` が `AttributeError` を捕捉しない）

---

## 確定問題候補

### [UTIL-1][Medium] 問題候補: `parse_retry_after` が非有限値（NaN/Infinity）および巨大値をクランプせず返す

- 該当箇所: [`utils/http_utils.py`](utils/http_utils.py:51)-[`utils/http_utils.py`](utils/http_utils.py:58)
- 影響経路:
  1. `float(raw)` は `"inf"` / `"Infinity"` / `"nan"` / `"NaN"` を `inf` / `nan` として受け入れる（`math.isfinite` チェックなし）。また巨大な整数文字列（例: `"1000000000"`）は巨大な `float` になる
  2. 呼び出し元 [`session_manager.py`](session_manager.py:551) の `duration = max(default_dur, retry_after)` で `Retry-After: inf` の場合 `duration = inf` → [`session_manager.py`](session_manager.py:553) の `int(duration)` が `OverflowError`
  3. `_handle_block` は [`session_manager.py`](session_manager.py:360) から `custom_request` 内の `try/except Exception`（[`session_manager.py`](session_manager.py:367)）で呼ばれるため、`OverflowError` は `logger.debug` のみで握られ、**`mark_rate_limited`（排他ウィンドウ設定＋UAローテーション＋epoch bump＋crumbリセット）が実行されない**
  4. 結果: 429/439 が検知されたのに排他ウィンドウが設定されず、焼けた crumb/cookie を使い続けるため **429 再取得ループを誘発**し得る
  5. 一方 [`market_state.py`](market_state.py:381) 経由（`mark_yf_429`）は `min(max(graduated, retry_after), self.yfinance_max_backoff_sec)` で 600s にクランプされるため実害なし。`nan` は `nan > 0` が False で `else` 分岐（graduated）となり実害なし
- 問題・根本原因: `parse_retry_after` は「秒数 or HTTP-date」を返すことを約束しているが、非有限値のフィルタと上限クランプがない。HTTP 仕様（RFC 9110 §10.2.3）では `Retry-After` は非負整数秒または HTTP-date のみであり、`inf` / `nan` は仕様乖離。加えて `session_manager` 経路ではバックオフ値の上限クランプがなく、異常値が `mark_rate_limited` の排他ウィンドウを直接操作する
- 重要度評価: Medium。実際に `Retry-After: inf` を送る通常のサーバーは稀だが、(a) プロキシ/CDN/改ざんによる非有限ヘッダ値、(b) 巨大な HTTP-date（遠い将来）による実質永久レート制限、の2経路で「外部障害時の不適切な挙動」に至る。429 ループは主要データ取得機能（株価同期）の継続的失敗に繋がる
- 客観的根拠: 静的プローブで `parse_retry_after(... "inf") == inf` を確認。`int(inf)` が `OverflowError` になることを確認。既存テスト `tests/test_rate_limiting.py` の `RetryAfterParsingTestCase` は「秒数」「HTTP-date」「無効文字列」「ヘッダ欠如」のみを検証しており、非有限値・巨大値は未検証

---

### [UTIL-2][Medium] 問題候補: `sanitize_cache_key` の文字置換により、異なる実キーが同一キャッシュキーに衝突する

- 該当箇所: [`utils/caching.py`](utils/caching.py:96)-[`utils/caching.py`](utils/caching.py:103)
- 影響経路:
  1. `sanitize_cache_key` は `re.sub(r"[^\w\-:._]", "_", key)` で、`!` `#` `?` `&` `+` `/` 等を `_` に一括置換する
  2. この置換は非可逆（injective でない）。例: `"search_a!b"` と `"search_a_b"` はどちらも `"search_a_b"` に正規化される（実測確認済み）
  3. 到達可能な実経路: [`routes/api_stocks.py`](routes/api_stocks.py:677) の `get_cached(f"search_{q}", ...)` で、ユーザー入力 `q`（`/api/search?q=...`、`require_trusted_or_admin(request, require_origin=False)` で検証、長さ2〜200のみ制約）を含むキーを生成
  4. 例: `q=a!b` と `q=a_b` は別検索だが同一キャッシュエントリになり、**先にフェッチされた方の検索結果が他方にも返る**（TTL=CACHE_DURATION_SEARCH=60秒間の誤結果）
  5. さらに `clear_cache_prefix(prefix)` も同じ正規化を使うため、`!` を含むprefix は意図したキー集合を消せない可能性がある
- 問題・根本原因: キャッシュキーを「ファイル名安全」に正規化する意図は分かるが、`_` への一括置換が衝突を生む。`stock_payload` や `search` などユーザー由来文字列をキーに含む箇所では、異なる入力が同じキャッシュを共有する。`disk_cache._entry_path`（[`utils/disk_cache.py`](utils/disk_cache.py:243)）は digest を付与して衝突を回避しているのに対し、インメモリキャッシュの `sanitize_cache_key` は衝突回避策がない
- 重要度評価: Medium。`/api/search` は実装済みの公開エンドポイントであり、`!` `+` `#` 等は検索クエリとして正当な入力。異なる検索語の結果混在は「重大なデータ不整合」に近いが、TTLが短く（60秒）、単一利用者前提のローカルモデルでは偶発的衝突の頻度は低い。ただし複数ユーザー/共有利用や、`!`を多用するクエリでは確実に再現する
- 客観的根拠: 静的プローブで `sanitize_cache_key("search_a!b") == sanitize_cache_key("search_a_b") == "search_a_b"` を実測確認。既存テスト `tests/test_review_fixes_r1_r2.py` は `sanitize_cache_key` の負キー（`__negative`）を扱うが、置換衝突（injective性）の検証はない

---

### [UTIL-3][Medium] 問題候補: `StockDiskCache.get()` が「正しいJSONだが dict でない形状」のキャッシュファイルで `AttributeError` を送出し、呼び出し元ルートで 500 に繋がる

- 該当箇所: [`utils/disk_cache.py`](utils/disk_cache.py:330)-[`utils/disk_cache.py`](utils/disk_cache.py:334)
- 影響経路:
  1. `get()` は `data = json.loads(path.read_text(...))` の後、`return data.get("value")` を実行（[`utils/disk_cache.py`](utils/disk_cache.py:331)）。`data` が dict でない場合（`[]` や `"str"` や `42`）に `AttributeError`
  2. 例外ハンドラは `except (json.JSONDecodeError, OSError, KeyError)`（[`utils/disk_cache.py`](utils/disk_cache.py:332)）で **`AttributeError` を捕捉しない** → 例外が `get()` から漏れる
  3. 到達可能な実経路:
     - [`routes/api_stocks.py`](routes/api_stocks.py:626) `disk_data = app_state.stock_disk_cache.get(cache_key)` は try で包まれていない → `AttributeError` がエンドポイントに伝播し 500（株価履歴API機能停止）
     - [`utils/stock_payload.py`](utils/stock_payload.py:170) は `except (OSError, TypeError)` のみ → `AttributeError` は漏れてペイロードビルド失敗
     - [`services/stock_provider.py`](services/stock_provider.py:911) も `get` を呼ぶ（try の有無は同様に未カバー）
  4. 不正shapeファイルの発生要因: 手動編集、別バージョンのアプリが異なるフォーマットで書き込む、ディスク障害、`remove_fields_recursive` 系のマイグレーションと同時の競合など
- 問題・根本原因: `get()` の「破損データ耐性」が JSON パース失敗（`JSONDecodeError`）のみを想定しており、「パースは成功するが想定外の形状（非dict）」を考慮していない。`data.get("value")` は dict 前提であり、list/スカラには耐性がない。`_read_stale_payload`（[`utils/disk_cache.py`](utils/disk_cache.py:76)）は `get_stale()` 内の `except Exception` で保護されているため安全だが、`get()` だけが漏れる不整合がある
- 重要度評価: Medium。特定条件下（破損/不正shapeキャッシュファイル）での機能不全・500。通常運用では `set()` が必ず dict（`{"value":..., "stored_at":...}`）を書くため、不正shapeは外部要因（手動編集・バージョン差異・ディスク障害）に依存する。ただし一度発生するとキャッシュファイルが存在し続ける限り `get()` を呼ぶたび例外となり、該当エンドポイントが継続的に失敗する
- 客観的根拠: 静的プローブで list 形状（`[]`）のキャッシュファイルに対する `get()` が `AttributeError: 'list' object has no attribute 'get'` を送出することを実測確認。既存テスト `tests/test_disk_cache.py` の `test_corrupt_json_returns_none`（[`tests/test_disk_cache.py`](tests/test_disk_cache.py:265)）は「不正JSON」のみを検証しており、「正しいJSONだが非dict」は未検証

---

## 要確認問題候補（確証が取れないもの）

### [UTIL-C1][要確認] `session_manager` 経路の `mark_rate_limited` がバックオフ上限をクランプしない
- 該当箇所: [`session_manager.py`](session_manager.py:551)-[`session_manager.py`](session_manager.py:553)、[`session_manager.py`](session_manager.py:646)-[`session_manager.py`](session_manager.py:663)
- 内容: [`market_state.py`](market_state.py:381) の `mark_yf_429` は `yfinance_max_backoff_sec`（既定600s）でクランプするが、`session_manager._handle_block` → `mark_rate_limited` は `duration` をクランプせず `new_until = now + duration` に直接用いる。そのため `Retry-After: <遠いHTTP-date>` や巨大整数が実質「永久レート制限」を生む可能性がある。`Retry-After` がHTTP-date形式で将来日付（例: 2099年）を返すサーバーの実在性と、実環境での影響継続時間は要確認
- 根拠: [`session_manager.py`](session_manager.py:551) の `duration = max(default_dur, retry_after)` に上限クランプが無いことを確認。`mark_rate_limited` も `new_until <= existing` の短縮防止のみで上限なし。

---

## 指摘対象外（健全と判断した項目）

- **環境変数ヘルパー**: [`utils/env_helpers.py`](utils/env_helpers.py:28)-[`utils/env_helpers.py`](utils/env_helpers.py:72) の `_env_int`/`_env_float`/`_env_bool` は、境界クランプ・`math.isfinite` による非有限値拒否・デフォルトフォールバックを正しく実装。`constants.py` の全環境変数が bounded で安全。
- **`_is_testing` / `_is_production_env` / `_is_remote_api_enabled`**: 単一の真実源として正しく実装。`MNS_COOKIE_SECURE` が本番判定を誘発しないコメントの意図も妥当。
- **ディスクキャッシュの書き込み**: [`utils/disk_cache.py`](utils/disk_cache.py:389)-[`utils/disk_cache.py`](utils/disk_cache.py:435) の `set()` は UUID tmp ファイル → `os.replace` 原子置換 → `fsync`（ファイル＋親ディレクトリ）を実施。破損リスクは低い。
- **ディスクキャッシュのキー生成**: [`utils/disk_cache.py`](utils/disk_cache.py:236)-[`utils/disk_cache.py`](utils/disk_cache.py:246) の `_entry_path` は SHA256 ダイジェスト付与で衝突を回避（`sanitize_cache_key` と異なり健全）。
- **プロセス間ロック**: [`utils/disk_cache.py`](utils/disk_cache.py:158)-[`utils/disk_cache.py`](utils/disk_cache.py:234) は `fcntl.flock` / `msvcrt.locking` を非ブロッキング＋タイムアウト（10s）で正しく実装。`DiskCacheLockTimeout` で graceful デグラデーション。
- **chat_history**: [`utils/chat_history.py`](utils/chat_history.py:227)-[`utils/chat_history.py`](utils/chat_history.py:638) の WAL モード、スレッド毎コネクション＋`weakref.finalize`、`ON DELETE CASCADE`、`_encrypt_content` の fail-closed、`_get_timestamp` の単調増加（`_last_ts_lock` 保護）、`_execute_in_transaction` のリトライ+指数バックオフ、プレースホルダーによるSQLインジェクション回避は全て健全。
- **`_parse_datetime_to_utc`**: [`utils/formatting.py`](utils/formatting.py:11)-[`utils/formatting.py`](utils/formatting.py:52) は 8桁日付のエポック誤解釈ガード、`OverflowError` 捕捉、UTC 正規化を正しく実装。`tests/test_formatting.py` で境界値（9桁エポック・オーバーフロー・8桁日付）を網羅。
- **`build_fallback_analysis_result`**: 構造化出力失敗時のネイティブ判定フォールバックとして妥当。

---

## 補足: テスト裏取りの限界
- 指示に含まれる `tests/test_caching.py`, `tests/test_http_utils.py`, `tests/test_env_helpers.py` はリポジトリに存在せず、実行対象から除外した（代わりに `test_coverage_pure_extra.py` / `test_coverage_utils_extra.py` / `test_rate_limiting.py` で `http_utils` / `env_helpers` / キャッシュ関連をカバー）。
- UTIL-1〜3 は全て「正規の入力に非正規・破損・境界データが混入する」経路であり、既存テストは正常系のみを検証しているため、全テストグリーンと問題存在は矛盾しない。
- `KEYRING_BACKEND=keyring.backends.fail.Keyring`、`MNS_SKIP_BOOTSTRAP=1` のテスト環境では実キーリング/DPAPI 分岐は未実行（`goal-review-core.md` と同一の制約）。

## 保存場所
- 本ファイル: `goal-review-utils.md`（作業ディレクトリ直下）
