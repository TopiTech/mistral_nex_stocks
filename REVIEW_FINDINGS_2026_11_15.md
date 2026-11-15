# 2026-11-15 全ファイル横断レビュー — 課題一覧・根本原因修正・検証記録

本ドキュメントは `mistral_nex_stocks_complete_fixed_v3` の UI / バックエンド / DB・データ層 /
共通モジュール・設定 / API 境界 / Chrome 拡張 / native host / CI を全ファイル横断で再精査した
記録である。既存の `REVIEW_FINDINGS_2026_11_14*.md` で意図的仕様・後方互換として除外された
項目は再指摘せず、今回新たに確認できた課題だけを対象にした。

## 1. 調査方法

- 事前に全ディレクトリ・全ソースの行数、テスト構成、`pyproject.toml` / `pytest.ini` /
  `package.json` / `.github/workflows/ci.yml` を確認。
- ベースライン（変更前）で pytest 全件、ruff、flake8(E9/F)、mypy、pyrefly、bandit(medium+)、
  npm build、npm audit、pip-audit、startup smoke がすべて成功することを確認。
- バックエンド中核・routes/schemas・services・utils/config/native host・frontend・extension/CI の
  6 領域を並行して全ファイル精査し、候補はすべて該当コードを直接読んで再検証した。
- 修正は依存関係の根底（設定・型・共通基盤・API 境界）から論理単位ごとに
  「修正 → 回帰テスト → 静的解析 → ビルド」の順で実施。
- 型の弱体化（`Any` 拡大・不当な型アサーション）、エラー抑制コメント、テストの skip /
  アサーション緩和による回避は行っていない。既存テストのうち *誤った旧挙動を固定していた*
  ものだけを、修正後の正しい契約に合わせて更新した（§4）。

## 2. 修正した課題（横断展開分を含む）

### 2.1 バックエンド中核・設定・データ層

