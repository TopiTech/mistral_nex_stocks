# 全網羅コードレビュー課題リスト & 根本原因解決計画

本ドキュメントは、プロジェクト `mistral_nex_stocks_complete_fixed_v3` の全ファイルを網羅的に精査したコードレビュー結果、および根本原因（Root Cause）分析と修正計画をまとめた記録である。

---

## 課題一覧と根本原因評価（Root Cause Analysis）

| ID | 領域 | 重要度 | 対象ファイル | 課題の概要 | 根本原因 (Root Cause) | 本質的解決策 (Permanent Fix) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **ISSUE-01** | バックエンド / AI | **Critical** | `services/ai_service.py` | サーキットブレーカー HALF_OPEN 時のプローブ解放漏れによる AI 機能の恒久的不全 | `claim_circuit_probe("mistral")` 後の処理が `try...finally` で保護されておらず、予期せぬ例外でプローブフラグ `probing=True` がメモリ上に永続化する | 全体の呼び出しを `try...finally` で囲み、成功/失敗/例外の如何に関わらずプローブを確実に解放する構造にする |
| **ISSUE-02** | サービス / データ | **High** | `services/ai_portfolio_service.py`, `schemas/ai_portfolio.py` | AI ポートフォリオ生成失敗時の `fallback_warning` がサニタイズでサイレント破棄される | `sanitize_ai_portfolio` のホワイトリストに `fallback_warning` が含まれておらず、フォールバック警告が除去されて古いデータが正常データとして返却される | `sanitize_ai_portfolio` の許可キーに `fallback_warning` を追加し、HTML除去・長さ制限を行って安全に透過させる。Pydantic スキーマ側にもフィールドを整合させる |
| **ISSUE-03** | バックグラウンド / 並行性 | **High** | `bg/leader_election.py` | Windows環境で `psutil` 未インストール時にリーダー選出がスプリットブレインを起こす | `_pid_is_alive` が Windows で `psutil` なしの場合に無条件に `False` を返すため、稼働中プロセスのロックが強制強奪され多重起動する | Windows標準の `ctypes` による Win32 `OpenProcess` API を実装し、ゼロ依存で正確な PID 生存判定を行う。判定不能時はフェイルセーフに扱う |
| **ISSUE-04** | 設定 / 認証 | **High** | `credential_manager.py` | 個別 API キーの空文字更新による削除・クリアが不可能 | `save_api_credentials` が `if value is not None and str(value).strip()` で空文字を無視するため、キーをクリアできず旧値が残り続ける | `value == ""` を明示的クリア要求として認識し、keyring・config・`os.environ` から該当キーを安全に削除する |
| **ISSUE-05** | UI / セキュリティ (CSP) | **High** | `static/js/tradingview_manager.js`, `static/js/*.js` | `getAttribute("nonce")` が空文字を返し、厳格な CSP 環境で TradingView 等のスクリプトがブロックされる | W3C CSP Nonce Hiding 仕様により `getAttribute("nonce")` は空文字を返す。IDL プロパティ `script.nonce` を使わなければならない | `document.querySelector("script[nonce]")?.nonce` を使用し、動的生成スクリプトに `script.nonce = nonce` を正しく設定する。全ファイルを横断点検 |
| **ISSUE-06** | UI / リソース | **High** | `static/js/api_client.ts`, `static/js/api_client.js` | バックグラウンドタブ遷移時に SSE 切断タイマーが設定されずストリーミングが漏洩する | チケット発行非同期通信中に `this.currentEventSource` が null のため非表示タイマーがスケジュールされず、バックグラウンドで EventSource が開かれる | `_isConnecting` 状態を導入し、非表示ハンドラで捕捉。URL 解決完了時にも一時停止状態であれば接続を中断するガードを設ける |
| **ISSUE-07** | UI / メモリリーク | **Medium** | `static/js/api_client.ts`, `static/js/api_client.js` | EventSource のラッパーリスナーが WeakMap 上書きで解除不能になる | 同一リスナーを再登録した際に古いラッパー参照が失われ、`removeEventListener` で解除できずリスナーが重複蓄積する | リスナーごとに登録されたラッパーのコレクションを適切に管理・解除できるように修正する |
| **ISSUE-08** | テスト / CI | **High** | `tests/test_audit_api_boundary_fixes_2026.py` | `test_pip_audit_cleanliness` が外部の古い Python 環境を監査してテスト失敗する | `PIPAPI_PYTHON_LOCATION` が設定されず、グローバルの `pip-audit` がグローバル Python 3.14 のキャッシュパッケージを監査してしまう | `uv run --locked --group security pip-audit --strict` または `PIPAPI_PYTHON_LOCATION=sys.executable` を明示してテスト実行環境を正確に監査する |
| **ISSUE-09** | サービス / 非同期クリーンアップ | **Medium** | `services/ai_service.py` | `_run_coro_sync` 内でイベントループ終了時に非同期リソースが未解放（`AsyncClient.aclose was never awaited`） | `new_loop.close()` 直前に非同期ジェネレータや残余タスクのシャットダウン処理が行われていない | ループ閉鎖前に `new_loop.run_until_complete(new_loop.shutdown_asyncgens())` 等の正規クリーンアップを実施する |
| **ISSUE-10** | クライアント / 拡張機能 | **Medium** | `chrome_extension/manifest.json` | `manifest.json` に `content_scripts` の宣言がなく `content.js` が自動実行されない | manifest.json に content_scripts の定義が欠落している | 対象金融サイトにマッチする `content_scripts` 宣言を追加する |
| **ISSUE-11** | クライアント / 拡張機能 | **Low** | `chrome_extension/popup.js` | 非推奨の `unload` イベントリスナー使用によるポーリング停止の不確実性 | MV3 で非推奨の `window.addEventListener("unload", ...)` を使用している | `pagehide` および `visibilitychange` に置き換えて確実なライフサイクル停止を行う |
| **ISSUE-12** | リアルタイム / SSE | **Medium** | `services/realtime/engine.py` | 銘柄削除時の SSE tombstone イベント未送信によるクライアント側の古い価格行残留 | `unregister_symbol` 時にクライアントキューへ削除イベントがエンキューされない | 銘柄登録解除時に削除マーカー付きの更新データを SSE クライアントキューに投入する |

