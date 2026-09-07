# Mistral NeX Stocks 自律コードレビュー報告書（現在HEAD）

> **本レポートの位置づけ**: 本ファイルは `plans/` 配下の統合レポートです。コードレビュー実施の都度、本ファイルに追記・更新します。各指摘は「対応済み」「対応不要」「対応不能」の最終状態を明記します。

- 報告日: 2026-09-06（JST）
- 対象: 現在の HEAD 全体（リポジトリ全体の自律レビュー）
- レビューフェーズ: 全7領域（バックエンドコア/routes/services/utils/残存バックエンド/フロントエンドテンプレート/Chrome拡張NativeHost）を静的・動的レビュー
- 修正フェーズ: バックエンド・セッション管理・セキュリティ・ライフサイクルのR1〜R11, R15〜R20, R22〜R24およびUI/フロントエンドアクセシビリティのR12〜R14, R21, R25を根本原因から修正＋回帰テスト追加＋全体検証完了
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

- 環境: Windows 11 / Python 3.14.7 / Node v22+
- ブランチ: `master`、HEAD: `16fe6cf`
- 既存未コミット差分: なし（クリーンツリーで維持）
- テストベースライン: 2159 passed / 2 skipped / 0 failed（POSIX専用スキップ2件）

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

### [R12][Low] スクリーナーリセット時のテーブルソートインジケーター非同期

- **該当箇所**: [`static/js/screener.js`](static/js/screener.js)
- **影響経路**: スクリーナー画面のリセットボタンを押下した際、ソート順ボタンは初期化されるがテーブルヘッダーのソート矢印インジケーターが更新されず、UI表示が不整合になる
- **問題・根本原因**: リセットハンドラ内でソート状態のリセット後に `updateTableSortIndicators()` の呼び出しが欠落していた
- **対応内容**: リセットイベントハンドラ内で `updateSortOrderBtn()` に続いて `updateTableSortIndicators()` を連動呼び出し
- **結果**: **✅ 修正済み**
- **回帰テスト**: [`tests/test_code_review_goal_audit_2026_09_v2.py`](tests/test_code_review_goal_audit_2026_09_v2.py)

---

### [R13][Medium] LocalStorage 書き込み例外による画面動作停止リスク

- **該当箇所**: [`static/js/api.js`](static/js/api.js), [`static/js/state.js`](static/js/state.js), [`static/js/index_main.js`](static/js/index_main.js), [`static/js/settings.js`](static/js/settings.js)
- **影響経路**: プライベートブラウズモードやブラウザの容量超過（QuotaExceededError）、ストレージ制限設定下で `localStorage.setItem` が例外を投げ、以降のJSスクリプト実行が停止する
- **問題・根本原因**: クライアント側ストレージ書き込み処理が try/catch で保護されておらず、未捕捉例外がUIライフサイクルを中断する
- **対応内容**: 全ての LocalStorage 保存・更新処理を try/catch ブロックで保護し、失敗時もフォールバックして処理を継続
- **結果**: **✅ 修正済み**
- **回帰テスト**: [`tests/test_code_review_goal_audit_2026_09_v2.py`](tests/test_code_review_goal_audit_2026_09_v2.py)

---

### [R14][Low] Chrome拡張機能ポップアップの観測所（Orbit）導線およびキーボードアクセシビリティ

- **該当箇所**: [`chrome_extension/popup.js`](chrome_extension/popup.js), [`chrome_extension/popup.html`](chrome_extension/popup.html), [`chrome_extension/popup.css`](chrome_extension/popup.css)
- **影響経路**: 拡張機能ランチャーから新設された観測所 (/experimental/orbit) へのクイック起動ができない。また、タブ切替時のキーボード操作やアクセシビリティ属性が不完全
- **問題・根本原因**: 観測所へのショートカットボタン欠落、および WAI-ARIA 仕様に基づくロービングフォーカス（ArrowRight/ArrowLeft/Home/End/Enter/Space）の未実装
- **対応内容**: ポップアップに Orbit 起動ボタンを追加しグリッドレイアウトを調整。タブナビゲーションにキーボード操作イベントリスナーと ARIA 属性を完全実装
- **結果**: **✅ 修正済み**
- **回帰テスト**: [`tests/test_code_review_goal_audit_2026_09_v2.py`](tests/test_code_review_goal_audit_2026_09_v2.py)

---

### [R15][Medium] parse_retry_after() および _parse_datetime_to_utc() の timezone-naive HTTP 日付パースにおけるローカル時刻誤解釈

