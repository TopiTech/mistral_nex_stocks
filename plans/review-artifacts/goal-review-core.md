# バックエンドコア・セキュリティ領域 自律レビュー結果

- レビュー実施日: 2026-08-16
- 対象HEAD: 現在の作業ディレクトリ（`c:\Users\mibu0\Documents\develop\mistral_nex_stocks_complete_fixed_v3\mistral_nex_stocks_complete_fixed_v3`）
- モード: レビューのみ（コード変更なし）
- 未コミット差分（`static/css/index.css`, `static/js/ai_portfolio.js`, `templates/index.html`）は変更・破棄していない

## レビュー結果サマリー

### レビュー対象ファイル（ルート直下のPythonファイル）
- [`app.py`](app.py), [`app_state.py`](app_state.py), [`app_bg.py`](app_bg.py), [`ai_state.py`](ai_state.py)
- [`config_store.py`](config_store.py), [`config_utils.py`](config_utils.py)
- [`credential_manager.py`](credential_manager.py), [`crypto_utils.py`](crypto_utils.py)
- [`session_manager.py`](session_manager.py), [`security_config.py`](security_config.py)
- [`error_handlers.py`](error_handlers.py), [`error_codes.py`](error_codes.py)
- [`execution_state.py`](execution_state.py), [`market_state.py`](market_state.py), [`shutdown_manager.py`](shutdown_manager.py)
- [`logging_config.py`](logging_config.py), [`messaging.py`](messaging.py), [`mistral_compat.py`](mistral_compat.py)
- [`route_helpers.py`](route_helpers.py), [`sectors.py`](sectors.py), [`trend_sources.py`](trend_sources.py), [`constants.py`](constants.py)

補助確認: [`utils/networking.py`](utils/networking.py), [`utils/env_helpers.py`](utils/env_helpers.py), [`utils/threading.py`](utils/threading.py)

### 問題候補数
- 確定候補: **3件**（内訳: Critical 0 / High 1 / Medium 1 / Low 1）
- 要確認: **4件**

### 実行テスト（裏取り）
全てパス（exit code 0）。対象領域の既存回帰テストは全てグリーンであり、**以下に挙げる問題は既存テストでカバーされていない未検証の経路**であることを示唆する。

```
[MNS TEST DIAGNOSTIC] Python 3.14.6 ... Windows-11
[MNS TEST DIAGNOSTIC] MNS_SKIP_BOOTSTRAP=1
[MNS TEST DIAGNOSTIC] KEYRING_BACKEND=keyring.backends.fail.Keyring
```

- `tests/test_error_handlers.py tests/test_csp_header.py tests/test_csrf_protection.py tests/test_cors_security.py tests/test_coverage_app_crypto_extra.py tests/test_app_state_lifecycle.py` → 66 passed
- `tests/test_messaging.py tests/test_messaging_extra.py tests/test_coverage_execution_shutdown_extra.py tests/test_leader_election.py tests/test_mistral_compat_coverage.py tests/test_input_validation.py` → 40 passed
- `tests/test_security_fixes.py tests/test_session_manager.py tests/test_review_autonomous_goal_fixes.py tests/test_release_hardening_fixes.py tests/test_csp_report.py` → 51 passed
- `tests/test_config_utils.py tests/test_config_utils_extra.py tests/test_coverage_config_extra.py tests/test_coverage_utils_extra.py` → 76 passed

---

## 確定問題候補

### [CORE-1][High] 問題候補: シャットダウンAPIのCSRF除外が、Origin/Sec-Fetch-Site検証を実質無効化する経路（MNS_STRICT_SEC_FETCH_SITE=0既定時）