| ID | 重要度 | 対象 | 課題 / 根本原因 | 修正 | 回帰テスト |
| :--- | :--- | :--- | :--- | :--- | :--- |
| BE-01 | High | `config/io.py` | レガシー設定の移行失敗後も `DEFAULT_CONFIG` を runtime に書き、次回起動でレガシーが stale 扱いされて master key が孤立 | 移行未完了かつレガシーが存在する間は runtime を作らず、`save_config` も拒否 | `test_config_legacy_migration_secrets::test_failed_copy_keeps_legacy_file`、`test_save_config_refuses_defaults_before_legacy_migration` |
| BE-02 | High | `config/io.py` | 一時的な `OSError` を破損と誤判定し、defaults を negative-cache。後続 save で secrets を消失 | `OSError` を破損処理から分離し、その呼び出しだけ defaults を返す。save 時に保護キーを既存ファイルからマージ | `test_transient_config_read_error_is_not_corruption`、`test_save_config_preserves_protected_secrets_when_caller_has_defaults` |
| BE-03 | High | `config/master_key.py` | 不正な `MNS_MASTER_KEY` / 保存鍵をそのまま返し、`protect_data` が keyring へ黙ってダウングレード。既存 Fernet 暗号文が読めなくなる | env / 保存鍵を `Fernet()` で検証し、不正なら fail-closed | `test_invalid_env_master_key_fails_closed`、`test_invalid_stored_master_key_fails_closed` |
| BE-04 | Medium | `logging_config.py` | 既定ログ先がソースツリーで、読み取り専用環境では handler 作成例外で起動不能 | 既定を `APP_DATA_DIR` に変更し、ファイル handler 作成失敗時は stderr にフォールバック | `test_logging_falls_back_when_log_dir_is_a_file` |
| BE-05 | Medium | `credential_manager.py` / `utils/text_utils.py` | NUL 等の制御文字を含む API キーが config 保存後に `os.environ` で失敗し、keyring rollback と config が不整合 | API キー検証で制御文字を拒否し、保存前にも二重検証 | 既存 credential テスト + コードレビュー |
| CRED-01 | Medium | `credential_manager.py` | `clear_api_credentials` が keyring を先に削除し、その後の config 保存前にクラッシュすると config が削除済み secret を参照 | config を先にクリアし、その後 keyring を削除。失敗時は keyring/ephemeral/config を rollback | `tests/test_r1_r5_goal_fixes.py` 全件 |
| BE-06 | Medium | `services/search/tavily.py` | `CircuitState` は dict ではないため `isinstance(state, dict)` が常に False。HALF_OPEN の単一プローブ制御が無効 | `state.get("status")` で判定 | `test_tavily_half_open_state_is_detected` |
| BE-07 | Medium | `app_factory/boot.py` / `utils/threading.py` | fork 後に親の executor の dead thread 集合と idle semaphore を引き継ぎ、submit が永久にキューされる | `_adjust_thread_count` で dead thread を剪定し、生存 worker が無ければ idle semaphore をリセット | `test_daemon_executor_recovers_from_inherited_dead_workers` |
| BE-08 | Low | `app_factory/early.py` | 起動時クラッシュログが既定 umask で作成され、他ユーザーから読める可能性 | POSIX では dir 0o700 / file 0o600 で append | コードレビュー + 既存 early excepthook テスト |
| BE-09 | Low | `app_factory/boot.py` | 破棄可能な payload cache 移行の `RecursionError` が起動失敗になる | 内側 handler に `RecursionError` を追加 | コードレビュー |
| BE-10 | Low | `bg/sync/control.py` | `wrapped_loop` が `Exception` のみ捕捉し、BaseException でループが永久停止 | `BaseException` を捕捉し、KeyboardInterrupt/SystemExit のみ再送出 | コードレビュー |
| BE-11 | Medium | `gunicorn.conf.py` | `--preload` で fork されると executor/session の inherited state が壊れる | `on_starting` で `preload_app` を fail-fast 拒否 | `test_gunicorn_rejects_preload_app` |
| DATA-01 | Medium | `utils/storage.py` | 移行途中の 0 バイト/部分 target を「移行済み」と誤認し、レガシー移行と保存が永久に失敗 | 完全性を検証し、部分 target は削除して再移行。tmp + fsync + replace に変更 | `test_partial_user_stocks_target_is_retried`、`test_storage_migrate_legacy_user_stocks_retries_on_transient_permission_error` |
| DATA-02 | Medium | `utils/chat_history.py` | `init_db` の一過性失敗が再試行されず、スキーマ無しで固定 | 接続取得時に未初期化なら `init_db()` を再試行 | コードレビュー + 既存 chat history テスト |
| DATA-03 | Low | `utils/chat_history.py` | スキーマ v2 の `ALTER TABLE` 失敗をすべて握りつぶし、`user_version` だけ進める | `PRAGMA table_info` で列存在を確認し、必要な列だけ追加 | コードレビュー |
| DATA-04 | Low | `utils/chat_history.py` | 終了しなかった worker thread の SQLite 接続が `_active_conns` に残り fd リーク | thread-local に guard を置き、thread 終了時に close + discard | `test_chat_history_closes_worker_connection_on_thread_exit` |
| DATA-05 | Low | `utils/disk_cache.py` | 破損した深い JSON の `RecursionError` が get/has から漏れる | 破損扱いに追加 | コードレビュー |
| DATA-06 | Low | `utils/disk_cache.py` | process lock 待ちを in-process lock 保持中に行い、1 つの詰まりが全 caller を直列化 | process lock → in-process lock の順に変更 | コードレビュー |
| DATA-07 | Low | `utils/text_utils.py` | 巨大整数の `OverflowError` が検証エラーでなく 500 になる | `OverflowError` を `ValueError` に変換 | コードレビュー |
| DATA-08 | Low | `config/io.py` | `.bak` が fsync されず、電源断で rollback 点が壊れ得る | backup tmp を flush + fsync してから replace | コードレビュー |

