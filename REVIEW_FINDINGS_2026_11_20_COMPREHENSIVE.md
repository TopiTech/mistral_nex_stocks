# 2026-11-20 包括レビュー課題リスト（全ファイル精査・現物検証済み）

基準: HEAD `cfbe42b`。4並列 explore + 現物コード・テスト契約の直接読み合わせで検証。
過去ラウンドで意図的仕様・後方互換として除外された項目は再指摘しない。

## 修正対象（真の欠陥・改善）

| ID | 重要度 | 対象 | 課題 / 根本原因 | 本質的修正の方針 | 横断展開 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| N20-01 | Medium | `services/openai_service.py:get_openai_embeddings_batch` | OpenAI互換埋め込みの成功時に `usage` を一切読まず `record_openai_usage` を呼ばない。Mistral埋め込み(`embeddings_service.py:210`)とchat(`openai_service.py:996`)は計上するため、OpenAI埋め込み利用量だけ Panels/コストが0のまま。根本原因: 委譲経路の会計フック漏れ | 200応答の `data.get("usage")` がdictなら `record_openai_usage(usage, model=target_model)`。例外・非dictは無視（会計はbest-effort、既存のchat計上と同一契約） | 埋め込み経路3種を確認: mistral計上あり/openai chat計上あり/openai埋め込みのみ漏れ。他のrecord漏れなし |
| N20-02 | Low | `services/news_service.py:403-408` | Mistral系ニュース要約バンドルのLLMキャッシュキーがモデル名を含まない。OpenAI系は `openai_{model}` を含むが Mistral系は空文字で `news_bundle_llm_{hash}`。設定で Mistral モデルを切替えても同一コンテキストは旧モデル要約を TTL中返し続ける。根本原因: キーにモデル識別子が入っていない | `credential_manager.get_model_name()` の正規化後モデル名をキーに含める（例 `mistral_{model}_{hash}`）。モデル名取得失敗時は従来キーにフォールバック | 同種のモデル非依存キー: `_build_mistral_cache_key`/`_build_openai_cache_key` はモデル含み。埋め込みは namespace にモデル含み。ニュースのみ漏れ |
| N20-03 | Low | `services/fallback_provider.py:268-271` | AlphaVantage の `Note`/`Information` をサニタイズせず `logger.warning(...%s, msg)`。プロバイダ制御文字列（改行・端末エスケープ）がログに素通し。例外経路(`:321`)は apikey マスク済みだが情報経路は未処理。根本原因: 情報経路のサニタイズ漏れ | `_sanitize_error_message` 相当の短縮・制御文字除去を通してからログ（既存 `services/ai_service` の流儀に合わせる。値は捨てず200字程度に切詰め） | 同ファイルの他ログは apikey マスク済み。`openai_service/tavily/langsearch` はサニタイズ済み |
| N20-04 | Low | `static/js/utils.js:752` | `closeTopmostOnEscape` の第2分岐がテンプレに存在しない `#ai-portfolio-modal` のみ参照。実テンプレにある `#ai-portfolio-view` 内フォーカス時の Escape 処理がなく次分岐へフォールスルーする。根本原因: 実DOMと乖離した旧ID参照 | 旧 modal 分岐は後方互換のため保持（既存テスト `test_ui_003` が架空modalで順序固定）し、`#ai-portfolio-view` 内フォーカス時の blur フォールバックを追加 | 同関数の他分岐(fs/portfolio/alert)は全て実在ID |
| N20-05 | Low | `static/js/heatmap.js:1395-1415` | `resize`+3D rAF/renderer/teardown が `beforeunload` のみで `pagehide` 未対応。bfcache 復元でリスナー・タイマ・WebGL が残留。`screener.js:764-765` は両対応済み。根本原因: ライフサイクル非対称 | `pagehide` でも同 teardown を実行（`beforeunload` と共通化）。`pageshow` 再開は既存 load 経路に委ねる | screener は対応済み。orbit/index_main は別IDで追跡 |
| N20-06 | Low | `static/js/index_main.js:338-363` | `?q=BAD` で一致カード無しだと 40回×250ms の再試行が世代管理なし。遷移・再検索と競合し最大10秒後のDOM操作になる。根本原因: 中止手段のない再試行鎖 | 世代カウンタ/Abort 的ガードで新検索・遷移時に旧鎖を無効化 | 同種の無制限鎖は他になし。他ポーリングは generation ガード済み |
| N21-01 | Medium | `utils/storage.py:_migrate_legacy_user_stocks` | ターミナル状態判定（legacy 不在＋ターゲット完全）まで毎 load で migration lock 取得＋全量 JSON parse を行う。プロセス内メモ化なしで毎回ホットパス相当の I/O が走る | legacy 不在＋ターゲット完全を確認したらプロセス内メモ化（パスキー付き・失敗時は未設定で再試行） | 同一問題は他ストアに無し。disk_cache sweep 等と同種の「確定済み状態の再検査」パターン |
| N21-02 | Low | `config/atomic_write.py` / `config/io.py:87-96` / `shutdown_manager.py:99-124` | kill -9 等の crash で atomic write の tmp（`config.json.*.tmp`、`.*.tmp`）が残留し得る。in-process error path は unlink するが hard kill では残留、既存 sweep は user_stocks/disk_cache のみ | crash orphan 用の uuid tmp sweep（grace window 付き）を config load / shutdown token dir に追加 | 既存 sweep（user_stocks/disk_cache）と同種パターンの漏れを config/shutdown に展開 |