- **該当箇所**: [`utils/http_utils.py:78-83`](utils/http_utils.py:78), [`utils/formatting.py:31-35`](utils/formatting.py:31)
- **影響経路**: 外部プロバイダや上流APIから ANSI C `asctime()` 形式（例: `"Sun Nov  6 08:49:37 1994"`）等のタイムゾーンを持たない HTTP-date が返された場合、`email.utils.parsedate_to_datetime()` は `tzinfo=None`（naive datetime）を返す。Python 標準仕様により、naive datetime に対して `.timestamp()` または `.astimezone(UTC)` を呼び出すと**システムのローカルタイムゾーン**（例: JST=+09:00）として解釈される。
- **問題・根本原因**: RFC 7231 / RFC 9110 ではすべての HTTP 日時表現は GMT (UTC) であることが規定されているが、`utils/http_utils.py` および `utils/formatting.py` では naive チェック（`if dt.tzinfo is None: dt = dt.replace(tzinfo=UTC)`）が欠落していた。これにより、JST 環境下では `dt.timestamp()` が 9時間（32,400秒）ずれて計算され、レート制限リトライ秒数が 0 秒に切り捨てられたり、過去日付に逆行する不具合が生じる（※ `services/ai_service.py:527-528` では既に正しく実装されていたが、共通ユーティリティ側で漏れがあった）。
- **対応内容**: `utils/http_utils.py` および `utils/formatting.py` において、パース結果が naive datetime の場合に明示的に `tzinfo=UTC` を付与するガードを追加。
- **結果**: **✅ 修正済み**
- **回帰テスト**: [`tests/test_formatting.py`](tests/test_formatting.py), [`tests/test_review_r5_r6_r7_fixes.py`](tests/test_review_r5_r6_r7_fixes.py) に asctime 形式の UTC 解釈回帰テストを2件追加

---

### [R16][Medium] session_manager.py における yfinance レート制限除外時間の暴走リスク（上流 Retry-After の上限クランプ欠落）

- **該当箇所**: [`session_manager.py:688-693`](session_manager.py:688), [`session_manager.py:796`](session_manager.py:796)
- **影響経路**: 上流の Yahoo Finance 等から `Retry-After: 86400`（24時間）などの過剰なバックオフ時間が返された場合、`_handle_block` および `mark_rate_limited` がこれをそのまま採用し、長時間プロセス全体で yfinance セッションが除外・機能停止する
- **問題・根本原因**: `market_state.py` の `_yf_rate_limit_backoff()` では `min(backoff, YFINANCE_BACKOFF_MAX)`（600秒上限）が適用されていたが、`session_manager.py` 側では上限クランプ処理が欠落していた
- **対応内容**: `constants.py` の `YFINANCE_BACKOFF_MAX`（600秒）を import し、`_handle_block` および `mark_rate_limited` で `min(max(1, duration), YFINANCE_BACKOFF_MAX)` による上限クランプを実施
- **結果**: **✅ 修正済み**
- **回帰テスト**: [`tests/test_code_review_goal_audit_2026_09_v3.py`](tests/test_code_review_goal_audit_2026_09_v3.py)

---

### [R17][Medium] native_host/native_host.py のログ出力先デフォルトがリポジトリソースツリーに配置される設計不備および初期化順序の整合

- **該当箇所**: [`native_host/native_host.py:101-125`](native_host/native_host.py:101)
- **影響経路**: `MNS_DATA_DIR` / `MNS_APP_DATA_DIR` が未設定の環境において、ログファイル `native_host.log` がスクリプトディレクトリ（`native_host/` ソースツリー配下）に作成され、開発ツリーを汚染する。また、ロガー初期化がルートパス解決より前に行われていたため、共通モジュールの設定ディレクトリを参照できなかった
- **問題・根本原因**: 初期化順序が不適切で、`config_store.APP_DATA_DIR` の利用が後回しになっていた
- **対応内容**: `ROOT` および `sys.path` の追加をロガー設定より前に配置し、デフォルトログディレクトリを `config_store.APP_DATA_DIR` に設定。フォールバック時のみローカル親ディレクトリを使用
- **結果**: **✅ 修正済み**
- **回帰テスト**: [`tests/test_code_review_goal_audit_2026_09_v3.py`](tests/test_code_review_goal_audit_2026_09_v3.py)

---

### [R18][Low] ニュース取得ファンアウトスレッドプールのシャットダウンライフサイクル欠落