### 2.2 API 境界・routes・schemas

| ID | 重要度 | 対象 | 課題 / 根本原因 | 修正 | 回帰テスト |
| :--- | :--- | :--- | :--- | :--- | :--- |
| API-01 | Medium | `routes/api_analysis.py` | `request_token` が payload に束縛されず、別 symbol/message で再利用すると前の AI 結果を返し、履歴にも追記 | request fingerprint を別 TTLCache に保持し、不一致なら cache を破棄して新規ジョブ | 既存 chat/analyze テスト + コードレビュー |
| API-02 | Medium | `routes/stocks/portfolio.py` / `views.py` | save 失敗 rollback が、同時に committed された reset/delete を復活させる | `user_stocks_rev` を記録し、rev が変わっていれば disk から再読込 | 既存 rollback テスト + コードレビュー |
| API-03 | Low | `routes/stocks/quotes.py` | `/api/stock-details` の fetching/失敗 200 に `Cache-Control` が無く、中間キャッシュされ得る | `no-cache, no-store, must-revalidate` を付与 | コードレビュー |
| API-04 | Low | `utils/text_utils.py` | Content-Length 超過 / chunked 超過の JSON body が 400 MALFORMED_INPUT になり、413 ハンドラを迂回 | 両経路で `RequestEntityTooLarge` を送出し Flask の 413 に委譲 | `test_content_length_over_json_limit_raises_413`、`test_json_parsers_reject_oversized_chunked_bodies` |
| API-05 | Low | `routes/api_analysis.py` | 空の LLM 応答が `""` のまま result cache に入り、再ポーリングで空 reply / 空履歴追記 | cache 保存時に fallback 文言へ正規化。abort 時は None のまま維持 | 既存 `test_api_chat_improved` |
| API-06 | Low | `routes/api_analysis.py` | analyze-v2 が provider の raw `details` をそのまま返し、prompt 等が漏れ得る | `_safe_provider_error_details` で allowlist 化 | コードレビュー |
| API-07 | Low | `routes/stocks/ai_portfolio.py` | キー未設定時に 401 にすると、意図された fallback portfolio 機能を壊す（誤検知） | 変更せず。fallback は仕様 | 既存 AI portfolio テスト |

### 2.3 services・AI・データ取得

| ID | 重要度 | 対象 | 課題 / 根本原因 | 修正 | 回帰テスト |
| :--- | :--- | :--- | :--- | :--- | :--- |
| SVC-01 | Medium | `services/ai_portfolio_service.py` | 401 桁級の `weight_pct` / `target_price` で `OverflowError` が 500 になる | 両 except に `OverflowError` を追加 | `test_ai_portfolio_sanitizer_drops_overflowing_numbers` |
| SVC-02 | Medium | `services/stock/retry.py` / `provider.py` | 1 回の 429 を retry 各回 + caller で最大 4 回記録し、cooldown が 120 秒に倍増 | decorator で 1 logical call 1 回だけ記録。caller 側の重複記録を削除 | `test_yfinance_retry_records_one_rate_limit_per_logical_call`、既存 yfinance テスト更新 |
| SVC-03 | Low | `services/ai_service.py` | `"403" in exc_str` が `1403 ms` / port `14030` に誤一致し、timeout を tier 制限と誤分類 | `\b403\b` の単語境界一致に変更 | `test_tier_restriction_does_not_match_incidental_403_substring` |
| SVC-04 | Low | `services/ai_service.py` | Agents SDK（agent_id なし）と Chat が同じ cache key を使い、モード切替で他モードの回答を返す | cache key に `execution_mode` を追加 | `test_mistral_cache_key_discriminates_execution_mode` |
| SVC-05 | Low | `services/ai_service.py` | 400 桁級の `start_price` / `end_price` で technical lines 全体が失われる | `OverflowError` を追加 | コードレビュー |
| SVC-06 | Low | `services/ai_service.py` | shutdown 中でも wait 0 の AI 呼び出しが provider request を発行し、semaphore を長時間保持 | `_wait_for_rate_limit_slot` の先頭で shutdown を確認 | `test_zero_wait_still_observes_shutdown` |
| SVC-07 | Low | `services/search/tavily.py` | 1 query の retry が total deadline を最大 2 倍超過し、news fanout worker を占有 | 残り deadline から per-attempt timeout を導出 | コードレビュー |
| SVC-08 | Low | `services/ai_service.py` | Agents モードの usage が常に 0 で、コスト表示が機能しない | SDK result の usage を読み取り | コードレビュー |
| SVC-09 | Low | `services/openai_service.py` | base URL の query string に secret があるとログへ出る | scheme/host/path のみログ | コードレビュー |