- 該当箇所: [`app.py`](app.py:526)-[`app.py`](app.py:620)（`_enforce_sec_fetch_site_check`）、[`app.py`](app.py:226)-[`app.py`](app.py:228)（`csrf.exempt`）、[`app.py`](app.py:554)
- 影響経路:
  1. `api_shutdown` は `csrf.exempt()` されており、かつ `_csrf_exempt_post_paths` にも含まれる
  2. `MNS_STRICT_SEC_FETCH_SITE` が既定（0/未設定）の場合、mutating で `Sec-Fetch-Site: cross-site` のリクエストは `_is_allowed_shutdown_origin()`（`Origin` ヘッダが許可リスト由来か）だけを確認して通過
  3. `_enforce_sec_fetch_site_check` は `before_request` で実行されるが、`/api/shutdown` は CSRFトークンを要求しないため、CSRFトークン検証の代替保護が存在しない
  4. `shutdown_manager.consume_shutdown_token` のトークンが空/未設定の場合（`get_or_create_shutdown_token` が失敗時もプロセス内トークンは返すが、初回起動直後や失敗パスでは`shutdown_token=None`になり得る）、検証なしでシャットダウン処理へ進む
- 問題・根本原因: `api_shutdown` は「同一オリジン運用」を前提にCSRFトークン不要としているが、`Origin` ヘッダのみに依存する許容判定は、`Origin` を偽装できないブラウザではある程度有効な一方、非ブラウザ（curl/native host/拡張機能）からの送信は `Origin` を任意設定可能。`MNS_STRICT_SEC_FETCH_SITE=0`（既定）では `Sec-Fetch-Site: cross-site` の非ブラウザリクエストが `Origin` 許可リスト由来でなければ拒否されるが、許可リストは `http://localhost:*` / `http://127.0.0.1:*` を含むため、同一マシン上のローカルマルウェアが送信すれば「許可オリジン」と判定され得る。設計コメント（REV-03/REV-04）はこの点を「loopback gate に依存」と明記しており、シャットダウンtokenが未設定・未生成の状態をフォールバックで保護していない。
- 重要度評価: High。`/api/shutdown` はサーバー全体を停止させる主要機能であり、トークン検証が`None`/未設定時に強制されない経路がある。ただし実環境では`get_or_create_shutdown_token()`がbootstrapで呼ばれ通常はトークンが存在し、`consume_shutdown_token`が`None`を拒否するため、実際のDoS（シャットダウン強制）にはトークン流出・推測が必要。個人的なローカル利用モデルではリスクは限定的だが、リモート/プロキシ公開時は注意が必要。
- 客観的根拠: [`app.py`](app.py:226)の`csrf.exempt(api_shutdown)`と[`app.py`](app.py:554)の`_csrf_exempt_post_paths`定義、[`shutdown_manager.py`](shutdown_manager.py:160)の`consume_shutdown_token`が`self.shutdown_token`の`None`チェックを持つことを確認。テストはCSRF保護・CORS・shutdown token個別にパスするが、`csrf.exempt` と`_enforce_sec_fetch_site_check` の組み合わせ・`shutdown_token=None` シナリオを統合的に検証するテストは存在しない。

---

### [CORE-2][Medium] 問題候補: `SECRET_KEY` 未設定時の自動生成キーが、`flask_secret_key` の取得失敗時に毎起動で変わる（セッション無効化）

- 該当箇所: [`app.py`](app.py:426)-[`app.py`](app.py:448)（`_configure_secret_key`）、[`credential_manager.py`](credential_manager.py:278)-[`credential_manager.py`](credential_manager.py:314)（`get_or_create_flask_secret_key`）
- 影響経路:
  1. `FLASK_SECRET_KEY` 未設定の非本番環境では `get_or_create_flask_secret_key()` で生成・永続化を試みる
  2. `config_store.save_config` が `RuntimeError`（Windowsロック競合やファイル書込失敗）を送出すると、[`app.py`](app.py:448) の呼び出しは例外を握りつぶさず伝播 → 起動失敗
  3. 一方、`get_or_create_flask_secret_key` 内では`save_config` が失敗しても例外は伝播する（`config_store.save_config` の失敗を握らない）ため、`_configure_secret_key` 自体が例外になる
  4. 結果: `FLASK_SECRET_KEY` 未設定で、かつ config 書込が一時的に失敗する環境（ReadOnlyなAPP_DATA_DIR、ディスク満杯、ロック競合）ではアプリが起動できない