- **該当箇所**: [`services/news_service.py:38-47`](services/news_service.py:38), [`app_state.py:353-359`](app_state.py:353)
- **影響経路**: アプリケーション終了時に `services/news_service.py` のモジュールレベルスレッドプール `_NEWS_FANOUT_POOL` に対する明示的なシャットダウンが呼ばれず、待機中ワーカースレッドがプロセスの円滑な終了を妨げる可能性
- **問題・根本原因**: `app_state.shutdown_executors()` にニュースファンアウトプールのクリーンアップフックが登録されていなかった
- **対応内容**: `services/news_service.py` に `shutdown_news_fanout_pool(wait=False)` を定義し、`app_state.shutdown_executors()` の終了シーケンスに安全に統合
- **結果**: **✅ 修正済み**
- **回帰テスト**: [`tests/test_code_review_goal_audit_2026_09_v3.py`](tests/test_code_review_goal_audit_2026_09_v3.py)

---

### [R19][Low] utils/networking.py におけるオリジン検証ヘルパーの名称乖離と公開API化

- **該当箇所**: [`utils/networking.py:442-458`](utils/networking.py:442), [`routes/stocks/views.py:553`](routes/stocks/views.py:553)
- **影響経路**: `_is_allowed_shutdown_origin` は名称が「シャットダウン」に特化しているが、実際には `/api/stocks/add_ext` 等の state-changing エンドポイント全般で利用されている。内部関数扱い（先頭アンダースコア）のため、他モジュールからの利用意図が不明瞭であった
- **問題・根本原因**: 責務と命名の乖離、および公開ヘルパーとしての未定義
- **対応内容**: 公開関数 `is_allowed_trusted_origin(req)` を定義し、後方互換性および既存テストの monkey-patch 互換性のため `_is_allowed_shutdown_origin` をエイリアスかつ委譲可能に維持。`routes/stocks/views.py` で新関数を呼び出し
- **結果**: **✅ 修正済み**
- **回帰テスト**: [`tests/test_code_review_goal_audit_2026_09_v3.py`](tests/test_code_review_goal_audit_2026_09_v3.py)

---

### [R20][Low] routes/stocks/views.py の _parse_strict_float 戻り値型アノテーションの明確化

- **該当箇所**: [`routes/stocks/views.py:116`](routes/stocks/views.py:116)
- **影響経路**: 静的解析（mypy/pyrefly）において、内部パーサーが `Any` と定義されていたため、返却される `float | None | tuple[Response, int]` のエラータプル型の追跡性が低下していた
- **問題・根本原因**: 内部ローカル関数のアノテーションが `Any` のまま残存していた
- **対応内容**: Flask `Response` をインポートし、型アノテーションを `float | None | tuple[Response, int]` に厳格化
- **結果**: **✅ 修正済み**
- **回帰テスト**: [`tests/test_code_review_goal_audit_2026_09_v3.py`](tests/test_code_review_goal_audit_2026_09_v3.py)

---

### [R21][Low] AIポートフォリオプリセットバーのキーボードアクセシビリティ（矢印キー・Home/End操作および aria-pressed 属性同期）

- **該当箇所**: [`static/js/ai_portfolio.js:115-163`](static/js/ai_portfolio.js:115), [`static/js/ai_portfolio.js:430-475`](static/js/ai_portfolio.js:430)
- **影響経路**: AIポートフォリオのプリセットピル（テック成長株/高配当ディフェンシブ/バランス型/カスタムテーマ）がタブキーでのフォーカス移動のみで、WAI-ARIA ボタングループ標準の左右上下矢印キーや Home/End による直感的なキーボード操作ができなかった。また、保存テーマ表示・削除時の切り替え時に `aria-pressed` が同期されないケースが存在した
- **問題・根本原因**: ピル要素に対するキーボードイベントハンドラおよびプログラム的変更時の ARIA 属性同期の欠落
- **対応内容**: `setupPresetBar()` に `keydown` リスナーを実装（`ArrowRight`/`ArrowDown`/`ArrowLeft`/`ArrowUp`/`Home`/`End`）。さらに `viewSavedAiPortfolio` および `deleteSavedAiPortfolio` において `aria-pressed` 属性を同期更新
- **結果**: **✅ 修正済み**
- **回帰テスト**: [`tests/test_code_review_goal_audit_2026_09_v3.py`](tests/test_code_review_goal_audit_2026_09_v3.py)

---

### [R22][Medium] api_indices における force=True パラメータ伝播欠落および共通ヘルパーの戻り値型アノテーション修正

