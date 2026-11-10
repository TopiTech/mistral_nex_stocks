# 2026-11-11 全ファイル横断コードレビュー — 課題一覧と修正結果

本ドキュメントは、`mistral_nex_stocks_complete_fixed_v3` の全ソース（Python 125 ファイル、
`static/js`、`templates`、`chrome_extension`、`scripts`、`native_host`、`config`、`tests`）を
対象に、正常系・異常系・エッジケース・非同期競合・リソースリーク・セキュリティ・
パフォーマンスの観点で実施したレビューの課題一覧と、根本原因（Root Cause）に対する
修正・回帰テストの記録である。

## 1. 課題一覧

| ID | 領域 | 重要度 | 対象 | 課題 | 根本原因 | 修正 | 回帰テスト |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| DATA-1 | データ層 | High | `utils/storage.py` | 旧 plaintext `user_stocks.json` の移行で、暗号化 target の fsync 前に旧ファイルを削除し、クラッシュ時に唯一の完全コピーを失う | 移行だけが通常の保存経路の `flush`/`fsync`/ディレクトリ fsync 契約を満たしていなかった | target の file fsync + 親ディレクトリ fsync 後に旧ファイルを削除。fsync 失敗時は旧ファイルを保持 | `tests/test_review_2026_11_11_root_cause_fixes.py` |
| DATA-2 | 設定 | High | `config/atomic_write.py` | 旧 config 移行で target ディレクトリの fsync 前に source を削除し、master key/secret を失う可能性 | target の作成がディレクトリ fsync されていなかった | target 親ディレクトリの fsync 成功後のみ source を削除。失敗時は source を保持 | 同上 |
| DATA-3 | データ層 | High | `utils/storage.py` | `load_user_stocks` の save-in-flight 判定が lock 外にあり、save 開始後に disk の旧状態を publish して更新を失う | check-then-act（TOCTOU）。判定と publish が同じ lock で保護されていなかった | `user_stocks_lock` 取得直後にも pending を再確認 | 同上 |
| DATA-4 | 設定 | Medium | `config/paths.py`, `shutdown_manager.py` | 既存の runtime data dir が 0o755 のまま残り、他ユーザーがファイル名を列挙できる | `mkdir(mode=...)` は新規作成時のみ有効で、既存ディレクトリを締めない | `_ensure_runtime_dir` と shutdown token 用 helper で POSIX の既存 dir を 0o700 に chmod | 同上（POSIX のみ） |
| DATA-5 | データ層 | Low | `utils/chat_history.py` | `BaseException` で transaction が open のまま残り、同一 thread の次の `BEGIN` が失敗する | `except Exception` が `KeyboardInterrupt`/`SystemExit`/`CancelledError` を捕捉しない | `finally` で `conn.in_transaction` なら rollback | 同上 |
| SVC-1 | AI/データ | High | `services/ai_portfolio_service.py` | schema 上有効な `target_price: 0.0` の item を sanitize が破棄し、空 portfolio で既存保存を上書きし得る | Pydantic schema と sanitizer のルール不一致（0 を invalid 扱い） | 0.0 は `None` として item を保持。空 canonical result は persist せず既存/fallback へ | `tests/test_review_2026_11_11_services_routes.py` |
| SVC-2 | サービス/競合 | Medium | `services/stock/retry.py`, `session_manager.py` | 成功した request が、並行して記録された新しい 429 window を消去し得る | check と clear が別 critical section。clear API が「観測した window」を区別できない | `clear_rate_limit(expected_until=...)` を追加し、`yfinance_lock` 内で再確認して条件付き clear | 同上 |
| SVC-3 | AI/API | Medium | `services/ai_service.py` | Mistral tool loop が合成した `tool_call_id` を tool result にだけ使い、assistant message は元の id のまま。次 turn が 400 | assistant turn と tool result を別 object から構築していた | assistant `tool_calls` を一度正規化し、両方で同じ id/name/args を使用 | 同上 |
| SVC-4 | AI/非同期 | Medium | `services/ai_service.py` | agents stream の worker が `asyncio.CancelledError` 等の `BaseException` で終了すると、consumer が空の `done` と circuit success を返す | error channel が `Exception` のみで、sentinel が正常終了と区別できない | worker で `BaseException` を捕捉して error item を enqueue。`KeyboardInterrupt`/`SystemExit` は再送出 | `tests/test_ai_service_agent_stream_backpressure.py` |
| SVC-5 | リアルタイム | Low/Medium | `services/realtime/engine.py` | mode-2 の full snapshot 間隔が最大 120s のとき、active client が 120s TTL で purge され更新が止まる | liveness の更新元が snapshot のみで、purge TTL が設定可能な最大間隔と一致していた | purge TTL を `max(120, snapshot_interval + 60)` に | `tests/test_review_2026_11_11_services_routes.py` |
| SVC-6 | AI/Embeddings | Low | `services/openai_service.py` | `index: true` を `int` として受理し、embedding を誤った text に割り当てる | `bool` が `int` の subclass | bool を明示的に invalid とし、位置 fallback | 同上 |
| SVC-7 | AI/Embeddings | Low | `services/embeddings_service.py` | Mistral embeddings を response の `index` 無視で位置割当し、cache を誤った text hash に汚染 | provider が順序を保証しない schema なのに positional mapping | `index` が有効ならそれを優先し、無効時のみ位置 fallback | 同上 |
| SVC-8 | AI/競合 | Low | `services/ai_service.py` | abort 時の pacing rollback が、別 thread の新しい reservation を消去し得る | rollback が自分の reservation を識別できなかった | thread-local に reservation timestamp を記録し、条件付き rollback | 同上 |
| API-1 | API/回路 | High | `routes/stocks/quotes.py` | HALF_OPEN probe が duplicate inflight（非 probe）で永久に `probing=True` のまま残り、history が `fetching` 固定になる | circuit key は symbol 単位、inflight key は period/interval 単位。duplicate を probe と誤認 | duplicate inflight が非 probe なら claim を release | 同上 |
| API-2 | API/キャッシュ | Medium | `routes/api_analysis.py` | `force=true` の news refresh が非 forced SWR job に join し、明示更新が no-op になる | inflight key が force を区別しない | forced 時は `news_{strategy}_force` を使用 | 同上 |
| API-3 | API/入力 | Low | `routes/system/credentials.py` | 非文字列 `openai_base_url` で 500 | OpenAI branch に型チェックがなく、service の `.strip()` が例外 | 型チェックして 400 envelope | 同上 |
| API-4 | API/競合 | Low | `routes/stocks/ai_portfolio.py` | copy-to-my の rollback が、並行 portfolio update の in-place mutation を同一 object と誤認して削除 | 書き込み時の live object を rollback 比較に使用 | `copy.deepcopy` した snapshot と比較 | 同上 |
| API-5 | API/競合 | Low | `routes/stocks/views.py`, `common.py` | heatmap の stale timeout 後の旧 fetch が、新しい fetch の inflight marker を消す | marker に owner token がない | start timestamp を fetch token として渡し、所有者だけが clear | 同上 |
| FE-1 | UI/状態 | High | `static/js/index_main.js`, `state.js` | portfolio save 後の直接配列代入が O(1) index を更新せず、modal が古い holdings を再保存する | `stocks` setter/`updateStocks` の invalidation 契約を bypass | `StateManager.replaceStock` を追加し、index を invalidate | `tests/test_frontend_state_index_invalidation.py` |
| FE-2 | UI/リアルタイム | Medium | `static/js/realtime_client.js`, `state.js` | 後から追加された portfolio card が realtime delta の DOM cache に入らず、価格が更新されない | symbol 単位 cache に wrapper registry 変更の invalidation がない | wrapper registry version を導入し、変更時に再 query | 静的/Node harness で確認 |
| FE-3 | UI/競合 | Low | `static/js/api.js` | 30s 間隔の indices polling で遅い旧 response が新しい値を上書きする | single-flight/generation guard がない | generation counter で最新 response のみ publish | コードレビュー/ビルド |
| FE-4 | UI/耐障害性 | Low | `static/js/index_main.js` | `new Notification()` が throw すると `updateStockUI` 全体が中断し、card が更新されない | platform API の error boundary がない | constructor を try/catch | コードレビュー |
| FE-5 | UI/競合 | Low | `static/js/settings.js` | 異なる symbol の連続 move が同じ pre-await order を読み、片方の move を上書き | read-modify-write が await を跨ぎ、lock が symbol 単位 | await 後に order を再読込 | コードレビュー |
| TEST-1 | テスト環境 | Low | `tests/test_support/environment.py` | harness の `AGENT=1` が Mistral Agents SDK の pydantic settings と衝突し、SDK テストが環境依存で失敗 | ambient な agent-harness 変数を test process が継承 | baseline test env で `AGENT` を除去 | 全テスト実行 |

