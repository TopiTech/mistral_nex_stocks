# コードレビュー報告（HEAD `fffefa2`）

レビュー日: 2026-08-17
対象HEAD: `fffefa2`（前回レビュー `66fa3e5` からの差分を含む）
方針: 前回レビュー（R1–R11、全て解決済み）以降の新規機能（AIポートフォリオ、SSEリプレイ/モード2、暗号化ポートフォリオ永続化、JSON修復）と全到達可能コードパスを再走査し、実害が確定した問題のみを根本修正＋回帰テスト＋検証。

---

## 1. 対応結果

確定した指摘は **2件**（いずれも Low）。その他は問題なし。

### [R1][Low] `/api/screener` の `q` が無制限で、エンリッチメントキャッシュキーが256文字トランケーションで衝突する潜在バグ

- **該当箇所**: `routes/api_stocks.py:708-716`（`q` 検証）、`routes/api_stocks.py:800-810`（enrich_key 構築）、`utils/caching.py:95-114`（`sanitize_cache_key` の256文字切り詰め）
- **影響経路**: `/api/screener?q=<任意長>` → `q` がそのまま `screener_enrich_{market}_{q}_{sha256(symbols)}` に埋め込まれる → `sanitize_cache_key` が256文字で切り詰め → 末尾の銘柄集合ハッシュが消失 → 異なる `(q, 銘柄集合)` の組が同一キャッシュキーに衝突 → TTL（60秒）内に誤った/欠落したエンリッチメント行が配信される。`/api/search` は `q` を200文字に制限しているが、screener には制限がなかった（仕様不整合）。
- **問題・根本原因**: ユーザー入力 `q` の長さ無制限＋キャッシュキーに生の `q` を前置した設計。プローブで実測確認: `q1="a"*300` と `q2="a"*237+"DIFFERENT"`（別銘柄集合）が `sanitize_cache_key` 後に同一キーに衝突（`k1==k2: True`）。現在のエンリッチメント経路は `q` が短い人気銘柄名・セクターの部分文字列である必要があり、>218文字の `q` では実質的に空集合となるため**エンドポイント経由の到達性は限定的**だが、プリミティブレベルでの衝突は確定しており、キー形式の将来変更（長い名称の一致など）で即座に顕在化する設計上の地雷。
- **対応内容**:
  1. `q` を200文字に制限し、超過時は 400 を返す（`/api/search` と同一の契約）。
  2. enrich_key の可変部（`q` と銘柄集合）を両方 SHA-256 ハッシュ化し、キーを約150文字に固定。256文字切り詰めが構造的に発生しなくなり、`(market, q, 銘柄集合)` に対して単射となる。
- **結果**: `修正済み`
- **回帰テスト**: `tests/test_screener.py` に `test_api_screener_rejects_overlong_query`（201文字→400）と `test_api_screener_accepts_max_length_query`（ちょうど200文字→200、境界）を追加。修正前は201文字の `q` が 200 を返していた。
- **参照情報**: なし（仕様は `/api/search` の既存実装を根拠）

### [R2][Low] `/api/ai-portfolio/copy-to-my` の `items` リスト長が無制限

- **該当箇所**: `routes/api_stocks.py:2258-2274`（copy-to-my 入力検証）、`services/ai_portfolio_service.py:48`（`_MAX_ITEMS = 20`）
- **影響経路**: `POST /api/ai-portfolio/copy-to-my` に `items` を大量（例: 数千件）含めて送信 → 各項目が `user_stocks_lock` 保持中にキャッシュ更新（`invalidate_stock_caches` / `ensure_stock_placeholder_in_caches`）を実行し、ロック解放後に `_sync_realtime_symbol(register=True)` でリアルタイムエンジンへ無制限にシンボル登録 → ロック保持時間の延伸とリソース消費。生成・サニタイズ層は `_MAX_ITEMS = 20` で切り詰めており、エンドポイントの契約と不整合。
- **問題・根本原因**: `items` リスト長の上限チェック欠如。単一リクエストで数千件のシンボル登録とキャッシュ操作を許す。
- **対応内容**: エンドポイントで `len(items) > MAX_AI_PORTFOLIO_ITEMS`（=20）を 400 で拒否。定数は `services/ai_portfolio_service.py` に公開エイリアス `MAX_AI_PORTFOLIO_ITEMS = _MAX_ITEMS` を追加し（プロジェクト既存の慣習: `is_yfinance_rate_limit_error` と同様）、生成層と同じ上限を単一の真実源として共有。
- **結果**: `修正済み`
- **回帰テスト**: `tests/test_ai_portfolio.py` に `test_api_copy_to_my_rejects_too_many_items` を追加。21件→400・状態無変更、ちょうど20件→200 を検証。
- **参照情報**: なし

