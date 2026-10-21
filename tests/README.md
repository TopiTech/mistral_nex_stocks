# Tests Suite & Test Support Architecture

本ディレクトリ（`tests/`）は、**Mistral Nex Stocks** の全テストケースおよびテストサポート基盤を格納しています。
CI（GitHub Actions）で 75% 以上のラインカバレッジが必須化されており、2,900件以上のテストが高速・安全に並列実行されます。

---

## 1. ディレクトリ構造と責務

```
tests/
├── __init__.py                # テストパッケージ定義 & test_support への後方互換 re-export
├── conftest.py                # pytest 設定、フック、セッション/テスト毎の fixture 定義
├── startup_smoke_runner.py    # CI 用の完全ブートストラップ検証スタンドアロンスモーク
├── README.md                  # 本ドキュメント（構成および棚卸しガイド）
├── test_support/              # 責務分割されたテストサポートモジュール群
│   ├── __init__.py            # 公開ユーティリティインターフェース
│   ├── state_reset.py         # アプリ状態・キャッシュ・セッションリセット
│   ├── temp_config.py         # 一時設定ファイルの生成・自動削除
│   ├── keyring_mock.py        # インメモリキーリング（OS キーリング分離）
│   ├── executors.py           # 同期テスト用 SynchronousExecutor
│   ├── environment.py         # テスト用ベースライン環境変数の管理
│   └── patches.py             # yfinance/キャッシュ/ストレージのモンキーパッチ集
└── test_*.py                  # 個別テストモジュール（約220ファイル）
```

---

## 2. テストサポートユーティリティ (`test_support/`)

テストの肥大化・密結合を解消するため、単一責任の原則に従ってユーティリティをモジュール分割しています。

| モジュール | 主なクラス / 関数 | 責務・役割 |
|:---|:---|:---|
| `state_reset.py` | `reset_app_state_internals()` | AIキャッシュ、マーケット状態、リアルタイムエンジン、リーダー選出ロック、SSEチケット等のプロセス内状態を完全初期化 |
| `temp_config.py` | `create_temp_config()`, `cleanup_temp_files()` | テスト用の一時 JSON 設定ファイルを生成し、セッション終了時または明示的なクリーンアップで確実に削除 |
| `keyring_mock.py` | `MemoryKeyring` | OS のキーリングや DBus デーモンへの依存を遮断し、メモリ内で安全にシークレットをモック管理 |
| `executors.py` | `SynchronousExecutor` | `ThreadPoolExecutor` を同期実行に置き換え、非同期タイミングレースやテスト終了時のスレッドリークを防止 |
| `environment.py` | `apply_baseline_test_env()`, `get_baseline_test_env()` | `MNS_SKIP_BOOTSTRAP`、`MNS_DATA_DIR`、`MNS_MISTRAL_MIN_INTERVAL` などのベースライン変数を定義・適用 |
| `patches.py` | `apply_all_test_patches()` | yfinance の SQLite キャッシュ分離、ストレージ・ディスクキャッシュパスの隔離、バックグラウンドスタブの適用 |

### 後方互換性
既存のテストコードで使用されている以下のインポートパスはすべて維持されており、壊れることなく動作します。
```python
# 従来のインポート（tests/__init__.py 経由）
from tests import cleanup_temp_files, create_temp_config, reset_app_state_internals

# 従来の conftest インポート
from tests.conftest import MemoryKeyring, SynchronousExecutor

# 新しい推奨インポート
from tests.test_support import (
    MemoryKeyring,
    SynchronousExecutor,
    cleanup_temp_files,
    create_temp_config,
    reset_app_state_internals,
)
```

---

## 3. テストフォルダの棚卸し（カテゴリ別分類）

テストフォルダ内の全ファイルを機能と目的に基づいて 9 つのカテゴリに分類・整理しています。

| カテゴリ | ファイル数 | 対象コンポーネント | 代表的なテストファイル |
|:---|:---:|:---|:---|
| **1. AI & LLM サービス・検索** | 24 | Mistral API、OpenAI 互換、DDGS、Tavily、LangSearch、Embeddings、Reasoning | `test_ai_service.py`, `test_mistral_api_improvements.py`, `test_search_service.py`, `test_openai_compat_service.py` |
| **2. マーケットデータ & 銘柄エンジン** | 19 | yfinance、PTS、スクレイパー、RealtimeMarketEngine、日経225、TradingView | `test_realtime_engine.py`, `test_stock_provider.py`, `test_tradingview_mapper.py`, `test_screener.py` |
| **3. ルーティング & API & ネットワーク** | 12 | Flask ブループリント、SSE ストリーミング、ネイティブホスト | `test_api_system.py`, `test_api_integration.py`, `test_sse_replay.py`, `test_native_host.py` |
| **4. セキュリティ & 認可** | 8 | CSRF、CORS、CSP、ネイティブホスト検証、ヘッダー保護 | `test_security_hardening.py`, `test_cors_security.py`, `test_csrf_protection.py`, `test_csp_header.py` |
| **5. ストレージ & キャッシュ & セッション** | 8 | ディスクキャッシュ、SQLite チャット履歴、セッション管理、指数キャッシュ | `test_storage_coverage.py`, `test_disk_cache.py`, `test_session_manager.py`, `test_chat_history.py` |
| **6. コアロジック & ユーティリティ** | 12 | スキーマ、バリデーション、フォーマット、設定ユーティリティ、エラーハンドラ | `test_validators.py`, `test_schemas.py`, `test_config_utils.py`, `test_route_helpers.py` |
| **7. テスト基盤 & ユーティリティ** | 5 | テストサポート機能、フィクスチャ検証、スモーク実行 | `test_test_utils.py`, `test_startup_smoke.py`, `startup_smoke_runner.py` |
| **8. 過去のレビュー/監査リグレッション防止** | 89 | 過去の監査・レビュー（R1〜R33、Goal fixes 等）で検出された不具合の再発防止 | `test_review_goal_fixes.py`, `test_audit_review_goal_final_2026.py`, `test_code_review_audit_2026.py` |
| **9. カバレッジ補完 & 運用ライフサイクル** | 44 | バックグラウンド同期、リーダー選出、シャットダウン、ドキュメント整合性 | `test_coverage_core.py`, `test_leader_election.py`, `test_doc_consistency.py`, `test_app_state_lifecycle.py` |

---

## 4. テスト実行ガイド

```bash
# 1. 全テストの通常実行（xdist による並列実行）
pytest

# 2. カバレッジ計測付き実行（CI と同等の 75% 閾値チェック）
pytest --cov=. --cov-report=term --cov-fail-under=75

# 3. テストサポートユーティリティ自体のテスト実行
pytest tests/test_test_utils.py -v

# 4. ブートストラップスモークテストのスタンドアロン実行
python tests/startup_smoke_runner.py
```
