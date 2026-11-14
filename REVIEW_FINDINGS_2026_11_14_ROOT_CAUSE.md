# 2026-11-14 全ファイル横断レビュー — 根本原因修正と検証記録

本ドキュメントは、`mistral_nex_stocks_complete_fixed_v3` の全ソース（UI / バックエンド /
DB・データ層 / 共通モジュール・設定 / API 境界）を対象に実施したレビューの課題リスト、
根本原因評価、修正内容、除外判断、および最終検証結果をまとめたものである。

## 1. 調査方法

- 全 Python / JavaScript / TypeScript / テンプレート / 拡張 / native host を対象に、
  静的解析（ruff / flake8 / mypy / pyrefly / pylint / bandit）と横断 grep を併用。
- ルート・スキーマ・サービス・ユーティリティ・フロントエンドの各層を並行監査し、
  報告された候補はすべて該当コードを直接読んで再検証した（誤検知は除外）。
- 修正は依存関係の根底（設定・型・共通基盤）から順に、論理単位ごとに
  「修正 → 回帰テスト → 静的解析 → ビルド」を実施。
- 型の弱体化（`Any` 拡大・不当な型アサーション）、エラー抑制コメント、
  テストのスキップ・アサーション緩和は行っていない。

## 2. 修正した課題（横断展開分を含む）

