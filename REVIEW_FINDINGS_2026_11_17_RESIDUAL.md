# 2026-11-17 残余課題レビュー（最終パス）

本ドキュメントは、既存の `REVIEW_FINDINGS_2026_11_14*` / `REVIEW_FINDINGS_2026_11_15.md` /
`.agent/issue_list_2026_11_17.md` で記録済みの全ファイル精査を前提に、現在の `HEAD`
（`63ca492`）に対して残余欠陥を再検証した結果をまとめたものである。過去に意図的仕様・
後方互換として除外された項目は再指摘せず、今回は「現在も再現する欠陥」だけを修正対象にした。

## 1. 調査範囲と方法

- 追跡対象: Python 511 ファイル、JavaScript / TypeScript 28 ファイル、Jinja2 テンプレート、
  Chrome/Edge 拡張、native host、CI 設定、テスト 385 ファイル。
- ベースライン: 変更前に全テストスイート、ruff、flake8、mypy、pyrefly（Linux/Windows）、
  pylint、bandit、npm build/audit、pip-audit、startup smoke が成功することを確認。
- 全ファイルを ruff（通常 + bugbear `B`）と横断検索（`json.load` の型検証、認可ゲート、
  `time.sleep`、時刻処理、`assert`、`eval`/`exec`/`pickle`/SQL 文字列、`innerHTML`、
  乱数、localStorage、リスナー解除）の対象にした。
- 候補は必ず該当コードと既存テストを直接読み、再現条件を確認してから採否を決めた。
- 修正は根底（共通基盤 → API 境界）から行い、各修正に回帰テストを追加した。
- 型弱体化（`Any` 拡大・不当な型アサーション）、エラー抑制コメント、テスト skip、
  アサーション緩和は行っていない。

## 2. 修正した課題

| ID | 重要度 | 対象 | 課題 / 根本原因 | 修正 | 回帰テスト |
| :--- | :--- | :--- | :--- | :--- | :--- |
| R18-01 | High | `utils/networking.py` | ネイティブホストのマニフェスト JSON のルートが配列・文字列・数値だと `or {}` で置換されず、`.get` の `AttributeError` が捕捉タプル外へ漏れる。この結果、信頼済み Origin 判定が例外になり、全ての状態変更 API が 500 になる | `isinstance(manifest_data, dict)` を検証し、不正ルートは `ValueError` として既存の縮退処理（`manifest_load_error`）へ流す | `test_non_object_manifest_root_degrades_without_raising` |
| R18-02 | Medium | `routes/api_analysis.py` | `POST /api/analyze-chart-image` が API キー確認より先に最大 7 MiB の body を読み込み・JSON デコード・スキーマ検証していた。未認証の巨大入力でワーカーの CPU/メモリを消費できる | `extract_api_key` を body パースより前に移動し、認証不能な要求は 401 で即時拒否する（他の AI ルートと同じ順序） | `test_chart_image_authenticates_before_parsing_body` |
| R18-03 | Medium | `routes/system/credentials.py` | Mistral 検証の単一 `except Exception` が、DNS 失敗・接続拒否・タイムアウトまで `400 INVALID_API_KEY` に変換していた。ユーザーは正常なキーを無効と誤認してローテーションする | `httpx.TimeoutException` / `TimeoutError` を 504、`httpx.NetworkError` / `ConnectionError` を 503 に分類し、固定メッセージの統一エンベロープで返す。例外本文は返さない | `test_credentials_verify_classifies_provider_outages`（503/504 の 2 ケース） |

## 3. 誤検知・過剰修正として除外 / 撤回した項目