### 2.4 UI・フロントエンド

| ID | 重要度 | 対象 | 課題 / 根本原因 | 修正 | 回帰テスト |
| :--- | :--- | :--- | :--- | :--- | :--- |
| FE-01 | High | `static/js/index_main.js` | 一時的な `/api/stocks` 失敗で、健全な state/SSE があっても全カードを破棄 | state/card が存在する場合は破棄せず toast のみ。state から再描画 | `test_failed_refresh_keeps_healthy_board_and_arms_startup_watchdog` |
| FE-02 | Medium | `static/js/index_main.js` | `connectSSE` が先に skeleton を作るため、起動時 watchdog が arm されず skeleton が永久化 | skeleton 有無に関わらず watchdog を arm。成功/失敗で clear | 同上 |
| FE-03 | Medium | `static/js/ui.js` | fullscreen chart の error banner が非表示の inline panel にだけ出る | `chartErrorHosts` が開いている fullscreen host を優先 | `test_chart_error_host_prefers_open_fullscreen_modal` |
| FE-04 | Medium | `static/js/api.js` | SSE diff ごとに indices bar を全再描画し、layout thrash + live region spam | indices 値の signature が同じなら早期 return | `test_sse_skeleton_and_indices_guards_include_all_markets_and_change_gate` |
| FE-05 | Medium | `static/js/settings.js` | `/api/stocks` の retry 枯渇/失敗時に「準備中」のまま復帰不能 | 各 market に error + 再試行ボタンを描画 | `test_settings_stock_error_renders_retry_control` |
| FE-06 | Low | `static/js/index_main.js` | `renderStocks()` を引数なしで呼び、portfolio 保存後の即時再描画が no-op | `renderStocks(market, state.stocks[market])` | 静的契約テスト |
| FE-07 | Low | `templates/index.html` | mode 2 で wrapper 全体を `display:none` にし、SR mirror も accessibility tree から消える | `#indices-bar-sr` を wrapper 外へ移動 | 静的契約テスト + 既存 a11y テスト |
| FE-08 | Low | `static/js/api.js` | skeleton 条件が `us`/`jp` のみで、idx-only board を再接続時に消す | `idx` も空判定に含める | 静的契約テスト |

### 2.5 Chrome 拡張・native host・CI