| ID | 領域 | 重要度 | 対象 | 課題 / 根本原因 | 修正 | 回帰テスト |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| RC-01 | API/データ | High | `routes/stocks/portfolio.py`, `static/js/index_main.js` | `avg_fx_rate` の「未送信」と「明示 null」を区別できず、保有数の更新だけで保存済み為替基準が消えていた | `model_fields_set` で明示指定を判定。UI は常に `null` を送ってクリアを表現 | `test_portfolio_omitted_avg_fx_rate_preserves_stored_value` / `..._explicit_null_...` |
| RC-02 | API/AI | Medium | `routes/api_analysis.py` | チャットの provider 429/403/404/5xx が `RuntimeError` で status を失い、すべて 500 になっていた | `ProviderAPIError` に status を保持し、`_chat_error_response` で 401/403/404/429/400/503 に写像 | `test_chat_error_response_preserves_provider_429` |
| RC-03 | API/AI | Medium | `routes/api_analysis.py` | analyze-v2 の成功後に chat history 書き込みが失敗すると、成功した分析結果ごと 500 になっていた | 履歴書き込みを独立した try/except に分離し、補助処理の失敗を非致命化 | 既存 analyze-v2 テスト群 + コードレビュー |
| RC-04 | API/データ | Medium | `routes/stocks/quotes.py` | 履歴取得のあらゆるエラーを `SYMBOL_NOT_FOUND (1102)` として返し、一時的な rate limit でも銘柄が無効と誤認された | service の `error_code` を優先し、transient は `FETCH_FAILED` / `API_RATE_LIMITED` に分類。200 エラーに `no-store` を付与 | `test_stock_history_transient_error_is_not_symbol_not_found` |
| RC-05 | API/性能 | Medium | `routes/stocks/quotes.py` | `MNS_SKIP_BOOTSTRAP` 時に `/api/indices` が毎回 6 秒待機し、ワーカーを占有していた | `/api/stocks` と同じ skip-bootstrap ガードを追加し即時 `fetching: true` を返す | `test_indices_skip_bootstrap_does_not_wait_for_snapshot` |
| RC-06 | API/並行性 | Medium | `routes/stocks/views.py` | reset 保存失敗時のロールバックが、同時に成功した削除済み銘柄を復活させていた | `user_stocks_rev` を記録し、競合保存があればディスクから再読込。なければ従来通り復元 | `test_reset_rollback_reloads_when_concurrent_save_committed` |
| RC-07 | 設定/セキュリティ | High | `config/master_key.py` | `mns_master_key` が非 dict（旧 base64 文字列等）のとき「未生成」と誤認し、新しい鍵で上書きして全暗号文を孤児化していた | 真値の非 dict は fail-closed で `RuntimeError`。dict だが復号不能な既存分岐と同一契約に統一 | `test_master_key_non_dict_entry_fails_closed` |
| RC-08 | AI/サーキット | High | `services/ai_service.py` | Agents SDK ストリームの 4xx・tier 制限・イベント待ちタイムアウトまで回路失敗として計上し、設定ミスで全 AI 機能が 60 秒停止していた | 非 agent 経路と同じ分類を共有。既知 4xx/tier は probe 解放、transport/5xx/未知例外のみ回路失敗 | `test_agent_stream_circuit_classification_excludes_client_errors` |
| RC-09 | AI/外部連携 | High | `services/fallback_provider.py` | AlphaVantage の日次クォータ切れがグローバル scraper block を発動し、無関係な JP スクレイパー全部を 60〜600 秒停止させていた | AlphaVantage 専用のローカル cooldown を追加。`_mark_yahoo_block` は呼ばない | `test_alphavantage_quota_does_not_pause_global_scrapers` |
| RC-10 | リアルタイム/並行性 | Medium | `services/realtime/engine.py` | `register_symbols` が保存のたびに JP 銘柄を再トークン化し、進行中の PTS 取得結果を破棄したうえで毎周期 4-provider 取得を再投入していた | `register_symbol` と同じく登録済みなら skip（冪等化） | `test_register_symbols_is_idempotent` |
| RC-11 | AI/並行性 | Medium | `services/embeddings_service.py` | 埋め込みの abort 時、予約トークンなしで pacing slot を解放し、他スレッドの新しい予約を消していた | `_acquire_mistral_call_slot_with_reservation` + `_release_mistral_call_slot(reserved_ts)` に統一 | `test_embeddings_abort_rolls_back_with_reservation_token` |
| RC-12 | AI/ツール | Medium | `services/ai_tools.py` | 市場ニュース検索がクエリの一部トークン一致だけで無関係な見出しを返し、`matched_query: true` と偽っていた | 複数トークンは全トークン一致（`all`）に変更 | `test_market_news_requires_every_query_token` |
| RC-13 | データ/評価 | Medium | `utils/stock_payload.py` | 指数の quote info が空のとき idx を USD とみなし、^N225 の保有評価額を約 150 倍に膨らませていた | 既知の米国指数のみ USD、それ以外は JPY を既定にする `_default_index_currency` を追加 | `test_index_default_currency_uses_symbol_not_usd` |
| RC-14 | 共通/堅牢性 | Medium | `utils/validators.py` | LLM が 400 桁級の整数を返すと `float()` の `OverflowError` が解析全体を 500 にしていた | 有限 float 変換ヘルパーを追加し、`OverflowError` を「非数値」として扱う | `test_validators_reject_huge_integer_without_overflow` |
| RC-15 | 設定/セキュリティ | Low | `credential_manager.py` | `extension_api_token_created` が負値だと回転も backfill も発動せず、トークンが永久に再利用された | 非正値も 0 に正規化して backfill を発動 | `test_extension_token_negative_timestamp_self_heals` |
| RC-16 | データ/キャッシュ | Low | `utils/disk_cache.py` | 時計が巻き戻ると未来 mtime が age 0 となり、TTL 切れエントリが時計追いつきまで新鮮扱いされた | 未来 mtime が 300 秒超なら `math.inf`（期限切れ）を返す | `test_disk_cache_future_mtime_is_expired` |
| RC-17 | 外部連携/並行性 | Medium | `trend_sources.py` | 同一 `TrendReq` クライアントを複数スレッドが同時に `suggestions/build_payload/related_queries` し、ペイロード状態が混線していた | クライアント呼び出し区間を専用ロックで直列化 | 既存 trend テスト + コードレビュー |
| RC-18 | AI/堅牢性 | Low | `services/ai_service.py` | `X-RateLimit-Reset` のミリ秒値を秒として解釈し、1 回の応答で 5 分間の AI 停止を招いた | 1e11/1e14 で ms/µs を正規化し、待機を 3600 秒にクランプ | `test_rate_limit_reset_milliseconds_are_normalized` |
| RC-19 | 外部連携/データ | Medium | `services/fallback_provider.py` | live price marker が O=H=L=現在値 を捏造し、ゼロレンジの日足として描画されていた | docstring と M22 方針に合わせ O/H/L を `None` に | `test_live_price_marker_does_not_fake_ohlc` |
| RC-20 | セキュリティ/ログ | Low | `services/openai_service.py` | provider のエラーボディ全文を ERROR ログに出し、セルフホスト LLM がリクエスト内容を反射するとプロンプトが漏れていた | status のみを ERROR、本文はログに出さない | 既存 openai テスト + コードレビュー |
| RC-21 | データ/スクリーナー | Medium | `routes/stocks/views.py` | `market=all` で USD と JPY の market_cap を直接比較し、トヨタが Apple より上位に並んでいた | 混在市況のみ USD 建てを JPY に正規化した `market_cap_jpy` でフィルタ/ソート | `test_screener_mixed_market_cap_is_normalized_to_jpy` |
| RC-22 | 設定/並行性 | High | `config/io.py` | 初回 `DEFAULT_CONFIG` 書き込みが cross-process update lock 外で、他プロセスの資格情報/master key トランザクションを上書きし得た | 初回初期化を `config_update_lock` 内で再チェックして実行 | `test_config_first_run_initialization_uses_update_lock` |
| RC-23 | 監視/堅牢性 | Low | `routes/system/observe.py` | `/api/metrics` の disk cache stats が try 外で、一時的な OSError が 500 になっていた | cache ごとに try/except し、取得できた分だけ返す | 既存 metrics テスト + コードレビュー |
| RC-24 | データ/キャッシュ | Low | `route_helpers.py` | ユーザーが明示追加した銘柄でも negative symbol cache が残り、最大 600 秒データが出なかった | placeholder 作成時に該当銘柄の negative cache をクリア | 既存 add/portfolio テスト + コードレビュー |
| RC-25 | API/エラー | Low | `route_helpers.py` | 負の `avg_fx_rate` に「>= 0」を案内し、0 で再試行しても失敗するメッセージになっていた | `avg_fx_rate` は常に「正の数値」メッセージに統一 | 既存 schema contract テスト |
| FE-01 | UI/SSE | High | `static/js/api.js` | SSE の差分が文書全体のカード有無で full/diff を決め、他タブで追加した銘柄のカードが永遠に作られず、削除カードも残った | market ごとに state と DOM の stockKey 集合を比較し、乖離した market だけ `renderStocks` で再整合 | `test_frontend_fixes_are_wired` + 既存 UI テスト |
| FE-02 | UI/SSE | Medium | `static/js/api.js`, `index_main.js` | 非表示中に届いた SSE は state だけ更新し、復帰時に SSE が OPEN だと DOM を再描画しなかった | `hiddenUpdatesPending` を立て、visibility 復帰時に一度だけ再描画 | 同上 |
| FE-03 | UI/SSE | Medium | `static/js/realtime_client.js`, `api.js` | rAF で遅延した古い delta が、同期適用された新しい snapshot の後に走り価格を巻き戻した | pending delta を保持し、snapshot/diff 適用前に `flushPendingRealtimeDeltas()` で先に適用 | 同上 |
| FE-04 | UI/メモリ | Medium | `static/js/chart.js` | カード削除後に解決した履歴取得が detached canvas に Chart を作成し、Chart.js レジストリに残っていた | `wrapper.isConnected === false` なら `drawChart` / `refreshStockChart` を中断 | 既存 chart テスト + コードレビュー |
| FE-05 | UI/設定 | Medium | `static/js/settings.js` | Mistral 接続テストが選択中ではなく保存済み provider を検証していた | verify payload に `ai_provider: "mistral"` を明示 | `test_frontend_fixes_are_wired` |
| FE-06 | UI/設定 | Medium | `static/js/settings.js` | OpenAI 互換を Base URL / モデル未入力のまま保存でき、AI が全滅する設定になっていた | setup と同じ URL/モデル必須検証を追加 | 同上 |
| FE-07 | UI/クォータ | Medium | `static/js/experimental/ai-dive-controller.js` | AI Dive の 20 回ポーリングが `request_token` なしで `/api/news` の 20/分枠を食い潰していた | 1 操作 1 トークンを生成し、全ポーリングで送信 | 同上 |
| FE-08 | UI/起動 | Medium | `static/js/index_main.js` | 初回 `/api/stocks` と portfolio snapshot が終わるまで SSE を開かず、遅い GET で復旧できなかった | `connectSSE()` を先に呼び、GET は first paint として並行実行 | 既存 startup テスト |
| FE-09 | UI/整合性 | Low | `static/js/api.js`, `index_main.js` | SSE の indices 更新と in-flight の `/api/indices` ポーリングが競合し、古い値が後勝ちし得た | SSE 適用時に `_indicesFetchGeneration` を進め、復帰時にも bar を再描画 | 同上 |
| FE-10 | UI/ポーリング | Medium | `static/js/api.js` | Mode 0 の 60 秒ポーリングが前回リクエストを毎回 abort し、60 秒超の応答が永久に完了しなかった | `fallbackPollInFlight` ガード付きラッパーに変更 | 同上 |
| FE-11 | UI/チャット | Medium | `static/js/api.js` | `createRequestToken()` が try 外で throw すると、入力欄と送信ボタンが無効のまま残った | payload 構築を try 内へ移動 | 同上 |
| FE-12 | UI/表示 | Low | `static/js/ai_portfolio.js` | 損失時に金額の絶対値だけを表示し、「¥500,000 (-5.0%)」と利益に見える表示になっていた | 符号付き金額を表示 | 同上 |
| FE-13 | UI/メモリ | Low | `static/js/state.js` | `purgeStockKeyCaches` が `historyPrefetchCache` の `stockKey|period|interval` キーを削除できていなかった | プレフィックス走査で全期間・全 interval を削除 | 同上 |
| FE-14 | UI/チャート | Low | `static/js/chart.js` | animation 無効時に AI テクニカルラインが初回描画されなかった | ライン設定後に `chart.update("none")` | 同上 |
| FE-15 | UI/鮮度 | Low | `static/js/state.js`, `ui.js` | `stockDetailsCache` に TTL がなく、開きっぱなしのタブで PER/時価総額が更新されなかった | 15 分 TTL と `stockDetailsCacheAt` を追加 | 同上 |