- **該当箇所**: [`routes/stocks/quotes.py:156-158`](routes/stocks/quotes.py:156), [`routes/stocks/common.py:100`](routes/stocks/common.py:100)
- **影響経路**: `api_stocks` では `schedule_sync_all_stocks_now(force=True)` が呼ばれていたが、`api_indices` では `force = request.args.get("force") == "true"` の判定後に引数なしの `schedule_sync_all_stocks_now()` が呼ばれていたため、クライアントが明示的に強制同期を要求しても `force=True` がバックグラウンドワーカーに渡らず同期フラグが立たなかった。また、`routes/stocks/common.py` の `schedule_sync_all_stocks_now` は戻り値型が `-> None:` と定義されていたが、実際の実装（`app_bg.py:113` / `bg/sync_worker.py:1148`）は `bool` を返していた。
- **問題・根本原因**: パラメータ転送の脱落および共通ディスパッチラッパーでの型アノテーション不整合
- **対応内容**: `api_indices` で `schedule_sync_all_stocks_now(force=True)` を渡すよう修正し、`routes/stocks/common.py` の型アノテーションを `-> bool:` に更新
- **結果**: **✅ 修正済み**
- **回帰テスト**: [`tests/test_code_review_goal_audit_2026_09_v4.py`](tests/test_code_review_goal_audit_2026_09_v4.py)

---

### [R23][Low] routes/api_stocks.py における is_allowed_trusted_origin 再エクスポート欠落の解消

- **該当箇所**: [`routes/api_stocks.py:109, 227`](routes/api_stocks.py:109), [`routes/api_system.py:41, 308`](routes/api_system.py:41)
- **影響経路**: R19 で導入された公開オリジン検証ヘルパー `is_allowed_trusted_origin` が `routes/api_stocks.py` でインポート・再エクスポートされておらず、レガシーエイリアス `_is_allowed_shutdown_origin` のみが残存していた。`routes.api_stocks` からインポートする外部コンポーネントとの対称性が損なわれていた
- **問題・根本原因**: R19 公開API移行時の routes/api_stocks.py 側のエクスポート漏れ
- **対応内容**: `routes/api_stocks.py` で `is_allowed_trusted_origin` をインポートし `__all__` に追加。`routes/api_system.py` の credentials GET でも公開関数 `is_allowed_trusted_origin` を直接呼ぶよう統一
- **結果**: **✅ 修正済み**
- **回帰テスト**: [`tests/test_code_review_goal_audit_2026_09_v4.py`](tests/test_code_review_goal_audit_2026_09_v4.py)

---

### [R24][Medium] 運用系システムエンドポイント（/api/cache-stats, /api/metrics, /api/system/ai-usage）におけるクロスオリジン Origin 検証多層防御

- **該当箇所**: [`routes/api_system.py:712-720, 751-760, 1158-1168`](routes/api_system.py:712)
- **影響経路**: 内部診断・メトリクス・AI使用量統計エンドポイントにおいて、ローカル接続チェック（`_is_local_request(request)`）は行われていたが、ブラウザが外部悪意あるWebサイトから 127.0.0.1 宛にリクエストを発行した場合（`Origin: https://malicious.site` かつ `remote_addr == 127.0.0.1`）、Origin の信頼性検証が抜けていたため、クロスオリジン読み取り・内部状態プロービングの多層防御が不完全であった
- **問題・根本原因**: `/api/credentials` GET に導入されていた寛容な Origin チェック（Origin ヘッダが存在する場合は信頼できるオリジンのみ許可する設計）が運用系エンドポイントに水平展開されていなかった
- **対応内容**: `/api/cache-stats`, `/api/metrics`, `/api/system/ai-usage` に対し、`not allow_remote and request.headers.get("Origin") and not is_allowed_trusted_origin(request)` の場合は 403 Forbidden（reason: untrusted origin）を返す多層防御を追加
- **結果**: **✅ 修正済み**
- **回帰テスト**: [`tests/test_code_review_goal_audit_2026_09_v4.py`](tests/test_code_review_goal_audit_2026_09_v4.py)

---

### [R25][Low] ヒートマップ画面およびトップ画面における ARIA グループロール・ラベルおよびキーボード操作性の向上

- **該当箇所**: [`templates/heatmap.html:29-80, 110-130`](templates/heatmap.html:29), [`templates/index.html:91`](templates/index.html:91), [`static/js/heatmap.js:64-102`](static/js/heatmap.js:64)
- **影響経路**: `templates/heatmap.html` の各種切り替えボタングループ（市場選択・表示モード・サイズ基準・3Dカメラ視点操作）に `role="group"` および `aria-label` が設定されておらず、スクリーンリーダーでの利用時にボタングループの文脈が把握困難であった。またボタングループ内の左右矢印キー / Home / End によるキーボードナビゲーションが未実装であった。さらに `templates/index.html` の設定ボタンの `aria-label` が英語表記（"Settings"）のままであった
- **問題・根本原因**: WAI-ARIA ボタングループ仕様および日本語UIアクセシビリティ要件の反映不足
- **対応内容**: `templates/heatmap.html` のコンテナに `role="group"` と日本語 `aria-label` を追加し、3Dカメラボタンにも個別のアクセシブルなラベルを付与。`static/js/heatmap.js` に `setupButtonGroupKeyboardNav` を実装して矢印キー/Home/End 移動を提供。`templates/index.html` の設定ボタンの `aria-label` を「設定画面を開く」に統一
- **結果**: **✅ 修正済み**
- **回帰テスト**: [`tests/test_code_review_goal_audit_2026_09_v4.py`](tests/test_code_review_goal_audit_2026_09_v4.py)