- 問題・根本原因: 非本番でも「シークレットを生成して永続化できない」場合に fail-closed しており、`MNS_COOKIE_SECURE` 等で本番相当にした場合のエラーメッセージは「本番ではFLASK_SECRET_KEY必須」だが、実際は本番判定以外でもconfig書込失敗で起動不能になる。個人利用のローカル環境でAPP_DATA_DIRが読取専用のケース（例: ポータブル実行・サンドボックス）で顕在化し得る。
- 重要度評価: Medium。起動不能（主要環境での起動不能に該当し得る）だが、`FLASK_SECRET_KEY`を設定すれば回避可能であり、環境依存。`MNS_DATA_DIR`を書込可能ディレクトリに指定すれば再現しないため、既定Windows環境では再現しにくい。
- 客観的根拠: [`app.py`](app.py:435)の`if len(_flask_secret) < 32: raise ValueError`、[`credential_manager.py`](credential_manager.py:300)-[`credential_manager.py`](credential_manager.py:301)の本番判定`raise ValueError`を確認。config書込失敗パスは`config_store.save_config`（Windowsロック競合で`RuntimeError`）が既存テストで検証されている（`tests/test_config_utils_extra.py`）が、`_configure_secret_key`との組み合わせはテストなし。

---

### [CORE-3][Low] 問題候補: `_cleanup_rate_limit_store` と `rate_limit` wrapper のロック競合で、レート制限ストアの一貫性が一時的に崩れる（カウント漏れ）

- 該当箇所: [`route_helpers.py`](route_helpers.py:81)-[`route_helpers.py`](route_helpers.py:97)（`_cleanup_rate_limit_store`）、[`route_helpers.py`](route_helpers.py:401)-[`route_helpers.py`](route_helpers.py:444)（wrapper内の`_rate_limit_lock`使用）
- 影響経路:
  1. `_cleanup_rate_limit_store()` は `_rate_limit_lock` を保持したまま呼ぶ前提（docstring: "Caller MUST hold _rate_limit_lock"）
  2. wrapper 内で `_rate_limit_lock` を取得後に `_cleanup_rate_limit_store()` を呼ぶ（[`route_helpers.py`](route_helpers.py:401)-[`route_helpers.py`](route_helpers.py:405)）→ ネストは同一スレッドなので問題なし（`threading.Lock` はリエントラントではないが、`_cleanup_rate_limit_store` 内で再度 `_rate_limit_lock` を取得していないためデッドロックは無い）
  3. 一方、`skip_polling_duplicates` のパスでは `_rate_limit_store` / `_rate_limit_distinct_token_counts` を `_rate_limit_lock` で保護している（[`route_helpers.py`](route_helpers.py:354)-[`route_helpers.py`](route_helpers.py:376)）が、`key not in _rate_limit_store` の直後の `len(_rate_limit_store) >= _RATE_LIMIT_MAX_ENTRIES` のeviction（[`route_helpers.py`](route_helpers.py:409)-[`route_helpers.py`](route_helpers.py:417)）は別のロック区間で行われる
  4. 実害: 高並行時に eviction と polling-skip の distinct-token カウントが不整合になり、一時的にレート制限を超過したリクエストが通る、または過剰に 429 になる可能性