---

## 修正・検証サイクル計画

依存関係の根底から順に論理的単位ごとに進める：
1. **フェーズ 1: テスト環境・依存性整合性（ISSUE-08）**
   - `test_pip_audit_cleanliness` の修正と実行検証
2. **フェーズ 2: 共通基盤・セキュリティ・設定（ISSUE-03, ISSUE-04）**
   - `bg/leader_election.py` の Windows ctypes フォールバック
   - `credential_manager.py` の空文字による個別キー削除対応
3. **フェーズ 3: サービス層・状態管理・非同期（ISSUE-01, ISSUE-02, ISSUE-09, ISSUE-12）**
   - `services/ai_service.py` の Circuit Breaker `try...finally` と `_run_coro_sync` クリーンアップ
   - `services/ai_portfolio_service.py` & `schemas/ai_portfolio.py` の `fallback_warning` サニタイズ
   - `services/realtime/engine.py` の tombstone 送信
4. **フェーズ 4: フロントエンド・クライアント・境界（ISSUE-05, ISSUE-06, ISSUE-07, ISSUE-10, ISSUE-11）**
   - `tradingview_manager.js` 等の `script.nonce` 修正
   - `api_client.ts` の In-flight visibility pause & 重複リスナー修正
   - TypeScript コンパイル、ビルド検証、Prettier / ESLint チェック
   - `chrome_extension/` の manifest.json & popup.js 修正
5. **フェーズ 5: 総合検証**
   - 全テスト（pytest 2100+ tests）、全静的解析（mypy, pyrefly, ruff, flake8, pylint, bandit, npm audit）、全ビルドの実行とクリーンパスの確認
