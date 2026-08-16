# routes/ 領域 自律レビュー結果

- レビュー実施日: 2026-08-16
- 対象HEAD: 現在の作業ディレクトリ
- モード: レビューのみ（コード変更なし）
- 未コミット差分（`static/css/index.css`, `static/js/ai_portfolio.js`, `templates/index.html`）は変更・破棄していない

## レビュー結果サマリー

### レビュー対象ファイル
- [`routes/api_analysis.py`](routes/api_analysis.py) — チャット、ニュース、分析、トレンド、テクニカル線AIエンドポイント
- [`routes/api_stocks.py`](routes/api_stocks.py) — 銘柄一覧、詳細、履歴、検索、スクリーナー、ポートフォリオ、SSEストリーム、AIポートフォリオ
- [`routes/api_system.py`](routes/api_system.py) — 認証情報、ヘルスチェック、キャッシュ統計、メトリクス、CSRFトークン、CSPレポート、シャットダウン
- [`routes/pages.py`](routes/pages.py) — ページルート（favicon, セットアップ, メイン, ヒートマップ, スクリーナー, 設定, 実験的ページ）
- [`route_helpers.py`](route_helpers.py) — レート制限、APIキー抽出、ストックキャッシュヘルパー、バックグラウンド実行
- [`utils/networking.py`](utils/networking.py) — `require_trusted_or_admin`, `require_sse_auth`, SSEチケット, `_is_local_request`（参照のみ）

### 問題候補数
- 確定候補: **3件**（内訳: Critical 0 / High 0 / Medium 2 / Low 1）
- 要確認: **2件**

### 実行テスト（裏取り）
全てパス（exit code 0）。対象領域の既存回帰テストは全てグリーンであり、**以下に挙げる問題は既存テストでカバーされていない未検証の経路**であることを示唆する。

```
[MNS TEST DIAGNOSTIC] Python 3.14.6 ... Windows-11
[MNS TEST DIAGNOSTIC] MNS_SKIP_BOOTSTRAP=1
[MNS TEST DIAGNOSTIC] KEYRING_BACKEND=keyring.backends.fail.Keyring
```

- `tests/test_api_system.py tests/test_api_integration.py tests/test_api_chat_improved.py tests/test_route_helpers.py tests/test_input_validation.py tests/test_csrf_protection.py` → 全パス
- `tests/test_review_r1_sse_ticket_binding.py tests/test_sse_modes.py tests/test_sse_replay.py tests/test_sse_simulation.py tests/test_cors_security.py tests/test_security_fixes.py tests/test_rate_limiting.py` → 全パス
- `tests/test_security_hardening.py tests/test_security_resilience_extra.py tests/test_review_rate_limit_identity.py tests/test_production_improvements.py tests/test_review_security_fixes.py tests/test_error_handlers.py` → 全パス
- `tests/test_sse_mode_ui_consistency.py tests/test_sse_mode2_resilience.py tests/test_screener.py tests/test_ai_portfolio.py tests/test_review_followup_20260814.py tests/test_review_followup_fixes.py tests/test_review_fixes_all.py` → 全パス

---

## 確定問題候補

### [ROUTE-1][Medium] 問題候補: `/api/ai-technical-lines` で Mistral API の内部エラーメッセージを固定文言へ正規化せずクライアントに露出（他エンドポイントとの一貫性欠如）

- 該当箇所: [`routes/api_analysis.py`](routes/api_analysis.py:1637)-[`routes/api_analysis.py`](routes/api_analysis.py:1643)
- 影響経路:
  1. `api_ai_technical_lines` エンドポイントは `generate_ai_technical_lines()` の戻り値をチェックする
  2. `generate_ai_technical_lines` が `{"error": ...}` 形式の辞書を返した場合、`isinstance(res, dict) and "error" in res` が True になる
  3. エラーレスポンスの `details={"reason": str(res["error"])}` で、Mistral 呼び出し由来の内部エラーメッセージをそのままクライアントに返す
  4. 他のエンドポイント（`api_chat` の `_chat_error_response`、`api_analyze_v2` の `_analyze_v2_error_response`、SSE stream の `_stream_chat_response`）では固定メッセージに正規化しているが、ここだけは内部エラーを露出