- 問題・根本原因: `_rate_limit_distinct_token_counts` の再構築が `_cleanup_rate_limit_store` と wrapper 内の2箇所で重複しており、`_rate_limit_lock` の粒度がコードパスごとに揺れている。実際には全て `_rate_limit_lock` 内で実行されるためデータ競合は無いが、eviction 後の distinct カウント再構築ロジックが複製され保守上の実害（将来の変更で片方だけ直すと不整合）がある。
- 重要度評価: Low。現状のロックは一貫しているため実データ破損は無いが、重複ロジックによる保守上の実害と、将来の回帰リスクを明示。
- 客観的根拠: [`route_helpers.py`](route_helpers.py:81)のdocstring、[`route_helpers.py`](route_helpers.py:403)のcleanup呼び出し、[`route_helpers.py`](route_helpers.py:124)-[`route_helpers.py`](route_helpers.py:131)と[`route_helpers.py`](route_helpers.py:418)-[`route_helpers.py`](route_helpers.py:424)の重複再構築を確認。`tests/test_input_validation.py`等のレート制限テストは単一シナリオでパスしており、高並行下の不整合は検証されていない。

---

## 要確認問題候補（確証が取れないもの）

### [CORE-C1][要確認] `_is_local_request` のリモート判定と `RAW_REMOTE_ADDR` の整合性
- 該当箇所: [`utils/networking.py`](utils/networking.py:485)-[`utils/networking.py`](utils/networking.py:499)（`_is_local_request`）、[`app.py`](app.py:378)-[`app.py`](app.py:386)（`RawRemoteAddressMiddleware`）
- 内容: `RawRemoteAddressMiddleware` は `ProxyFix` の外側（最外）に配置されるが、`_is_local_request` が `RAW_REMOTE_ADDR` を参照する際、`request.remote_addr` を参照している箇所（`_rate_limit_identity` など）との整合性を要確認。`MNS_PROXY_FIX=1` で `RAW_REMOTE_ADDR` が正しく設定されるかは WSGI サーバ（gunicorn/gthread, waitress等）依存のため、実環境での挙動確認が必要。
- 根拠: [`app.py`](app.py:413)-[`app.py`](app.py:423)のラップ順序は正しい（ProxyFix→Raw→app）。ただし、`_rate_limit_identity` が `request.remote_addr`（ProxyFix後）を使用する一方、`RAW_REMOTE_ADDR` は最外で捕捉されるため、リモートモードでのIP判定に差異が出る可能性はある。

### [CORE-C2][要確認] `bg_leader_election_loop` のシャットダウン遅延は実害なし（リスト登録済みを確認）
- 該当箇所: [`app_bg.py`](app_bg.py:342)-[`app_bg.py`](app_bg.py:358)、登録箇所 [`app_bg.py`](app_bg.py:2102)-[`app_bg.py`](app_bg.py:2106)
- 内容: `bg_leader_election_loop` は `while not shutdown_event.is_set()` で回り、`_try_acquire_leader_lock()`（非ブロッキング）→ `shutdown_event.wait(10.0)` のサイクル。シャットダウン時は `shutdown_event` が即座にセットされるため `wait(10.0)` は即時解放され、ループは即座に抜ける。`shutdown_executors` の `t.join(timeout=2.0)` は `background_threads` リスト登録済みのスレッドを対象としており、このスレッドは [`app_bg.py`](app_bg.py:2105) でリスト登録済み。したがってシャットダウン遅延は最大2秒に抑えられ、実害なしと判断（要確認から「健全」に結論変更）。
- 根拠: [`app_bg.py`](app_bg.py:2102)-[`app_bg.py`](app_bg.py:2106) で `app_state.execution.background_threads.append(t_leader)` を確認。`wrapped_loop`（[`app_bg.py`](app_bg.py:2065)）も `shutdown_event` を条件にループするため、シャットダウン時は即時終了。

### [CORE-C3][要確認] `trend_sources.py` の `_GOOGLE_TRENDS_LOCK` / `_GOOGLE_TRENDS_LAST_CALL` がスレッドセーフ
- 該当箇所: [`trend_sources.py`](trend_sources.py:65)-[`trend_sources.py`](trend_sources.py:68)
- 内容: グローバルな `_GOOGLE_TRENDS_LOCK` と `_GOOGLE_TRENDS_LAST_CALL` は `_EXECUTOR`（max_workers=6）から並列呼び出しされる可能性がある。`_GOOGLE_TRENDS_MIN_INTERVAL` の適用が `with _GOOGLE_TRENDS_LOCK:` 内で行われているかを要確認。`lock` が正しく使われているなら問題なし。