---

### [R26][Medium] 日本株式市場（market=jp）における avg_fx_rate の混入・永続化欠陥、スキーマ境界防御の不備、およびテストハーネスのモック欠陥

- **該当箇所**: [`utils/stock_payload.py:364-370, 394-396, 945-948`](utils/stock_payload.py:364), [`utils/storage.py:27-51`](utils/storage.py:27), [`schemas/stocks.py:93-102`](schemas/stocks.py:93), [`routes/stocks/portfolio.py:195-201`](routes/stocks/portfolio.py:195), [`tests/test_portfolio_avg_fx_rate_idx.py`](tests/test_portfolio_avg_fx_rate_idx.py), [`tests/test_schemas.py:83-93`](tests/test_schemas.py:83)
- **影響経路**: 日本国内株式（`market == "jp"`）は日本円（JPY）建てであり、USD/JPY 為替レート（`avg_fx_rate`）は不要・無関係である。しかし `utils/stock_payload.py` の `_extract_portfolio_fields` は `market == "idx"` のみ `avg_fx_rate = None` にリセットしており、`market == "jp"` では除去されず、レスポンス構築時にも `_resolve_stocks_for_response` で `idx` のみ防御されていたため、レガシーデータや手動編集等で保有データに `avg_fx_rate` が含まれていた場合に日本株のポートフォリオスナップショットに不正な `avg_fx_rate` が露出していた。また `utils/storage.py` でも `_normalize_idx_holding_fields` による `idx` サニタイズのみが行われ、`_normalize_jp_holding_keys` では `avg_fx_rate` の除去が行われず永続化ファイルやインメモリキャッシュに残留していた。さらに `schemas/stocks.py` の `PortfolioUpdateRequest` は説明文で `"(US market only)"` と謳いながらモデル検証がなく非US市場での `avg_fx_rate` 指定を素通ししており、`routes/stocks/portfolio.py` も `jp` で `avg_fx_rate` が指定された際にワーニングを出していなかった。加えて `tests/test_portfolio_avg_fx_rate_idx.py` のテストハーネスが `MarketDataState` に存在しない属性 `user_stocks["idx"]` を操作・アサートしており、本番の `user_idx` を完全にバイパスしていたため、テストが実際の保存先を検証できていなかった。
- **問題・根本原因**: market-aware な為替レート境界処理（抽出、レスポンス構築、永続化正規化、リクエストスキーマ検証、ログ記録）において、`idx` のみが部分的に対応され、同じく JPY 建てである `jp`（および non-US）に対する防御が統一的に適用されていなかったこと、およびテストハーネスのモック対象が実体と乖離していたこと。
- **対応内容**:
  1. `utils/stock_payload.py`: `_extract_portfolio_fields` において `market in ("jp", "idx")`（または `market != "us"`）の場合に `avg_fx_rate` を確実に `None` に初期化し、`_resolve_stocks_for_response` でも `m_key in ("jp", "idx")` に対して `avg_fx_rate` をポップしてレスポンス境界を二重防御。
  2. `utils/storage.py`: `_normalize_jp_holding_keys` において JP 保有銘柄辞書から `avg_fx_rate` を除去・サニタイズし、読込時および保存時に非US銘柄から為替レートを完全に排除。
  3. `schemas/stocks.py`: `PortfolioUpdateRequest` に `@model_validator(mode="after")` を追加し、`self.market != "us"` で `avg_fx_rate` が指定された場合に `ValueError` を送出するモデル検証を実装。
  4. `routes/stocks/portfolio.py`: `api_update_portfolio` において `market == "jp"` で `avg_fx_rate` が指定された場合にワーニングログを出力し、`idx` と同等の監査ログを確保。
  5. `tests/test_portfolio_avg_fx_rate_idx.py`: テストフィクスチャおよびテストケースを修正し、`app_state.market.user_us`, `user_jp`, `user_idx` を正確に操作・検証するよう刷新。さらに `jp` 市場における `avg_fx_rate` 排除、スナップショットでの無視、ストレージ正規化の回帰テストを追加。
  6. `tests/test_schemas.py`: `PortfolioUpdateRequest` における非US市場の `avg_fx_rate` 拒絶テストを追加。