---

## 2. 変更ファイル一覧

| ファイルパス | 変更概要 | 対応ID |
|---|---|---|
| `routes/api_stocks.py` | screener `q` 200文字制限＋enrich_key の q/銘柄集合ハッシュ化、copy-to-my `items` 上限チェック、`MAX_AI_PORTFOLIO_ITEMS` import | R1, R2 |
| `services/ai_portfolio_service.py` | `MAX_AI_PORTFOLIO_ITEMS` 公開エイリアス追加 | R2 |
| `tests/test_screener.py` | 回帰テスト2件（overlong q 400 / 境界200文字 200） | R1 |
| `tests/test_ai_portfolio.py` | 回帰テスト1件（items 21件→400 / 20件→200） | R2 |

---

## 3. 検証結果

### 成功
- `pytest tests/` 全体: **exit 0**（ベースラインと同等、失敗なし）
- 変更対象ファイル: `test_screener.py` / `test_ai_portfolio.py` / `test_market_data_service.py` — 全パス
- 追加回帰テスト3件: 全パス
- `mypy`: Success, no issues found in 50 source files
- `ruff check . --line-length=100`: All checks passed
- `pylint --errors-only`（変更4ファイル）: エラーなし
- `flake8 --select=E9,F63,F7,F82`（変更4ファイル）: 0件

### 失敗/スキップ
- なし。ベースライン（tests/mypy/ruff 成功）と同一状態を維持。

---

## 4. 互換性・移行

| 対応ID | 影響 | 対応 |
|---|---|---|
| R1 | screener エンリッチメントのインメモリキャッシュキー形式変更（`q` ハッシュ化） | アプリ再起動後、旧形式キーのキャッシュエントリは参照されなくなる。TTL=60秒のため実害なし（前回 R6 の sanitize_cache_key 変更と同種の影響） |
| R1 | `q` 201文字以上は 400 | フロントエンドのスクリーナーUIは実用的に短いクエリのみ送信するため影響なし |
| R2 | `items` 21件以上は 400 | 生成層が最大20件しか生成しないため影響なし。公開API契約・データ構造の変更なし |

---

## 5. 調査範囲・残存リスク

- **調査済み経路**: エントリポイント（`app.py` / `app_bg.py` / WSGI）、認証・認可境界（`require_trusted_or_admin`、CSRF、Origin チェック、SSEチケット）、暗号化永続化（`config_store.py` / `crypto_utils.py` / `credential_manager.py`）、並行処理（`realtime_engine.py` クライアントライフサイクル、`utils/threading.py`、キャッシュロック階層）、例外処理・入力検証（数値・シンボル・重量検証）、SSE（モード1/2、リプレイ、ハートビート）、ネイティブホスト（`native_host/`）、フロントエンド（XSS面: `innerHTML` 不使用、CSP、テンプレートエスケープ）、検索・ニュース・AI分析系API、前回指摘 R1–R11 の修正痕跡。
- **問題なしと判定した領域**: 前回レビュー以降の差分の大半（SSEリプレイ、暗号化保存、AIポートフォリオ生成/保存、JSON修復、FX換算）は堅牢。プローブ（NaN重量、無効シンボル、過割当、超過株数、古いFXレート、2重送信）でも全ケースが安全に拒否/ハンドリングされることを確認。
- **対象外**: 外部API（yfinance / Mistral / 検索プロバイダ）への依存そのもの、OS レベルの鍵管理（Windows Credential Manager / keyring）の実装詳細。
- **残存リスク（軽微）**: スクリーナーエンリッチメント経路は、`q` が200文字制限を超えるケースで実質的に空集合となるため、R1 の衝突は現状エンドポイント経由では顕在化しにくい（プリミティブレベルでは確定）。ハッシュ化により構造的に解消済み。CI の security-scan（bandit / pip-audit）と frontend ジョブ（tsc / build / verify-generated）はローカル実行済みのうち `npm run typecheck` と `verify-generated` を確認済みだが、bandit/pip-audit のフル実行は本セッションでは未実施（依存関係の変更なしのためリスク低）。