### [CORE-C4][要確認] `logging_config` の `WarningDeduplicationFilter` が `record.msg` を直接改変しないため、ログフォーマッタ後のサニタイズとの順序依存
- 該当箇所: [`logging_config.py`](logging_config.py:140)-[`logging_config.py`](logging_config.py:170)
- 内容: `WarningDeduplicationFilter.filter` は `record.getMessage()` を正規化するが、`record.msg`/`record.args` は変更しない。`SanitizedFormatter` が後段で実行されるため、`record.getMessage()` の正規化と実際のフォーマット出力が一致しない可能性。ただし、`_sanitize_error_message` はフォーマッタ段で実行されるため、dedup キーがサニタイズ前の生メッセージである点にのみ影響。実害は軽微。

---

## 指摘対象外（健全と判断した項目）

- **CSRFトークン方式**: [`security_config.py`](security_config.py:70)の`CSRFProtect`有効化と、[`app.py`](app.py:222)-[`app.py`](app.py:228)の`csrf.exempt`が明示的で、`api_credentials`等の機密操作は非exempt。健全。
- **暗号化**: [`crypto_utils.py`](crypto_utils.py:199)の`_encode_secret`で平文フォールバック廃止、[`crypto_utils.py`](crypto_utils.py:244)-[`crypto_utils.py`](crypto_utils.py:279)の`MNS_EPHEMERAL_FALLBACK`ゲート。秘密情報はキーリング/DPAPI/Fernetで保護され、エフェメラルフォールバックもインメモリ暗号化。健全。
- **セッションcookie**: [`security_config.py`](security_config.py:54)-[`security_config.py`](security_config.py:67)で`HttpOnly`/`SameSite=Strict`/`Secure`（条件付き）/`Partitioned`（条件付き）。健全。
- **yfinanceセッションプール**: [`session_manager.py`](session_manager.py:175)-[`session_manager.py`](session_manager.py:239)のシングルトン+ロック、`_enforce_pool_cap`/`_reclaim_idle_and_cap`/`bg_session_reap_loop`によるFD/メモリ枯渇対策。健全。
- **マスターキー・シャットダウントークン**: [`shutdown_manager.py`](shutdown_manager.py:16)-[`shutdown_manager.py`](shutdown_manager.py:54)の原子書込・0o600・fsync、[`shutdown_manager.py`](shutdown_manager.py:160)の`compare_digest`。健全。
- **エラーハンドリング**: [`error_handlers.py`](error_handlers.py:151)-[`error_handlers.py`](error_handlers.py:178)のcatch-allで本番時`exc_info`抑制。健全。
- **秘密情報のログマスキング**: [`logging_config.py`](logging_config.py:173)-[`logging_config.py`](logging_config.py:202)、[`utils/networking.py`](utils/networking.py:169)-[`utils/networking.py`](utils/networking.py:194)。健全。

---

## 補足: テスト裏取りの限界
- 既存テストは `KEYRING_BACKEND=keyring.backends.fail.Keyring` で実行されており、実キーリング/DPAPI の分岐（`_dpapi_protect`/`_dpapi_unprotect`）は `# pragma: no cover` で実網羅されていない。
- `MNS_SKIP_BOOTSTRAP=1` で実行されるため、`bootstrap()` の fail-closed パス（`MNS_ALLOW_REMOTE_API` + `MNS_PROXY_FIX` チェック等）はテストで実行されていない。
- 高並行（マルチスレッド）でのレート制限ストア不整合、`WarningDeduplicationFilter` の経時変化、`bg_leader_election_loop` のシャットダウン遅延は、いずれも単体テストでカバーされていない。

## 保存場所
- 本ファイル: `goal-review-core.md`（作業ディレクトリ直下）