## 3. 誤検知・意図された仕様として除外した項目

| 項目 | 除外理由 |
| :--- | :--- |
| keyring の service 名がデータディレクトリ非依存 | 既定値を変えると既存インストールの鍵を孤児化する。`MNS_KEYRING_SERVICE` で明示分離可能なため、後方互換を優先 |
| 保存済み API キーの `os.environ` エクスポート | 環境変数オーバーライドは既存の運用契約。削除すると既存デプロイのキー解決が壊れる |
| `worker_validation` が任意の gunicorn config ファイルを解析しない | 同梱 `gunicorn.conf.py` の `on_starting` フックが通常経路を守る。任意設定の完全解析は別設計課題 |
| AI portfolio の cache pop-on-read / error pop | 「明示的な再試行で新しいジョブを開始できる」意図された契約。既存テストが明示的に固定 |
| `state.js` の FX 150 フォールバック | 既存テストが「1.0 ではなく 150.0」を意図された仕様として固定。UI の null ガードは初回未取得用に維持 |
| ニュース要約の短い英語行フィルタ | LLM の定型句除去ヒューリスティック。緩めるとノイズが増えるため現状維持 |
| `YahooJPRealtimeScraper.stop()` の `wait=True` | 停止後の遅延コールバックを防ぐ正確性優先の設計。HTTP タイムアウトで上限あり |
| Nikkei225JP の stale index cache | 市場閉場中の最終値を返す意図。staleness フラグ追加は UI 契約変更になるため別課題 |
| scraper の `change=0.0` | engine が previous_close 既知時に再計算し、UI も欠損を 0 として扱う。データ契約の破壊的変更を避けた |
| copy-to-my の 0.01 株未満で全体 400 | 部分コピーで意図しないポートフォリオを作らない明示的失敗。製品判断 |
| ニュース失敗が analyze-v2 のエラーマッパーを使う | 共通 envelope の code/message は汎用で、ニュース専用文言は UI 側で補完可能。低影響 |
| AI portfolio キャッシュキーに model/provider を含めない | 同一テーマ・同一スコープの短時間再生成は稀。キー形状変更は save/delete の無効化契約に波及 |
| `_run_coro_sync` の放棄ワーカー | タイムアウト時に待たない設計。恒久対策は専用ループスレッド化で、別アーキテクチャ課題 |
| `config/atomic_write.py` の fd 二重 close | `os.fdopen` 失敗時のみの低確率経路。EBADF で無害化されるため優先度低 |
| `utils/storage.py` の legacy plaintext 残置 | target が既存の場合に削除すると、空の target を先に作ったケースで実データを失う。データ安全優先 |
| `routes/stocks/common.py` の US dot/dash alias | `BRK.B`/`BRK-B` の重複は UI 入力正規化で回避可能。低影響 |
| `config/io.py` の `load_config` 初回チェックの stat | 追加コストは 1 回の `exists()` で、ロック内再チェックにより正しさを担保 |