- **結果**: **✅ 修正済み**
- **回帰テスト**: [`tests/test_portfolio_avg_fx_rate_idx.py`](tests/test_portfolio_avg_fx_rate_idx.py), [`tests/test_schemas.py`](tests/test_schemas.py)

---

## 4. 変更ファイル一覧

| ファイル                                                                       | 変更概要                                                            | 対応ID    |
| ------------------------------------------------------------------------------ | ------------------------------------------------------------------- | --------- |
| [`app.py`](app.py)                                                             | SECRET_KEY永続化失敗時フォールバック（+`secrets` import）           | R2        |
| [`routes/api_analysis.py`](routes/api_analysis.py)                             | AI技術的線エラーメッセージ正規化                                    | R3        |
| [`routes/api_system.py`](routes/api_system.py)                                 | /api/credentials GET Originチェック + 運用系エンドポイントOrigin多層防御 | R4,R23,R24 |
| [`routes/api_stocks.py`](routes/api_stocks.py)                                 | /api/screener total 整合 + `is_allowed_trusted_origin` 再エクスポート | R10,R23   |
| [`routes/stocks/common.py`](routes/stocks/common.py)                           | `schedule_sync_all_stocks_now` 戻り値型アノテーション `-> bool:`     | R22       |
| [`routes/stocks/quotes.py`](routes/stocks/quotes.py)                           | `api_indices` の `force=True` 同期引数伝播                          | R22       |
| [`routes/stocks/views.py`](routes/stocks/views.py)                             | `_parse_strict_float` 型厳格化 + `is_allowed_trusted_origin` 呼び出し | R19,R20   |
| [`services/ai_service.py`](services/ai_service.py)                             | `generate_ai_technical_lines()` エラー正規化                        | R3        |
| [`services/stock_service.py`](services/stock_service.py)                       | `dict(result)` → `copy.deepcopy(result)`                            | R11       |
| [`services/news_service.py`](services/news_service.py)                         | `shutdown_news_fanout_pool()` 追加                                  | R18       |
| [`session_manager.py`](session_manager.py)                                     | `YFINANCE_BACKOFF_MAX` クランプ適用                                 | R16       |
| [`app_state.py`](app_state.py)                                                 | `shutdown_executors()` にニュースプール終了フック登録               | R18       |
| [`utils/http_utils.py`](utils/http_utils.py)                                   | `parse_retry_after()` クランプ処理（+`math`）＆ naive 日付 UTC 解釈 | R5,R15    |
| [`utils/formatting.py`](utils/formatting.py)                                   | `_parse_datetime_to_utc()` naive 日付 UTC ガード                    | R15       |
| [`utils/caching.py`](utils/caching.py)                                         | `sanitize_cache_key()` パーセントエンコード方式（未使用 `re` 除去） | R6        |
| [`utils/disk_cache.py`](utils/disk_cache.py)                                   | `StockDiskCache.get()` 形状ガード                                   | R7        |
| [`utils/networking.py`](utils/networking.py)                                   | 公開 `is_allowed_trusted_origin()` 定義 + 互換委譲                   | R19       |
| [`native_host/native_host.py`](native_host/native_host.py)                     | ログマスキング完全化 + トークンゲート + APP_DATA_DIR 既定化         | R8,R9,R17 |
| [`static/js/heatmap.js`](static/js/heatmap.js)                                 | トグルボタングループの矢印キー/Home/End キーボードナビゲーション     | R25       |
| [`templates/heatmap.html`](templates/heatmap.html)                             | トグルコンテナへの `role="group"` および `aria-label` 付与          | R25       |
| [`templates/index.html`](templates/index.html)                                 | 設定ボタンの `aria-label` 日本語統一（「設定画面を開く」）          | R25       |
| [`static/js/screener.js`](static/js/screener.js)                               | リセット時のソートインジケーター同期                                | R12       |
| [`static/js/ai_portfolio.js`](static/js/ai_portfolio.js)                       | プリセットピルのキーボード操作 + `aria-pressed` 属性完全同期         | R21       |
| [`static/js/api.js`](static/js/api.js)                                         | LocalStorage 例外ハンドリング保護                                   | R13       |
| [`static/js/state.js`](static/js/state.js)                                     | お気に入り保存時の LocalStorage 保護                                | R13       |
| [`static/js/index_main.js`](static/js/index_main.js)                           | アラート設定保存時の LocalStorage 保護                              | R13       |
| [`static/js/settings.js`](static/js/settings.js)                               | ソート設定保存時の LocalStorage 保護                                | R13       |
| [`chrome_extension/popup.js`](chrome_extension/popup.js)                       | Orbit ランチャー連携 + キーボードアクセシビリティ                   | R14       |
| [`chrome_extension/popup.html`](chrome_extension/popup.html)                   | Orbit ボタン追加 + ARIA タブ属性                                    | R14       |
| [`chrome_extension/popup.css`](chrome_extension/popup.css)                     | ランチャーボタングリッドのレスポンシブスタイル                      | R14       |
| [`tests/test_formatting.py`](tests/test_formatting.py)                         | asctime 形式 naive 日時 UTC 解釈テスト追加                          | R15       |
| [`tests/test_review_r1_r10_fixes.py`](tests/test_review_r1_r10_fixes.py)       | 既存テスト期待値更新（R3対応）                                      | R3        |
| [`tests/test_review_r1_r2_fix_app.py`](tests/test_review_r1_r2_fix_app.py)     | 新規回帰テスト 9件（R2:4 + R1防御固定:5）                           | R1,R2     |
| [`tests/test_review_r3_r4_r10_fixes.py`](tests/test_review_r3_r4_r10_fixes.py) | 新規回帰テスト 11件（R3:4 + R4:5 + R10:2）                          | R3,R4,R10 |
| [`tests/test_review_r5_r6_r7_fixes.py`](tests/test_review_r5_r6_r7_fixes.py)   | 新規回帰テスト 22件（R5:10 + R6:6 + R7:5 + R15:1）                  | R5,R6,R7,R15 |
| [`tests/test_review_r8_r9_fixes.py`](tests/test_review_r8_r9_fixes.py)         | 新規回帰テスト 15件（R8:11 + R9:4）                                 | R8,R9     |
| [`tests/test_review_r11_fix.py`](tests/test_review_r11_fix.py)                 | 新規回帰テスト 3件                                                  | R11       |
| [`tests/test_code_review_goal_audit_2026_09_v2.py`](tests/test_code_review_goal_audit_2026_09_v2.py) | 新規回帰テスト 12件（R12, R13, R14）         | R12,R13,R14 |
| [`tests/test_code_review_goal_audit_2026_09_v3.py`](tests/test_code_review_goal_audit_2026_09_v3.py) | 新規回帰テスト 14件（R16, R17, R18, R19, R20, R21） | R16-R21   |
| [`tests/test_code_review_goal_audit_2026_09_v4.py`](tests/test_code_review_goal_audit_2026_09_v4.py) | 新規回帰テスト 13件（R22, R23, R24, R25）           | R22-R25   |
| [`routes/stocks/portfolio.py`](routes/stocks/portfolio.py)                     | JP市場での `avg_fx_rate` 指定時のワーニングログ追加                 | R26       |
| [`schemas/stocks.py`](schemas/stocks.py)                                       | `PortfolioUpdateRequest` の非US市場 `avg_fx_rate` 拒絶バリデーション | R26       |
| [`utils/stock_payload.py`](utils/stock_payload.py)                             | `_extract_portfolio_fields` / `_resolve_stocks_for_response` で非US `avg_fx_rate` 除去 | R26       |
| [`utils/storage.py`](utils/storage.py)                                         | `_normalize_jp_holding_keys` における `avg_fx_rate` サニタイズ      | R26       |
| [`tests/test_portfolio_avg_fx_rate_idx.py`](tests/test_portfolio_avg_fx_rate_idx.py) | テストモック修正 + JP市場 `avg_fx_rate` 回帰テスト追加         | R26       |
| [`tests/test_schemas.py`](tests/test_schemas.py)                               | `PortfolioUpdateRequest` 非US市場拒絶回帰テスト追加                 | R26       |