| ID | 重要度 | 対象 | 課題 / 根本原因 | 修正 | 回帰テスト |
| :--- | :--- | :--- | :--- | :--- | :--- |
| EXT-01 | High | `chrome_extension/background.js` / `popup.js` | `fetchWithTimeout` が headers 到着で timer を止め、body 読み取りが永久にハング | body 読み取り完了まで timer を保持し、timeout を `TimeoutError` に変換 | `test_fetch_with_timeout_covers_body_consumption` |
| EXT-02 | Medium | `chrome_extension/background.js` | 強制 token refresh が in-flight promise を消し、同時 4 呼び出しが native token budget を消費 | 強制 refresh も single-flight 化し、generation で古い結果の上書きを防止 | `test_concurrent_forced_token_refresh_is_single_flight` |
| EXT-03 | Medium | `native_host/start_backend.py` | 起動直後（port bind 済み・health 未 ready）の 2 回目 start が PID file を消して error | fresh PID + port in use は `warming_up` を返す | `test_fresh_pid_with_bound_port_is_warming_up_not_unhealthy` |
| EXT-04 | Medium | `native_host/start_backend.py` | PID 再利用で無関係プロセスを backend と誤認し、永久 warming_up | PID の cmdline が `app.py` か検証。無関係なら PID file を削除して spawn | `test_reused_pid_is_not_reported_as_warming_up` |
| EXT-05 | Medium | `package.json` / `.github/workflows/ci.yml` | compile 後に verify するため、stale な生成物を検出できない | verify-generated を compile より前に実行 | `npm run build` / CI |
| EXT-06 | Low | `chrome_extension/background.js` | 401/403 のうち token と無関係な 403 でも token refresh し、native budget を浪費 | 401 または extension-token 理由の 403 のみ refresh | `test_non_token_403_does_not_refresh_extension_token` |
| EXT-07 | Low | `chrome_extension/popup.js` | content script の `{ok:false}` を「ティッカー0件」と表示 | 明示的 error 表示に分離 | `test_content_script_error_is_not_reported_as_no_tickers` |
| EXT-08 | Low | `.github/workflows/ci.yml` | POSIX job の `-k` が POSIX 分岐を持つテストを拾わない | 対象テスト名のキーワードを追加 | CI 設定レビュー |
| EXT-09 | Low | `scripts/verify_ui_safety_contracts.mjs` | `//` コメント除去が文字列内の `//` を削り、sink を見逃す | 行頭コメントのみスキップ | `npm run verify-generated` |
| EXT-10 | Low | `scripts/verify_ui_safety_contracts.mjs` | onMessage 検査が `sender`/`runtime.id` の近接だけを見て、拒否していなくても通る | 比較 + 拒否 return のパターンを要求 | `npm run verify-generated` |
| EXT-11 | Low | `native_host/native_host.py` | POSIX ancestry が basename のみ信頼し、`/tmp/chrome` 等の偽装を通す | world-writable な実行ファイル/親ディレクトリを拒否 | `test_posix_executable_path_trust_rejects_world_writable_locations` |
| EXT-12 | Low | `native_host/native_host.py` | 深い JSON の `RecursionError` で native host が終了 | `SKIP_FRAME` に変換 | コードレビュー |
| EXT-13 | Low | `native_host/uninstall_host_windows.ps1` | 8.3 短縮パスの生存登録を認識できず、必要なファイルを削除 | 両パスを canonical 化して比較 | コードレビュー |
| EXT-14 | Medium | `native_host/start_backend.py` / `native_host/native_host.py` / `chrome_extension/background.js` | health JSON だけでは、ポートを奪った別プロセスに extension token を渡し得る | native host が PID と listening socket の所有者を照合し、`verified:false` なら extension は token を取得・送信しない。判定不能時は後方互換で unknown | `test_backend_verification_requires_pid_to_own_listening_port`、`test_unverified_backend_never_receives_extension_token` |

## 3. 誤検知・意図的仕様として除外した項目

