# Mistral NeX Stocks 自律コードレビュー報告書（現在HEAD）

> **本レポートの位置づけ**: 本ファイルは `plans/` 配下の統合レポートです。コードレビュー実施の都度、本ファイルに追記・更新します。各指摘は「対応済み」「対応不要」「対応不能」の最終状態を明記します。

- 報告日: 2026-08-16（JST）
- 対象: 現在の HEAD 全体（リポジトリ全体の自律レビュー）
- レビューフェーズ: 全7領域（バックエンドコア/routes/services/utils/残存バックエンド/フロントエンドテンプレート/Chrome拡張NativeHost）を静的・動的レビュー
- 修正フェーズ: 確定した9件の指摘を根本原因から修正＋回帰テスト追加＋全体検証完了
- 既存レポート統合元: `plans/code_review_report.md`（M1〜M8）、`plans/current_head_code_review_report.md`（旧版、R3-1〜R4-5）
- 遵守事項: 既存未コミット差分（`static/css/index.css`, `static/js/ai_portfolio.js`, `templates/index.html`）は保護・未変更。commit/push は行わない。

---

## 0. 本レポートの記載ルール

- コードレビューを実施した指摘は本ファイルに記載する
- 各指摘には一意のID（R1, R2, ...）を付与する
- 各指摘の「結果」欄には必ず以下のいずれかを明記する:
  - **✅ 修正済み**: 根本原因からの修正が完了し、回帰テストで検証済み
  - **⚪ 対応不要**: 精査の結果、実害がないと判断
  - **⛔ 対応不能**: 技術的・環境的理由で修正不可（理由を明記）
- 同一の根本原因から派生する問題は1件に統合する

---

## 1. 基準状態

### 1.1 検証環境

- 環境: Windows 11 / Python 3.14.6 / uv 0.11.25 / Node v24.19.0
- ブランチ: `master`、HEAD: `66fa3e5`
- 既存未コミット差分: `static/css/index.css`, `static/js/ai_portfolio.js`, `templates/index.html`（3ファイル、保護対象）
- テストベースライン: 2096 passed / 2 skipped / 0 failed（POSIX専用スキップ2件）

### 1.2 プロジェクト概要

| 項目              | 内容                                                                            |
| ----------------- | ------------------------------------------------------------------------------- |
| 名称              | Mistral NeX Stocks v3.0.0                                                       |
| 言語/ランタイム   | Python >=3.11,<3.15                                                             |
| Webフレームワーク | Flask >=3.1.3                                                                   |
| WSGI              | Gunicorn（単一ワーカー必須）                                                    |
| 主要依存          | mistralai, yfinance, pandas, cryptography, keyring, tavily-python, curl_cffi 等 |
| CI                | 5ジョブ（frontend/lint/type-check/security-scan/test）、cov-fail-under=68%      |
| テスト            | pytest 2098件（全テストパス）、mypy/ruff クリーン                               |

### 1.3 既存レポートからの統合指摘（M1〜M8, R3-1〜R4-5）

`plans/code_review_report.md`（M1〜M8）および旧版 `plans/current_head_code_review_report.md`（R3-1〜R4-5）の指摘は全て解決済みまたは対応不要であることを確認した。