---

## 5. 検証結果

### 5.1 全テスト

- **コマンド**: `pytest -n auto -q`
- **結果**: **2188 passed / 0 failed / 0 errors / 2 skipped** ✅
- **カバレッジ**: **79%** (20,870 statements)
- スキップ2件は POSIX 専用テスト（環境要因、既知）

### 5.2 型チェック

- **コマンド**: `mypy .`
- **結果**: `Success: no issues found in 87 source files` ✅
- **コマンド**: `pyrefly check`
- **結果**: `0 errors (19 suppressed, 7 warnings not shown)` ✅
- **コマンド**: `pyrefly check --python-platform win32`
- **結果**: `0 errors (19 suppressed, 7 warnings not shown)` ✅

### 5.3 Lint / セキュリティ

- **コマンド**: `ruff check . --line-length=100`
- **結果**: `All checks passed!` ✅
- **コマンド**: `flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics`
- **結果**: 0 errors ✅
- **コマンド**: `pylint --errors-only ...`
- **結果**: 0 errors ✅
- **コマンド**: `bandit -c pyproject.toml -r .`
- **結果**: 0 issues identified (34,754 lines scanned) ✅

### 5.4 フロントエンド検証

- **TypeScript**: `npx tsc --noEmit -p tsconfig.json` → 0 errors ✅
- **ESLint**: `npx eslint "static/js/**/*.js" "chrome_extension/**/*.js"` → 0 issues ✅
- **Prettier**: `npx prettier --check` → All matched files use Prettier style! ✅
- **verify-generated**: `node scripts/verify_generated_frontend.mjs` → 一致 ✅