## 2. 横断展開

- 「永続化前に唯一のコピーを消す」パターンを `config/atomic_write.py` と `utils/storage.py` の
  両方で確認し、file fsync + directory fsync + 条件付き削除に統一した。
- 「check-then-act を lock 外で行う」パターンを `load_user_stocks`、yfinance retry の
  rate-limit clear、heatmap inflight、copy-to-my rollback、indices polling で確認し、
  同じ lock/所有権/世代の内側で再確認する形に揃えた。
- 「provider が返す index / id を位置や元 object で扱う」パターンを Mistral tool loop、
  Mistral/OpenAI embeddings で確認し、provider の明示的な index/id を単一の正規化結果として
  両側で使う形にした。
- 「DOM cache が追加された wrapper を検知しない」パターンを realtime client で確認し、
  registry version による invalidation を追加した。

## 3. 除外した候補（誤検知・意図された仕様）

- `/api/stocks/add` の missing market が missing symbol を返す挙動: 既存テストで固定された
  後方互換 contract。
- `/api/stock-history` / `/api/stock-details` の HTTP 200 + body-level error: chart/detail
  client が body-level error を terminal として扱う既存 contract。
- `resolve_stocks_for_response` の `TypeError` 再試行: 旧 signature 互換のための意図的な shim。
- `sanitize_ai_portfolio` の weight 100% 正規化: L10 で意図された仕様。
- `_run_coro_sync` の timeout 時 worker 放棄: AI-05 の意図された trade-off。
- `chrome_extension` の `content_scripts` 不在: `activeTab` + `scripting` による
  least-privilege 設計。