## 4. 最終検証結果

| チェック | 結果 |
| :--- | :--- |
| pytest 全件 | 4344 tests collected / 全パス（`-n auto --dist=loadfile --timeout=120`） |
| 新規回帰テスト | `tests/test_review_2026_11_14_root_cause_fixes.py` 21 件 全パス（xdist / 非 xdist 両方） |
| ruff | All checks passed |
| flake8 (E9,F63,F7,F82) | 0 |
| mypy (pinned 2.4.1) | Success: no issues found in 125 source files |
| pyrefly (pinned 1.3.1) | 0 errors |
| pylint (errors-only) | 0 |
| bandit (medium+) | No issues identified |
| npm run build (tsc + verify-generated + eslint + prettier) | exit 0 |
| npm audit (high+) | 0 vulnerabilities |
| startup smoke (`tests/startup_smoke_runner.py`, `MNS_SKIP_BOOTSTRAP=0`) | 成功 |
| git diff --check | whitespace error 0 |

## 5. 残存する既知の制約（欠陥ではなく設計課題）

- keyring のインストール間分離は `MNS_KEYRING_SERVICE` の明示設定に依存する。
- 任意の gunicorn 設定ファイルを使う場合の single-worker 検証は、同梱設定フックの範囲外。
- AI portfolio の in-flight ジョブと save/delete の競合は、cache の epoch 管理を導入する別改善課題。
- Nikkei225JP / heatmap の stale データに明示的な `stale` フラグを付けるには、UI 契約の追加設計が必要。