| 旧ID | 内容                                            | 現状                                                       | 本報告書での状態                      |
| ---- | ----------------------------------------------- | ---------------------------------------------------------- | ------------------------------------- |
| M1   | ルートに `config.json` 残留                     | 移行コードで削除実装済み                                   | ⚪ 対応不要（既存レビューで解決済み） |
| M2   | `PYTHONKEYRING_BACKEND` 誤り                    | `ci.yml:16` に残存するが実害なし（正しい行が先に設定済み） | ⚪ 対応不要（実害なし）               |
| M3   | SSE初期スナップショットのデータ競合             | ロック順序整理・防御層強化で緩和済み                       | ⚪ 対応不要（既存レビューで改善済み） |
| M4   | `last_event_id` のURLマスク                     | `_SENSITIVE_QUERY_PARAMS` に追加済み                       | ⚪ 対応不要（既存レビューで解決済み） |
| M5   | レガシーconfig移行後に元ファイル削除なし        | `load_config` で削除実装済み                               | ⚪ 対応不要（既存レビューで解決済み） |
| M6   | chat_history が `.cache` に作成                 | `APP_DATA_DIR` へ移行済み                                  | ⚪ 対応不要（既存レビューで解決済み） |
| M7   | エラーハンドラーの error_code 一貫性欠如        | `error_handlers.py` で修正済み                             | ⚪ 対応不要（既存レビューで解決済み） |
| M8   | `last_loaded_rev` 初期値不一致                  | `market_state.py:105` で `-1` に修正済み                   | ⚪ 対応不要（既存レビューで解決済み） |
| R3-1 | ディスクキャッシュが `BASE_DIR/.cache` に作成   | `APP_DATA_DIR` へ移行済み                                  | ⚪ 対応不要（旧版レビューで解決済み） |
| R3-2 | `config.json.template` 追跡状態の不明確さ       | 追跡済み・無視対象外を確認                                 | ⚪ 対応不要（問題不存在）             |
| R4-1 | ローカルレート制限の余裕                        | 意図的設計（仕様どおり）                                   | ⚪ 対応不要（仕様どおり）             |
| R4-2 | ワークスペースルートのランタイム成果物          | 保護対象（削除不可）                                       | ⛔ 対応不能（保護対象）               |
| R4-3 | `config.json.template` 追跡（R3-2 重複）        | R3-2 に統合                                                | ⚪ 対応不要                           |
| R4-4 | `PYTHONKEYRING_BACKEND`（M2 重複）              | M2 に統合                                                  | ⚪ 対応不要                           |
| R4-5 | `app_state.py` の `KeyringError` フォールバック | 到達不能パス                                               | ⚪ 対応不要（実害なし）               |

---

## 2. 調査した主要実行経路・公開境界

### 2.1 起動・初期化・終了

- `app.py: create_app()` / `bootstrap()` / `_register_signal_handlers()` / `_cleanup_on_exit()`
- `app_bg._start_background_threads()`: バックグラウンドスレッド管理、クラッシュ時指数バックオフ再起動
- `shutdown_manager.py`: ワンタイムシャットダウントークン、Fernet暗号化保存
- `credential_manager.py`: DPAPI/Fernet 暗号化、平文フォールバック削除、メモリクリア実装

### 2.2 API サーフェス（全ルート確認）

- `routes/api_system.py`: 認証情報・ヘルスチェック・キャッシュ・メトリクス・CSRF・CSP・シャットダウン
- `routes/api_stocks.py`: 株価一覧・詳細・履歴・検索・スクリーナー・ポートフォリオ・ヒートマップ・SSEストリーム
- `routes/api_analysis.py`: トレンド・チャット（ポーリング+SSE）・ニュース・分析・AIテクニカル線
- `routes/pages.py`: 静的ページルーティング

### 2.3 セキュリティ境界

- CSRF: Flask-WTF `CSRFProtect`、3エンドポイントのみ例外（各々独自トークン機構）
- Origin/Sec-Fetch-Site: 多層検証（`_enforce_sec_fetch_site_check`, `_is_local_request`, `_is_allowed_shutdown_origin`, `require_trusted_or_admin`）
- レート制限: IP/トークン別、ローカル倍率、ポーリング重複スキップ
- 暗号化: keyring/DPAPI/Fernet による秘密保存、fail-closed設計
- シャットダウン防御: 7層防御（ローカル判定→Origin許可リスト→confirm必須→ワンタイムトークン必須）

### 2.4 SSE / リアルタイム

- `/api/stocks/stream`（モード0/1/2）、Last-Event-ID リプレイ、リスナー上限、バックプレッシャー
- `services/realtime_engine.py`: TradingView WS / Yahoo JP / PTS / フォールバックチェーン
- ポートフォリオ境界: 公開市場データ・SSE・payload disk cache から除去

### 2.5 バックグラウンド / 永続化

- `app_bg`: 同期ループ、自動無効シンボル削除、補完SSE
- ストレージ: 全永続化データ（config/user_stocks/chat_history/ai_portfolios）は Fernet 暗号化、`APP_DATA_DIR` 配下に統合

### 2.6 ネイティブホスト / 拡張

- ネイティブメッセージプロトコル（4バイト長+JSON）、拡張ID・プロセス祖先・origin 三重検証
- Chrome拡張: MV3、`host_permissions` は loopback 限定、`<all_urls>` なし

---

## 3. 確定問題リスト（今回の自律レビューで検出・対応）

### [R1][High → 対応不要] シャットダウンAPIのCSRF除外がOrigin/Sec-Fetch-Site検証を実質無効化する経路（精査の結果、実害なし）