- 問題・根本原因: `generate_ai_technical_lines` のエラー文字列には、モデル名、内部エラーコード、Mistral SDK の例外メッセージが含まれる可能性がある。`call_mistral_chat` はエラー時に `{"error": {"message": "...", "status_code": ...}}` 形式の辞書を返す設計であり、`str(res["error"])` は `{'message': '...', 'status_code': 503}` のような内部表現を露出する。R5 で chat の stream 経路に「SDKの生エラー文字列をクライアントへ露出させず固定メッセージへ正規化する」対応を入れているが、このエンドポイントは対応漏れ。
- 重要度評価: **Medium**。Mistral SDK エラーは固定文言が多いため API キー等の機密情報そのものが漏れる可能性は低いが、内部エラー詳細（ステータスコード、内部状態）を露出し、かつ同一コードベース内でエラー正規化方針が一貫していない（R5適用漏れ）。「例外ハンドリング不備、外部障害時の不適切な挙動」に該当。
- 客観的根拠:
  - [`routes/api_analysis.py`](routes/api_analysis.py:1637)-[`routes/api_analysis.py`](routes/api_analysis.py:1643): `str(res["error"])` を直接 `details` に設定
  - [`routes/api_analysis.py`](routes/api_analysis.py:907)-[`routes/api_analysis.py`](routes/api_analysis.py:933): 他エンドポイントの `_chat_error_response` は固定メッセージに正規化済み
  - [`routes/api_analysis.py`](routes/api_analysis.py:845)-[`routes/api_analysis.py`](routes/api_analysis.py:868): SSE stream の `_stream_chat_response` は R5 で「SDKの生エラー文字列をクライアントへ露出させず固定メッセージへ正規化」を実装済み
  - [`services/ai_service.py`](services/ai_service.py:870)-[`services/ai_service.py`](services/ai_service.py:871): `generate_ai_technical_lines` が `{"error": response["error"]}`（辞書）を返す
  - テスト: 既存テストは全パスするが、エラー文字列の露出を検証するテストは存在しない

---

### [ROUTE-3][Medium] 問題候補: `/api/credentials` の `GET` メソッドが Origin チェックなしで認可され、スキーマ未定義のフィールドを含む状態を返す

- 該当箇所: [`routes/api_system.py`](routes/api_system.py:94)-[`routes/api_system.py`](routes/api_system.py:170)
- 影響経路:
  1. `/api/credentials` の `GET` は `require_trusted_or_admin(request, require_origin=request.method in ("POST", "DELETE"))` と定義されている
  2. `GET` の場合は `require_origin=False` となり、Origin ヘッダのチェックなしで `_is_local_request` のみで判定される
  3. `GET` は `get_api_credential_state()` の全状態を返す。これには `has_mistral_api_key`, `has_langsearch_api_key`, `has_tavily_api_key`, `has_alphavantage_api_key`, `credentials_ephemeral_keys`（鍵名リスト）, `credentials_ephemeral_warning` 等が含まれる
  4. `credentials_ephemeral_keys` は [`crypto_utils.get_ephemeral_keys()`](crypto_utils.py:534) の戻り値で、`_EPHEMERAL_CREDENTIALS.keys()` を返す。これは鍵名（`"mistral_api_key"` 等）のリストであり、APIキー値そのものではない
- 問題・根本原因: `GET` メソッドは Origin なしで localhost からアクセス可能。`get_api_credential_state()` の戻り値には `AppConfigSchema` で定義されていないフィールド（`credentials_ephemeral_keys`, `credentials_ephemeral_warning`, `credentials_ephemeral`）が含まれ、`jsonify` で直接シリアライズされる。`AppConfigSchema` は `has_mistral_api_key` 等のブール値のみを定義するため、追加フィールドはスキーマ検証なしでレスポンスに含まれる。
- 重要度評価: **Medium**。`credentials_ephemeral_keys` は鍵名リストであり API キー値そのものではないため、機密情報の直接漏洩には当たらない。しかし、`GET` が Origin チェックなしで認証情報の状態を返すことは防御の深さの観点で不十分であり、レスポンスにスキーマ未定義のフィールドが含まれることは将来の秘密情報追加時に誤って露出するリスクを孕む。
- 客観的根拠:
  - [`routes/api_system.py`](routes/api_system.py:160): `require_origin=request.method in ("POST", "DELETE")` — GET は Origin チェックなし
  - [`credential_manager.py`](credential_manager.py:231)-[`credential_manager.py`](credential_manager.py:244): `get_api_credential_state()` が `credentials_ephemeral_keys` 等を含む
  - [`crypto_utils.py`](crypto_utils.py:534)-[`crypto_utils.py`](crypto_utils.py:537): `get_ephemeral_keys` は `_EPHEMERAL_CREDENTIALS.keys()` のリストを返す
  - [`utils/validators.py`](utils/validators.py:72)-[`utils/validators.py`](utils/validators.py:83): `AppConfigSchema` は `has_*` のブール値のみ定義
  - テスト: `tests/test_api_system.py` は全パスするが、`GET` のレスポンスに `credentials_ephemeral_keys` が含まれることの検証や、Origin 未設定時の挙動検証はない