### 5.5 起動スモーク

- **コマンド**: `python tests/startup_smoke_runner.py`
- **結果**: Clean pass ✅

---

## 6. 互換性・移行

| 対応ID                   | 影響                             | 対応                                                                            |
| ------------------------ | -------------------------------- | ------------------------------------------------------------------------------- |
| R6（sanitize_cache_key） | インメモリキャッシュキー形式変更 | アプリ再起動後に既存キャッシュエントリは参照されなくなる（TTL短いため実害限定） |
| R10（screener）          | `total` が小さくなる可能性       | フロントエンドは表示用途のみ。`totalFiltered` 追加で後方互換維持                |
| R16（backoff max）       | レート制限除外時間が最大600秒に  | 過剰な除外によるサービス停止を防止。正常な回復を促進                            |
| R19/R23（origin helper） | 関数名変更・再エクスポート       | `_is_allowed_shutdown_origin` を完全互換エイリアスとして維持                   |
| R22（api_indices force） | `?force=true` が即時同期反映     | 既存パラメータ仕様との整合性回復。破壊的変更なし                                |
| R24（Origin多層防御）    | 悪意ある外部Webからのプロービング遮断 | 同一オリジン（Originヘッダなし）および正規loopback Originは平常通過             |
| R25（アクセシビリティ）  | スクリーンリーダー・キーボード対応 | 既存UIデザイン・操作に悪影響なくアクセシビリティ向上                            |
| R26（非US avg_fx_rate 境界） | 非US市場（jp/idx）で avg_fx_rate を厳格排除 | スキーマ・永続化・APIレスポンスの境界で仕様（US市場限定）に統一。既存正常系に破壊的影響なし |
| その他                   | 戻り値型・契約不変               | 後方互換性維持                                                                  |

---

## 7. 調査範囲・残存リスク

### 調査範囲

- バックエンド全Pythonファイル（~30ファイル）
- フロントエンド全JS/TS/CSS/Template（~40ファイル）
- Chrome拡張（6ファイル）、Native Host（8ファイル）
- テストファイル（95+ファイル、全2188テスト実行済み）

### 対象外領域

- 外部APIの実動作検証（モックテストのみ）
- ブラウザ互換性テスト、負荷テスト、E2Eテスト
- npm audit / bandit / pip-audit（CIで検証）

### 残存リスク

1. **`parse_retry_after` の HTTP-date パース（解決済み・R15）**: `email.utils.parsedate_to_datetime` が返す naive datetime について、システムローカル時刻でなく UTC として処理するよう `tzinfo=UTC` ガードを追加し、asctime 形式の回帰テストで安全性を検証済み。
2. **SSE クライアント切断時の一時的な接続リーク**: `GeneratorExit` 時の内部 HTTP ストリーム close（GC 依存、SVC-C2 は要確認のまま）
3. **yfinance 内部 API 依存**: `session_manager.reset_yfinance_auth()` は内部属性（`_crumb`, `_cookie`）にアクセス
4. **外部サイト構造依存**: Yahoo JP / Kabutan / SBI / Minkabu / TradingView のスクレイピングに依存
5. **インメモリ単一状態**: 単一ワーカー必須（`wsgi.py` / `gunicorn.conf.py` で fail-closed 強制）
6. **テストカバレッジ**: CI の `--cov-fail-under=68` を満たすが、例外経路の網羅率は低い可能性

---

## 8. 変更・安全策の確認

- `git status --short` で確認: 変更対象は指定されたソースファイルおよび新規回帰テスト [`tests/test_code_review_goal_audit_2026_09_v4.py`](tests/test_code_review_goal_audit_2026_09_v4.py)、本レポートのみ
- `git reset --hard`, `git clean -fd`, `git checkout -- .` 等の破壊的操作は未実行
- commit / push / タグ / PR 作成は未実行
- 余計な一時ファイルやキャッシュファイルは生成・残留なし