- **該当箇所**: [`app.py:226`](app.py:226)（CSRF除外登録）、[`routes/api_system.py:704`](routes/api_system.py:704)（`api_shutdown` エンドポイント）
- **影響経路**: シャットダウンAPIが `csrf.exempt()` 対象 → CSRFトークンなしのリクエストが到達可能
- **問題・根本原因**: 当初はCSRF除外経路でOrigin/Sec-Fetch-Site検証が無効化される可能性を懸念
- **精査結果**: `api_shutdown` はエンドポイント自身が常時Origin検証（`_is_allowed_shutdown_origin()`）を実施。`consume_shutdown_token()` は fail-closed（トークンなしではFalse）。`Sec-Fetch-Site: cross-site` は `_enforce_sec_fetch_site_check()` がブロック。既存7層防御（ローカル判定→RAW_REMOTE_ADDR loopback→Origin許可リスト→confirm必須→ワンタイムトークン必須）により検証ギャップは存在しない
- **結果**: **⚪ 対応不要（実害なし）**
- **回帰テスト**: 防御固定のため5件のテストを追加（`tests/test_review_r1_r2_fix_app.py`）

---

### [R2][Medium] SECRET_KEY自動生成キーの永続化失敗による起動不能

- **該当箇所**: [`app.py:426-448`](app.py:426)
- **影響経路**: 非本番環境で `FLASK_SECRET_KEY` 未設定 → `get_or_create_flask_secret_key()` が `config_store.save_config()` 失敗（読取専用APP_DATA_DIR・ディスク満杯・Windowsロック競合）で例外伝播 → 起動不能
- **問題・根本原因**: `_configure_secret_key()` が永続化失敗を致命的エラーとして扱っていた。本番環境の fail-closed（`ValueError`）は維持必須
- **対応内容**: `try/except Exception` でラップし、永続化失敗時は警告ログを出力して `secrets.token_hex(32)` によるメモリ内キーへフォールバック、起動を継続。本番fail-closed・短キー拒否は維持
- **結果**: **✅ 修正済み**
- **回帰テスト**: [`tests/test_review_r1_r2_fix_app.py`](tests/test_review_r1_r2_fix_app.py) に4件追加（永続化失敗→起動継続、警告ログ、本番fail-closed維持、短キー拒否維持）

---

### [R3][Medium] AI技術的ラインの内部エラーメッセージ露出（ROUTE-1 + SVC-1 統合）

- **該当箇所**: [`services/ai_service.py:927`](services/ai_service.py:927) + [`routes/api_analysis.py:1637`](routes/api_analysis.py:1637)
- **影響経路**: `/api/ai-technical-lines` 呼び出し → Mistral API エラー → `str(exc)` が `details["reason"]` 経由でクライアントレスポンスに露出
- **問題・根本原因**: 他エンドポイント（`_chat_error_response`, `_analyze_v2_error_response`, SSE stream）では固定文言に正規化済みだが、このエンドポイントのみ対応漏れ。`_sanitize_error_message` は既知パターンのみREDACTEDするため、SDK内部エラーが漏れる
- **対応内容**: サービス層（`generate_ai_technical_lines()`）とルート層（`/api/ai-technical-lines`）の両方で内部エラー文字列を固定メッセージに正規化。内部詳細はサーバーログに記録
- **結果**: **✅ 修正済み**
- **回帰テスト**: [`tests/test_review_r3_r4_r10_fixes.py`](tests/test_review_r3_r4_r10_fixes.py) に4件追加

---

### [R4][Medium] /api/credentials GET の Origin チェックなし・スキーマ検証なし

- **該当箇所**: [`routes/api_system.py:160`](routes/api_system.py:160)
- **影響経路**: 内部フィールド（`credentials_ephemeral_keys` 等、`AppConfigSchema` 未定義）がスキーマ検証なしでレスポンスに含まれる
- **問題・根本原因**: `require_origin=False` で認可され、応答フィールドを許可リスト方式でフィルタリングしていなかった。秘密値そのものは漏れないが、防御の深さ不足
- **対応内容**: GET に lenient Origin チェック追加（Origin 未設定の同一オリジンブラウザGETは許可）。応答フィールドを明示許可リスト方式に変更。リモートモードでは admin token 認証により Origin チェックをスキップ
- **結果**: **✅ 修正済み**
- **回帰テスト**: [`tests/test_review_r3_r4_r10_fixes.py`](tests/test_review_r3_r4_r10_fixes.py) に5件追加