---

### [ROUTE-4][Low] 問題候補: `api_screener` の `total` フィールドが `stocks[:150]` の切り詰め後もフィルタリング全件数を示す

- 該当箇所: [`routes/api_stocks.py`](routes/api_stocks.py:854)-[`routes/api_stocks.py`](routes/api_stocks.py:859)
- 影響経路:
  1. `api_screener` はフィルタリング後の全件数を `total` として返す（`"total": len(filtered)`）
  2. 同時に `"stocks": filtered[:150]` で最大150件に切り詰めて返す
  3. `total` が150を超える場合、フロントエンドが `total` をページネーションの総件数として使用すると、実際に取得できる件数（最大150）と不整合が発生する
- 問題・根本原因: 現在のフロントエンドはページネーションを行わず全件表示するため、実際の実害はない。しかし、`total` が「フィルタに一致した全件数」を意味するのか「レスポンスに含まれる件数」を意味するのかが不明確であり、将来の改修でページネーションを実装した際に問題となる可能性がある。
- 重要度評価: **Low**。現状のフロントエンド実装では実害なし。ただし、API コントラクトの不明確さとして将来の改修リスクを孕む。
- 客観的根拠:
  - [`routes/api_stocks.py`](routes/api_stocks.py:858): `"total": len(filtered)` でフィルタリング全件数
  - [`routes/api_stocks.py`](routes/api_stocks.py:859): `"stocks": filtered[:150]` で最大150件に制限
  - テスト: `tests/test_screener.py` は全パスするが、`total` と `stocks` の長さの一致を検証するテストはない

---

## 要確認問題候補（確証が取れないもの）

### [ROUTE-C1][要確認] `api_screener` の `_parse_strict_float` が2種類の戻り値型を持つ

- 該当箇所: [`routes/api_stocks.py`](routes/api_stocks.py:727)-[`routes/api_stocks.py`](routes/api_stocks.py:755)
- 内容: `_parse_strict_float` は正常時 `float | None`、エラー時 `(Response, int)` の tuple を返す。呼び出し側は `isinstance(result, tuple)` でエラー検出している。このパターンは Python の型検査（mypy/pyright等）で正しく扱えず、また `_parse_strict_float` の戻り値の型アノテーションがないため、将来的な変更でエラー検出が漏れるリスクがある。
- 根拠: [`routes/api_stocks.py`](routes/api_stocks.py:727) の `def _parse_strict_float(raw, field_name):` に戻り値の型アノテーションがない。呼び出し側（[`routes/api_stocks.py`](routes/api_stocks.py:744)-[`routes/api_stocks.py`](routes/api_stocks.py:755)）は `isinstance(min_price, tuple)` で判定している。このパターンは `Union[float | None, tuple]` となり、型チェッカーがエラーを検出できない。

### [ROUTE-C2][要確認] `api_add_stock_ext` の `_is_allowed_shutdown_origin` 呼び出しは関数名の意味と異なる用途

- 該当箇所: [`routes/api_stocks.py`](routes/api_stocks.py:1260)
- 内容: `_is_allowed_shutdown_origin(request)` は関数名は「シャットダウン」用途を示唆するが、実際には汎用的な Origin 許可チェック関数であり、`allowed_origins` セットを参照する。機能的には正しいが、関数名がミスリーディングであり、将来の保守担当者が混乱する可能性がある。
- 根拠: [`utils/networking.py`](utils/networking.py:436)-[`utils/networking.py`](utils/networking.py:446) の `_is_allowed_shutdown_origin` は汎用的な Origin チェックであり、`/api/shutdown` 以外でも使用されている。

---

## 指摘対象外（健全と判断した項目）