| 項目 | 除外理由 |
| :--- | :--- |
| AI portfolio のキー未設定 401 化 | キー無しでも deterministic fallback portfolio を返す既存仕様。401 にすると機能退行 |
| `close_all()` 後の `get_session()` 再生成 | `close_all()` はテスト/再初期化の reset として使われ、shutdown 中の request は `shutdown_event` で拒否される。terminal 化は既存契約を壊す |
| 拡張機能の health fingerprint spoof（設計課題） | 同一ユーザーの local process は native host の秘密ファイルを読めるため、共有秘密を追加しても完全な防御にならない。token の権限は `add_ext` のみで、既存 threat model の範囲。設計課題として残す |
| `clear_api_credentials` の keyring 先行削除 | 既存テストが「失敗時に keyring を rollback する」契約を固定。クラッシュ時の journal 導入は別設計課題 |
| `_run_coro_sync` の abandoned worker | 既存レビューで timeout 時に待たない設計として受理済み |
| `YahooJPRealtimeScraper.stop(wait=True)` | 停止後の遅延コールバック防止を優先する既存設計 |
| `state.js` の FX 150 fallback | 既存テストが意図的仕様として固定 |
| AI portfolio cache の pop-on-read / error pop | 明示的再試行で新ジョブを開始できる既存契約 |
| 拡張機能 badge の 2.5 秒 setTimeout | MV3 の worker 停止は通常 30 秒 idle 後で、2.5 秒 timer は停止前に発火する。alarm 化は不要な複雑化 |
| CSP report の oversized を 413 にしない | CSP は fire-and-forget で、既存テストが 204 + 空レポートログを固定。JSON API は 413、CSP だけは 204 を維持 |
| keyring service 名のデータディレクトリ非依存 | 既存インストールの鍵を孤児化するため後方互換を優先 |
| `worker_validation` の任意 gunicorn 設定非対応 | 同梱 `gunicorn.conf.py` の `on_starting` が通常経路を守る |
| ニュース要約の短い英語行フィルタ | LLM 定型句除去の意図されたヒューリスティック |
| Nikkei225JP / heatmap の stale 表示 | 市場閉場中の最終値を返す意図。`stale` フラグ追加は UI 契約変更 |

## 4. 既存テストの契約更新（旧挙動の固定を解除）

以下のテストは「修正前の誤った挙動」を固定していたため、修正後の正しい契約に更新した。
アサーションの緩和や skip ではなく、期待値の修正である。

- `tests/test_start_backend.py`: fresh PID + occupied port は `warming_up`、stale PID は unhealthy error。
- `tests/test_config_legacy_migration_secrets.py`: 移行失敗時に runtime defaults を作らない。
- `tests/test_yfinance_perf_fixes.py`: 429 は retry 各回ではなく 1 logical call 1 回記録。
- `tests/test_unit2_services_fixes_2026_11_14.py`: caller 側の重複 mark を期待しない。
- `tests/test_detected_stock_add.py`: token refresh 条件を token 理由付き 403 に更新。
- `tests/test_review_2026_11_08_root_cause_fixes.py`: master key 検証に合わせて有効な Fernet key を使用。
- `tests/test_code_review_final_improvements.py`: O_EXCL 直書きではなく tmp + replace の retry を検証。

## 5. 最終検証結果

| チェック | 結果 |
| :--- | :--- |
| pytest 全件 | 全パス（exit 0、`-n auto --dist=loadfile --timeout=120`） |
| 新規回帰テスト | `test_review_2026_11_15_core_fixes.py` / `..._extension_native_fixes.py` / `..._frontend_fixes.py` 全パス |
| ruff | All checks passed |
| flake8 (E9,F63,F7,F82) | 0 |
| mypy | Success: no issues found in 125 source files |
| pyrefly | 0 errors |
| pylint (errors-only) | 0 |
| bandit (medium+) | No issues identified |
| npm run build (tsc + verify + eslint + prettier) | exit 0 |
| npm audit (high+) | 0 vulnerabilities |
| pip-audit --strict | No known vulnerabilities |
| startup smoke (`MNS_SKIP_BOOTSTRAP=0`) | 成功 |
| git diff --check | whitespace error 0 |
| coverage (CI 75% gate) | 82.41%（`--cov-fail-under=75` 成功、exit 0） |

## 6. 残存する既知の制約（欠陥ではなく設計課題）

- native host の backend 検証は PID と listening socket の所有者照合まで実装した。
  同一ユーザーが PID file 自体を改竄できる場合の完全防御には共有秘密 + challenge-response が必要で、
  その threat model は別設計課題として残る。
- `gunicorn --preload` は `on_starting` で fail-fast するよう強化した。PID 不一致時の
  executor 修復は防御的に残しているが、preload 以外の独自 fork 形態はサポート対象外。