---

### [R5][Medium] `parse_retry_after()` が `Retry-After: inf`/`NaN` をクランプしない

- **該当箇所**: [`utils/http_utils.py:51`](utils/http_utils.py:51)
- **影響経路**: `Retry-After: inf`/`NaN` → `int(inf)` で `OverflowError` → `mark_rate_limited`（排他ウィンドウ＋UAローテーション＋crumbリセット）スキップ → 429再取得ループ誘発
- **問題・根本原因**: 非有限値・負値・過大値に対するガードがない
- **対応内容**: `_clamp_retry_after()` ヘルパー導入。inf/NaN → None、負値 → 0.0、過大値（>86400s）→ 86400.0 にクランプ。戻り値型 `float | None` は不変
- **結果**: **✅ 修正済み**
- **回帰テスト**: [`tests/test_review_r5_r6_r7_fixes.py`](tests/test_review_r5_r6_r7_fixes.py) に10件追加

---

### [R6][Medium] `sanitize_cache_key()` のキー衝突で検索結果混在

- **該当箇所**: [`utils/caching.py:96`](utils/caching.py:96)
- **影響経路**: `!` `+` `#` の `_` への一括置換により、`search_a!b` と `search_a_b` が同一キーに衝突 → 異なる検索語の結果が混在
- **問題・根本原因**: 置換が可逆的でなく、異なる文字が同一キーにマッピングされる
- **対応内容**: パーセントエンコード方式へ変更（英数字と `_` `-` `.` `:` はそのまま、それ以外は `%XX` に変換、`%` は `%25` にエンコード）。未使用 `import re` を除去
- **結果**: **✅ 修正済み**
- **回帰テスト**: [`tests/test_review_r5_r6_r7_fixes.py`](tests/test_review_r5_r6_r7_fixes.py) に6件追加
- **注意**: アプリ再起動後のインメモリキャッシュキーが変わる（TTL短いため実害限定）。永続ディスクキャッシュには影響なし

---

### [R7][Medium] `StockDiskCache.get()` が list 形状キャッシュで 500

- **該当箇所**: [`utils/disk_cache.py:331`](utils/disk_cache.py:331)
- **影響経路**: 正しいJSONだが list 形状のキャッシュファイル → `AttributeError: 'list' object has no attribute 'get'` → 500
- **問題・根本原因**: 例外ハンドラが `AttributeError` を捕捉せず、破損データの検出と安全な fallback がない
- **対応内容**: `isinstance(data, dict)` ガード追加（dict以外は破損キャッシュとして `None` を返す）。例外ハンドラに `TypeError, AttributeError` 追加（防御的二重化）
- **結果**: **✅ 修正済み**
- **回帰テスト**: [`tests/test_review_r5_r6_r7_fixes.py`](tests/test_review_r5_r6_r7_fixes.py) に5件追加

---

### [R8][Medium] ログマスキング不完全でトークン漏洩

- **該当箇所**: [`native_host/native_host.py:71`](native_host/native_host.py:71) の `_sanitize_log_message()`
- **影響経路**: `Authorization: Bearer abc.def.ghi` → `[REDACTED] abc.def.ghi`（トークン漏洩）、`token=abc"def` → `[REDACTED]"def`（部分漏洩）を直接実行で確認
- **問題・根本原因**: マスキング正規表現の値部分が `[^\s'\"]+` で引用符・区切り文字を境界としていたため、トークン全体がマスクされない
- **対応内容**: 値部分を `[^\s]+` に変更。`authorization` にスキーム消費オプション（`Bearer`/`Basic` 等）追加
- **結果**: **✅ 修正済み**
- **回帰テスト**: [`tests/test_review_r8_r9_fixes.py`](tests/test_review_r8_r9_fixes.py) に11件追加

---

### [R9][Medium] トークン発行がバックエンド稼働状態を未確認

- **該当箇所**: [`native_host/native_host.py:940`](native_host/native_host.py:940) の `get_extension_api_token()`
- **影響経路**: バックエンド停止中でもトークンを発行（`get_shutdown_token` の「停止中は秘密を渡さない」方針と非対称）
- **問題・根本原因**: ヘルスチェックゲートが欠如
- **対応内容**: `is_backend_healthy_once` が False/None の場合、トークン発行を拒否（`{"ok": False, "error": "..."}`）。`get_shutdown_token` と対称化
- **結果**: **✅ 修正済み**
- **回帰テスト**: [`tests/test_review_r8_r9_fixes.py`](tests/test_review_r8_r9_fixes.py) に4件追加