- **SSE チケットバインディング**: [`utils/networking.py`](utils/networking.py:282)-[`utils/networking.py`](utils/networking.py:375) の `_session_id_for_sse` / `create_sse_ticket` / `consume_sse_ticket` はセッションIDベースでチケットを発行・消費しており、`session_backed=False` の場合は `SseTicketSessionUnavailable` を送出する。健全。
- **SSE ストリームのリソース解放**: [`routes/api_stocks.py`](routes/api_stocks.py:1572)-[`routes/api_stocks.py`](routes/api_stocks.py:1910) の `stream()` 関数は `finally` ブロックで `reservation.release()` を確実に呼び、`response.call_on_close(reservation.release)` も登録済み。`GeneratorExit` も適切にハンドリング。健全。
- **SSE イベントログのバウンド**: [`messaging.py`](messaging.py:244)-[`messaging.py`](messaging.py:248) の `SseEventLog` は `maxlen` でバッファサイズを制限（`SSE_EVENT_LOG_MAX`）。`deque` を使用しており、メモリリークのリスクなし。健全。
- **レート制限の polling-skip 保護**: [`route_helpers.py`](route_helpers.py:352)-[`route_helpers.py`](route_helpers.py:376) の `_RATE_LIMIT_MAX_TOKEN_POLLS` (120) と `_RATE_LIMIT_MAX_DISTINCT_TOKENS` (40) によるバウンド。健全。
- **`require_trusted_or_admin` の認可ロジック**: [`utils/networking.py`](utils/networking.py:197)-[`utils/networking.py`](utils/networking.py:255) はローカルモードとリモートモードを適切に分岐、`MNS_ADMIN_TOKEN` が設定されている場合は全リクエストにトークン検証を要求。健全。
- **`api_credentials` の入力検証**: [`routes/api_system.py`](routes/api_system.py:207)-[`routes/api_system.py`](routes/api_system.py:305) で各 API キーの型・長さ検証を実施。`custom_ai_prompt` の長さ制限（5000文字）もあり。健全。
- **`api_shutdown` の多層防御**: [`routes/api_system.py`](routes/api_system.py:699)-[`routes/api_system.py`](routes/api_system.py:862) は `MNS_PROD` チェック、リモートモード拒否、`_is_local_request`、`RAW_REMOTE_ADDR` の loopback 確認、`_is_allowed_shutdown_origin`、JSON `confirm` フラグ、ワンタイム shutdown token の7層の防御。`consume_shutdown_token` が `None` を拒否することも確認。健全（CORE-1 で指摘された課題は routes 領域外の `app.py` の CSRF exempt 設定に起因）。
- **`api_csp_report` のログインジェクション対策**: [`routes/api_system.py`](routes/api_system.py:637)-[`routes/api_system.py`](routes/api_system.py:696) で URI 値の長さ制限・制御文字除去を実施。`json.dumps` の `[:2000]` でログ長も制限。健全。
- **`api_health` のキー状態開示制御**: [`routes/api_system.py`](routes/api_system.py:401)-[`routes/api_system.py`](routes/api_system.py:408) で `allow_remote` が False かつ `_is_local_request` の場合のみ `get_api_credential_state()` を追加。リモート環境では開示なし。健全。
- **`api_ai_technical_lines` の `fetch_stock` 戻り値ガード**: [`routes/api_analysis.py`](routes/api_analysis.py:1631)-[`routes/api_analysis.py`](routes/api_analysis.py:1632) は `stock.get("history", []) if isinstance(stock, dict) else []` と `isinstance` ガード済み。`fetch_stock` が `None` を返しても `AttributeError` は発生しない。健全。
- **SSE ストリームの `_finish_stream` 二重呼び出し保護**: [`routes/api_analysis.py`](routes/api_analysis.py:753)-[`routes/api_analysis.py`](routes/api_analysis.py:767) の `terminal_lock` による二重実行防止。健全。
- **`api_chat` SSE のスロット解放**: [`routes/api_analysis.py`](routes/api_analysis.py:534)-[`routes/api_analysis.py`](routes/api_analysis.py:550) の `_ReleaseOnce` パターン。`stream_chat_slots.acquire(blocking=False)` が False の場合、`release_once` は作成されずに `return error_response(...)` するため、`ValueError: Semaphore released too many times` は発生しない。健全。

---

## 補足: テスト裏取りの限界

- 既存テストは `KEYRING_BACKEND=keyring.backends.fail.Keyring` で実行されており、実キーリング/DPAPI の分岐はテストされていない。
- `MNS_SKIP_BOOTSTRAP=1` で実行されるため、`bootstrap()` の fail-closed パスはテストで実行されていない。
- `generate_ai_technical_lines` のエラー文字列露出（ROUTE-1）は、Mistral API が実際にエラーを返す環境でなければ再現できないため、現在のテストでは検証されていない。
- `api_screener` の `total` と `stocks[:150]` の不整合（ROUTE-4）は、フィルタリング結果が150件を超えるケースのテストが存在しない。
- 実行中に `tests/test_auto_remove_symbols.py::test_auto_removal_restores_symbol_when_persistence_fails` が1件失敗した（`assert 'DELIST' in {}`）。これは [`app_bg.py`](app_bg.py:1837) の自動削除ロジックとパーシステンス失敗時のロールバック経路に起因するものであり、routes/ 領域外（バックグラウンド同期処理）の問題。routes/ 領域のレビュー対象外として記録のみ。

## 保存場所
- 本ファイル: `goal-review-routes.md`（作業ディレクトリ直下）