## 誤検知・除外（現物検証で否定）

| 候補 | 判断 |
| :--- | :--- |
| ai-portfolio rebalance の「毎pollで有料再生成」 | 誤検知。完了レスポンス直後に `result_cache.pop`+`delivered=True` を記録し、`_is_polling_token_inflight` は delivered トークンを False にして rate-limit skip を止める。2回目の poll は通常 quota 消費の新規扱いで 429 の対象。テスト `test_ai_portfolio_polling_error_cache` が drain 契約を固定 |
| chat SSE の `code/error_code` 非対称 | 除外。非streaming契約のみテスト固定で、SSE最小形は意図的。stocks SSE との差は既知の設計差で後方互換変更は過剰 |
| tech-lines/chart-image の 500 一律 | 除外。`generate_ai_technical_lines`/`analyze_chart_image_with_mistral` が返すのは固定文言 `{"error": str}` で status 情報を落としている。呼び側で 401/429 を復元できる材料が無く、誤分類より固定500+ログが正しい |
| prewarm の setdefault vs `{**cached,**new}` 順序 | 誤検知。prewarm箇所は意図的に旧値温存（marketCap/quoteType保護）でコメント明記。`get_fast_info` は新値優先で用途が異なる。両立する設計 |
| retry の OSError 再試行 | 誤検知。`RequestsHTTPError` を先に捕捉し `_extract_http_error_status` で 4xx は即raise。OSError 残渣は接続系のみで再試行が正しい |
| leader atomic の PID リサイクル | 除外。60秒 stale + unlink 競合の窓は既知の設計制約で、advisory が主経路。mtime+pid の両条件化は別設計課題として保留 |
| disk_cache `delete_prefix` のロック内全読込 | 除外。削除対象判定に stored key が必要で、R19 で掃除経路は有界化済み。単発管理操作で通常運用のホットパスではない |
| storage ロック内 I/O | 除外。保存側はスナップショット化済み。load のロック内 I/O は一貫性のための意図的保持 |
| corruption バックアップの fsync なし | 除外。破損バックアップ自体が best-effort の診断物で、原子性は tmp+replace で確保 |
| config fork 継承 | 除外。`register_at_fork` の対象は leader ロックで、config ロックは fd 都度開で継承しない設計 |
| sync mtimes の非同期競合 | 除外。フォロワー専用ウォーム経路で重複しても冪等な disk get + 同値追記のみ。prune は R17-13 で有界化済み |
| native-host ledger の fail-open | 除外。2秒タイムアウト後の素通しは意図的縮退でログ明記。同一ホストの IPC で大量並行は想定外 |
| tradingview nonce / orbit fetch / realtime reconnect / verify スクリプト / CI セレクタ | いずれも latent・低影響・テスト/設計で固定。今回は対象外 |
| `/api/stocks?force=1` の Origin なし | 除外。`_CROSS_SITE_COSTLY_GET_PATHS` + Sec-Fetch-Site cross-site ブロックが有効。loopback の force は意図的運用操作 |
| SSE ticket の POST 化 | 現状 POST で確認。候補の記述が古い |
| add_ext/shutdown の `UNSAFE_INPUT/403` 混在 | 除外。`error_code` の差はあるが HTTP 403 は一致し、テスト固定なし。認可自体は有効 |
| embeddings HALF_OPEN 早期 return | 誤検知。`is_circuit_open()` が OPEN/HALF_OPEN 両方で True を返す実装で、probe 要求は chat 経路が担う設計。埋め込み単独回復遅延は受容範囲とコメント明記 |
| embeddings OpenAI 委譲の会計漏れ（当初候補） | 委譲前に openai/provier 分岐があり、委譲先 `get_openai_embeddings_batch` 自体が未計上。N20-01 として委譲先で修正する（委譲元での二重計上はしない） |
| agents stream の abort 後リーク | 誤検知。`consumer_abort+drain+join(5s)` の順序化と finally close が実装済み。5秒超は警告ログ付きで意図的縮退 |

## 検証計画

- N20-01: OpenAI埋め込み200応答の usage 計上テスト（正常系・非dict・欠落の3系）+ 既存 embeddings テスト
- N20-02: モデル切替えでキーが変わる単体テスト + 既存 news テスト
- N20-03: 改行・エスケープ入り Note のログ出力テスト（既存の apikey マスクと両立）
- N20-04/05/06: `npm run build`（typecheck/verify-generated/compile/eslint/prettier）+ 該当 verify スクリプト
- N21-01: migration memo が terminal state 後に memoize される回帰テスト + 失敗時再試行契約
- N21-02: stale tmp sweep（config / shutdown）の回帰テスト + 既存 atomic write テスト
- 全体: pytest 全件、ruff、flake8(E9,F63,F7,F82)、pylint errors-only、mypy、pyrefly(default+win32)、bandit medium+、npm run build、git diff --check