---

### [R10][Low] /api/screener の total と stocks 件数不整合

- **該当箇所**: [`routes/api_stocks.py:858`](routes/api_stocks.py:858)
- **影響経路**: `total` がフィルタリング全件数、`stocks` は最大150件に切り詰められる → API コントラクト不整合
- **問題・根本原因**: `total` の意味が文書化されておらず、フロントエンドが期待する値と異なる
- **対応内容**: `total` を返却件数ベース（`min(len(filtered), 150)`）に変更。後方互換のため `totalFiltered` フィールドを追加（フィルタリング全件数）
- **結果**: **✅ 修正済み**
- **回帰テスト**: [`tests/test_review_r3_r4_r10_fixes.py`](tests/test_review_r3_r4_r10_fixes.py) に2件追加
- **後方互換性**: フロントエンド `screener.js` は `data.total` をテキスト表示にのみ使用（ページネーションなし）。`totalFiltered` は新規追加フィールド

---

### [R11][Low] キャッシュのシャローコピー共有でデータ不整合リスク

- **該当箇所**: [`services/stock_service.py:287`](services/stock_service.py:287) の `fetch_history_sync_impl()`
- **影響経路**: `dict(result)` のシャローコピーを `yfinance_short_cache` に格納。可変の `history` リストがキャッシュと返り値で共有 → 呼び出し元の破壊的変更でキャッシュ汚染
- **問題・根本原因**: シャローコピーでは `history` リストの参照が共有される
- **対応内容**: `dict(result)` → `copy.deepcopy(result)` に変更（`import copy` 追加）
- **結果**: **✅ 修正済み**
- **回帰テスト**: [`tests/test_review_r11_fix.py`](tests/test_review_r11_fix.py) に3件追加（キャッシュ独立性・呼び出し元変更の非汚染・ネストされたdictの独立性）

---

## 4. 変更ファイル一覧

| ファイル                                                                       | 変更概要                                                            | 対応ID    |
| ------------------------------------------------------------------------------ | ------------------------------------------------------------------- | --------- |
| [`app.py`](app.py)                                                             | SECRET_KEY永続化失敗時フォールバック（+`secrets` import）           | R2        |
| [`routes/api_analysis.py`](routes/api_analysis.py)                             | AI技術的線エラーメッセージ正規化                                    | R3        |
| [`routes/api_system.py`](routes/api_system.py)                                 | /api/credentials GET の Originチェック・フィールド許可リスト        | R4        |
| [`routes/api_stocks.py`](routes/api_stocks.py)                                 | /api/screener total 整合 + totalFiltered 追加                       | R10       |
| [`services/ai_service.py`](services/ai_service.py)                             | `generate_ai_technical_lines()` エラー正規化                        | R3        |
| [`services/stock_service.py`](services/stock_service.py)                       | `dict(result)` → `copy.deepcopy(result)`                            | R11       |
| [`utils/http_utils.py`](utils/http_utils.py)                                   | `parse_retry_after()` クランプ処理（+`math` import）                | R5        |
| [`utils/caching.py`](utils/caching.py)                                         | `sanitize_cache_key()` パーセントエンコード方式（未使用 `re` 除去） | R6        |
| [`utils/disk_cache.py`](utils/disk_cache.py)                                   | `StockDiskCache.get()` 形状ガード                                   | R7        |
| [`native_host/native_host.py`](native_host/native_host.py)                     | ログマスキング完全化 + トークン発行ゲート                           | R8,R9     |
| [`tests/test_review_r1_r10_fixes.py`](tests/test_review_r1_r10_fixes.py)       | 既存テスト期待値更新（R3対応）                                      | R3        |
| [`tests/test_review_r1_r2_fix_app.py`](tests/test_review_r1_r2_fix_app.py)     | 新規回帰テスト 9件（R2:4 + R1防御固定:5）                           | R1,R2     |
| [`tests/test_review_r3_r4_r10_fixes.py`](tests/test_review_r3_r4_r10_fixes.py) | 新規回帰テスト 11件（R3:4 + R4:5 + R10:2）                          | R3,R4,R10 |
| [`tests/test_review_r5_r6_r7_fixes.py`](tests/test_review_r5_r6_r7_fixes.py)   | 新規回帰テスト 21件（R5:10 + R6:6 + R7:5）                          | R5,R6,R7  |
| [`tests/test_review_r8_r9_fixes.py`](tests/test_review_r8_r9_fixes.py)         | 新規回帰テスト 15件（R8:11 + R9:4）                                 | R8,R9     |
| [`tests/test_review_r11_fix.py`](tests/test_review_r11_fix.py)                 | 新規回帰テスト 3件                                                  | R11       |