| 項目 | 判断 |
| :--- | :--- |
| `/api/health` への loopback ゲート追加 | **撤回**。既存テスト `test_health_endpoint_hides_credential_state_for_remote` / `test_health_untrusted_origin_omits_credentials` / `test_admin_token_bucket_independent_from_anonymous` / `test_health_does_not_report_ready_for_explicit_false_skip_value` が「health は remote からも 200 で到達可能、ただし credential state は出さない」という意図的契約を固定していた。全テスト実行で 4 件の失敗として検出したため、変更と追加テストを削除した。 |
| `/api/chat` エラーの `code`（int）と `error_code`（`ERR_CHAT_*`）の非対称 | `tests/test_api_chat_error_contract.py` が明示的に固定する後方互換契約。変更すると既存クライアントを壊すため除外。 |
| `/api/ai-technical-lines` / `/api/analyze-chart-image` の同期実行 | ワーカー占有は事実だが、非同期ジョブ化は API 契約（ポーリング形式）の変更を伴う設計課題。今回は過剰変更を避けて保留。 |
| `mistral_compat.py` の `MISTRAL_API_KEY` 環境変数ブリッジ | インストール済み Agents SDK のローカル `Agent` コンストラクタに `api_key` / `client` 引数が存在しないことを `inspect.signature` で確認。呼び出し全体をロックすると長時間直列化するため、設計課題として保留。 |
| `_parse_strict_float` / `_get_operation_token` の未使用コード | 実行時の不具合ではなく、既存テストがシグネチャを固定。削除は今回の欠陥修正と無関係なため除外。 |
| `native_host/native_host.py` のマニフェスト型検証 | 既に広い例外ハンドラで fail-safe に縮退しており、クラッシュしない。backend 側の R18-01 のみ修正。 |
| `PRAGMA user_version = {定数}` の f-string | 埋め込み値はモジュール定数の整数のみで、外部入力は到達しない。SQL インジェクションではない。 |
| `Math.random()` のジッター | 再接続バックオフの揺らぎ用途で、セキュリティトークン生成ではない。暗号論的乱数への置換は不要。 |

## 4. 既知の設計課題（未修正・理由付き）

- `config/io.py` / `config/merge.py` のプロセス間ロック順序（旧 CFG-03/04）。修正には
  `save_config` / `load_config` の大規模な再配置とクロスプロセステストが必要で、
  部分的な移動は AB-BA デッドロックを生むため、専用変更として保留。
- Agents SDK の環境変数キー共有（旧 AI-02）。SDK 側の API 制約による。
- 同期 AI エンドポイントのワーカー占有（上記）。
- native messaging host の同一ユーザー境界。`SECURITY.md` に明記済みの threat model。

## 5. 最終検証結果

| チェック | 結果 |
| :--- | :--- |
| pytest 全件 + coverage（`-n auto --dist=loadfile --timeout=120 --cov-fail-under=75`） | exit 0、coverage 82.89%（75% ゲート通過） |
| 新規回帰テスト（3 件 + 503/504 の 2 ケース） | 全パス |
| 既存の health / chart-image / credentials-verify / CORS 契約テスト | 全パス（47 件の対象実行） |
| ruff（`--line-length=100`） | All checks passed |
| ruff bugbear（`--select B`） | プロダクションコード 0 件（テストの様式 2 件のみ） |
| flake8（E9,F63,F7,F82） | 0 |
| mypy | Success: 125 source files |
| pyrefly（default / `--python-platform win32`） | 0 errors |
| pylint（errors-only、CI 対象一式） | exit 0 |
| bandit（medium+） | 0 issues |
| `npm run build`（tsc + verify-generated + compile + eslint + prettier） | exit 0 |
| `npm audit --audit-level=high` | 0 vulnerabilities |
| `pip-audit --strict` | No known vulnerabilities |
| SRI チェック（テンプレートの CDN script） | PASS |
| startup smoke（`MNS_SKIP_BOOTSTRAP=0`） | exit 0 |
| `git diff --check` | exit 0 |

補足: ローカル環境（uv 0.13.8、Python 3.11/3.12/3.14）で
`uv export --locked --no-hashes --no-dev` を実行すると、`requirements-locked.txt` に対して
PEP 508 の `python_full_version >= '3.12'` マーカー差分が出る。CI は `uv 0.11.25` を固定して
生成しており、この差分は uv のバージョン差による出力形式の差である（3.11/3.12/3.14 の
いずれでも同じ差分が出ることを確認済み）。今回依存ファイルは一切変更しておらず、
本変更の不整合ではない。

## 6. 変更ファイル

- `utils/networking.py`
- `routes/api_analysis.py`
- `routes/system/credentials.py`
- `tests/test_review_2026_11_17_residual_fixes.py`（新規）