- `MNS_ALLOW_REMOTE_API` の admin token 要件: 起動時 fail-closed の意図された仕様。

## 4. 最終検証

| チェック | コマンド | 結果 |
| :--- | :--- | :--- |
| Python tests | `pytest tests/ -q --tb=short -n auto --dist=loadfile --timeout=120 --timeout-method=thread` | 4,186 tests collected / failures 0 |
| Startup smoke | `MNS_SKIP_BOOTSTRAP=0 python tests/startup_smoke_runner.py` | exit 0 |
| Ruff | `ruff check . --line-length=100` | All checks passed |
| Flake8 | `flake8 . --count --select=E9,F63,F7,F82` | 0 |
| Mypy | `mypy` | Success: no issues found in 125 source files |
| Pyrefly | `pyrefly check` | 0 errors |
| Pylint (CI targets) | `pylint --errors-only --disable=import-error ...` | exit 0 |
| Bandit | `bandit --configfile pyproject.toml -r . --severity-level medium` | No issues identified |
| npm audit | `npm audit --audit-level=high` | 0 vulnerabilities |
| Frontend build | `npm run build` (tsc / compile / verify-generated / eslint / prettier) | exit 0 |
| Diff hygiene | `git diff --check` / `git status --short` | whitespace error 0 / debug・不要ファイル 0 |