---

## 5. 検証結果

### 5.1 全テスト

- **コマンド**: `uv run --locked --group test python -m pytest tests/ --tb=short -q --timeout=60`
- **結果**: **2159 passed / 0 failed / 0 errors / 2 skipped** ✅
- スキップ2件は POSIX 専用テスト（環境要因、既知）

### 5.2 型チェック

- **コマンド**: `uv run --locked --group typecheck mypy . --ignore-missing-imports`
- **結果**: `Success: no issues found in 64 source files` ✅

### 5.3 Lint

- **コマンド**: `uv run --locked --group lint ruff check . --line-length=100`
- **結果**: `All checks passed!` ✅

### 5.4 フロントエンド検証

- **TypeScript**: `npx tsc --noEmit -p tsconfig.json` → 0 errors ✅
- **ESLint**: `npx eslint static/js` → 0 issues ✅
- **verify-generated**: `node scripts/verify_generated_frontend.mjs` → 一致 ✅

### 5.5 起動スモーク

- **コマンド**: `uv run --locked --group test python -m pytest tests/test_startup_smoke.py tests/test_start_backend.py -q --timeout=60`
- **結果**: 6 passed ✅

---

## 6. 互換性・移行

| 対応ID                   | 影響                             | 対応                                                                            |
| ------------------------ | -------------------------------- | ------------------------------------------------------------------------------- |
| R6（sanitize_cache_key） | インメモリキャッシュキー形式変更 | アプリ再起動後に既存キャッシュエントリは参照されなくなる（TTL短いため実害限定） |
| R10（screener）          | `total` が小さくなる可能性       | フロントエンドは表示用途のみ。`totalFiltered` 追加で後方互換維持                |
| その他                   | 戻り値型・契約不変               | 後方互換性維持                                                                  |

---

## 7. 調査範囲・残存リスク

### 調査範囲

- バックエンド全Pythonファイル（~30ファイル）
- フロントエンド全JS/TS/CSS/Template（~40ファイル）
- Chrome拡張（6ファイル）、Native Host（8ファイル）
- テストファイル（90+ファイル、関連テスト実行済み）

### 対象外領域

- 外部APIの実動作検証（モックテストのみ）
- ブラウザ互換性テスト、負荷テスト、E2Eテスト
- npm audit / bandit / pip-audit（CIで検証）

### 残存リスク

1. **`parse_retry_after` の HTTP-date パース**: `email.utils.parsedate_to_datetime` 依存（標準ライブラリの edge case は未検証）
2. **SSE クライアント切断時の一時的な接続リーク**: `GeneratorExit` 時の内部 HTTP ストリーム close（GC 依存、SVC-C2 は要確認のまま）
3. **yfinance 内部 API 依存**: `session_manager.reset_yfinance_auth()` は内部属性（`_crumb`, `_cookie`）にアクセス
4. **外部サイト構造依存**: Yahoo JP / Kabutan / SBI / Minkabu / TradingView のスクレイピングに依存
5. **インメモリ単一状態**: 単一ワーカー必須（`wsgi.py` / `gunicorn.conf.py` で fail-closed 強制）
6. **テストカバレッジ**: CI の `--cov-fail-under=68` を満たすが、例外経路の網羅率は低い可能性

---

## 8. 既存ユーザー変更の保護確認

- `git status --short` で確認: 既存未コミット差分（`static/css/index.css`, `static/js/ai_portfolio.js`, `templates/index.html`）は **保持・未変更**
- 本修正による変更ファイル: 上記「変更ファイル一覧」の16ファイルのみ
- 新規追加ファイル: 5つのテストファイル + 本レポート
- `git reset --hard`, `git clean -fd`, `git checkout -- .` 等の破壊的操作は未実行
- commit / push / タグ / PR 作成は未実行
- 一時ファイル（`pytest_smoke_results.xml;` 等）は削除済み
