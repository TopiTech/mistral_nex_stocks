# 包括コードレビュー 2026-11-28（mistral_nex_stocks）

対象 HEAD: `aafcff4`（`test: add comprehensive regression tests for Unit 5 frontend fixes`）
レビュー日: 2026-11-28 / 環境: Windows 11 + Git Bash + `.venv` (Python 3.14.7) + Node

---

## 1. 目的とスコープ

UI・バックエンド・DB/データ層・共通モジュール・設定ファイル・API 境界（型整合性 / バリデーション / エラー封筒）を、
正常系だけでなくエッジケース・異常系・レースコンディション・リソースリーク・セキュリティ・パフォーマンスの観点で精査し、
根本原因ベースで修正したうえで回帰テストを追加し、静的解析・ビルド・全テストをクリーンにする。

対象規模（実測）:

| 区分 | 規模 |
| --- | --- |
| 非テスト Python | 63,358 行 |
| tests（Python） | 133,191 行 / 収集テスト 4,989 件 |
| static/js | 29,610 行 |
| その他 | templates, chrome_extension, native_host, config, scripts, bg（バックグラウンド） |

### 手法（抜粋調査ではなく全数走査）

1. **機械的全走査**: `ruff` / `mypy` / `pyrefly` / `bandit` / `pytest --cov` / `npm run build`（`tsc` + `verify_*` + eslint + prettier）。
2. **契約の突合**（スクリプトで機械照合）:
   - バックエンドの `@*.route` 全 46 本 vs フロントエンド（`static/js/**`, `chrome_extension/**`）の URL リテラル 37 件 → 未定義エンドポイント 0 件（残り 2 件はベース URL `/api` と動的パス）。
   - テンプレート内 `<script>` タグ全件の CSP nonce 付与、インラインイベントハンドラ（`onclick=` 等）0 件、インライン `style=` は `style-src 'unsafe-inline'` 許容範囲。
3. **全モジュール読解**: `app_state` / `market_state` / `execution_state` / `messaging` / `shutdown_manager` / `utils/*`（caching, storage, disk_cache, validators, chat_history, stock_payload, http_utils, formatting）/ `services/*`（stock/retry, stock/provider, preview_responses, realtime/engine）/ `routes/*`（stocks/*, system/*）/ `schemas/*` / `config/*` / `bg/*` / `app_factory/*` / `static/js/*`。
4. **反証による採否判定**: 候補所見は「実装を読む → 到達可能性を確認 → 既存テストが固定している契約か確認」の順で潰し、誤検知は根拠付きで除外（§4）。
5. **横断展開**: 採用した不具合クラス（ロック保持中の I/O、falsy 合体による型エラー握り潰し、スレッド跨ぎ close、ログ経由の制御文字注入、リダイレクト検証、サイズ上限なしのストリーム、世代（epoch）競合）を全リポジトリで grep し、同型の箇所を Unit に追加。

---

## 2. ベースライン / 最終検証の実測値

| チェック | コマンド | 結果 |
| --- | --- | --- |
| 全テスト | `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider -n auto --dist=loadfile` | **exit 0**（4,989 件収集、失敗 0 / skip 5。`filterwarnings=error::ResourceWarning` 有効） |
| カバレッジ | 上記 + `--cov` | **exit 0** / `TOTAL 31,442 stmts, 4,871 miss, 85%`（閾値 75%） |
| 型検査 (mypy) | `.venv/Scripts/python.exe -m mypy` | **exit 0** `Success: no issues found in 126 source files` |
| 型検査 (pyrefly) | `.venv/Scripts/python.exe -m pyrefly check` | **exit 0** `0 errors (83 warnings, 23 suppressed)` |
| Lint (ruff) | `.venv/Scripts/python.exe -m ruff check .` | **exit 0** `All checks passed!` |
| セキュリティ (bandit) | `.venv/Scripts/python.exe -m bandit -c pyproject.toml -r . -ll -q` | **exit 0**（MEDIUM 以上 0 件） |
| フロント/ビルド | `npm run build` | **exit 0**（`tsc` / `verify_*` / eslint / prettier すべて通過） |
| ワークツリー | `git status --porcelain` | **クリーン**（未追跡ファイル 0、デバッグ用一時ファイルは削除済み） |

型の弱体化（`any` 追加・不当な型アサーション・`# type: ignore` の新規追加）、エラー抑制コメント、テスト skip、アサーション緩和は一切行っていない（`static/js/**/*.ts` に `: any` は 0 件、非テスト Python の `type: ignore`/`noqa` は既存 32 ファイル分のみで増加なし）。

---

## 3. 修正した課題（Unit 1〜6、根本原因と回帰テスト）

すべて「対症療法ではなく原因の除去」で修正し、各 Unit にエッジケース/異常系の回帰テストを追加した。
実装コミット: `396f2dd`, `428e532`, `8b90580` / テストコミット: `aafcff4`（Unit 1〜6 のテストファイル `tests/test_comprehensive_fixes_2026_11_28_unit{1..6}.py`）。

### Unit 1: 共通基盤・設定・リソース

| # | 根本原因 | 修正 |
| --- | --- | --- |
| U1-1 | 壊れた JSON の救済処理が正規表現バックトラックで ReDoS 可能 | 線形走査の状態機械に置換（`utils/validators`） |
| U1-2 | 読み取り専用の app data ディレクトリで既定値解決が例外終了 | 一時既定値へ縮退（`config/io`） |
| U1-3 | トレンド取得ストリームが全バッファ前提でサイズ上限が効かない | 逐次読みで上限超過時に打ち切り |
| U1-4 | 閉じたリスナーのキューを強参照し続けメモリリーク + ID 再利用で誤配送（ABA） | `weakref.WeakSet` 管理に変更（`messaging`） |
| U1-5 | キーリング検査失敗時に資格情報を破壊的に更新 | 失敗時は変更しない（`credential_manager`） |
| U1-6 | セッションプール上限超過の close をロック保持中に実行 | ロック外へ退避（`session_manager`） |
| U1-7 | マスターキーロックがプロセス全体の `windll` を書き換え | 自前ロック取得に限定（`config/locks`） |
| U1-8 | 深いネストのログデータで再帰深度超過 | 深さ上限でサニタイズ（`utils/text_utils`） |
| U1-9 | 実行中にキャッシュディレクトリを削除されると以後書き込み不能 | 再作成して回復（`utils/disk_cache`） |

### Unit 2: バックグラウンド・データ整合性

| # | 根本原因 | 修正 |
| --- | --- | --- |
| U2-1 | 複数銘柄バッチで直前銘柄のフレームが混入（参照共有） | 銘柄ごとに独立フレームを返す |
| U2-2 | 指数データの末尾 NaN・小文字シンボル・連続失敗カウントが正規化されない | 正規化と失敗連鎖リセットを追加 |
| U2-3 | バックグラウンドワーカー同時起動が直列化されず二重起動 | 起動を直列化 |
| U2-4 | 前日終値が負・指数変化率がドリフトした場合に補間が破綻 | 負値/ドリフトを棄却（`bg/sse_interpolator`） |
| U2-5 | 再スケジュール失敗時に sync_pending が復元されず同期が止まる | 失敗時に復元（`bg/sync/control`） |
| U2-6 | シャットダウンとリーダー解放の競合で `is_leader` が True のまま残る | 解放を原子的に（`bg/leader_election`） |

### Unit 3: データ取得・フォールバック

| # | 根本原因 | 修正 |
| --- | --- | --- |
| U3-1 | yfinance レート制限中に raw キャッシュ/フォールバックが使えず空応答 | 制限中は生キャッシュとフォールバックを許容（`services/stock_service`） |
| U3-2 | yfinance 側の例外クラス欠如で import 時に落ちる | 例外 import を防御化（`services/stock/provider`） |
| U3-3 | Yahoo ブロック時にセッションマネージャへ二重マーク | 一度だけ記録（`services/fallback_provider`） |
| U3-4 | スクレイパーの `Retry-After` が呼び出し側へ伝播しない | 応答を転送（`services/realtime/scrapers`） |

### Unit 4: API 境界・セキュリティ・封筒

| # | 根本原因 | 修正 |
| --- | --- | --- |
| U4-1 | リバースプロキシ配下（リモートモード）の読み取り系で正当な Origin が拒否される | 信頼プロキシ経由の同一性判定に修正（`routes/system*`） |
| U4-2 | AI ポートフォリオ生成が失敗しても request_token を消費せず、再試行で枠が戻る | 失敗試行でも消費（`routes/stocks/ai_portfolio`） |
| U4-3 | 検索 API のキャッシュ済みエラー文字列が未サニタイズで返る | 上流文言サニタイザ経由（`routes/stocks/quotes`） |
| U4-4 | `/api/health` が資格情報状態を無制限に露出 | 公開許可リストでフィルタ（`routes/system/observe`） |
| U4-5 | リバランスの空テーマがスキーマで `tech` に正規化されない | `mode="before"` で正規化（`schemas/ai_portfolio`） |

### Unit 5: フロントエンド

| # | 根本原因 | 修正 |
| --- | --- | --- |
| U5-1 | bfcache 復帰（`pageshow`）で送信中フラグが残り再送信できない | 復帰時にリセット（`static/js/setup.js`） |
| U5-2 | モーダル再オープンで直前のフォーカス復元先が失われる | モーダル外の要素を保持して復元 |
| U5-3 | 可視性一時停止中に直接 SSE を開くと last_params が更新されない | 直接オープン経路でも更新（`static/js/api_client`） |
| U5-4 | 検索候補の閉鎖で `aria-*` が残り、unload で watchdog が停止しない | 状態とタイマーを確実にクリア |

### Unit 6: ネイティブホスト / 拡張

| # | 根本原因 | 修正 |
| --- | --- | --- |
| U6-1 | 起動テンプレートが UTF-8 コードページを設定せず日本語が文字化け | 静かにコードページ設定（`native_host/host_launcher.cmd.template`） |
| U6-2 | POSIX 側の一時ディレクトリが共有・台帳書き込みがシンボリックリンク追従 | スコープ限定 + `O_NOFOLLOW`（`native_host`） |
| U6-3 | 拒否済みトークンが起動時ハイドレーションで復活 | 拒否状態を優先（`chrome_extension/background.js`） |

---

## 4. 誤検知・意図された仕様として除外したもの（反証付き）

| 候補 | 判定 | 根拠 |
| --- | --- | --- |
| `with_yfinance_retry` が明示 `backoff_factor` を環境変数で上書きする | **仕様** | `tests/test_stock_provider.py::test_backoff_factor_tracks_runtime_env_changes` が実行時変更を契約として固定 |
| `routes/stocks/portfolio.py` の復元が `previous_value is not None` で失敗する | **到達不能** | `previous_value` は pop した値の `deepcopy`。値が `None` の場合は直前の型ガード分岐が無条件で復元するため、この条件が欠落を招く経路がない |
| `extract_json_payload` が dict/list を渡すとオブジェクトでなく文字列を返す | **契約** | `utils/validators.py:743-744` が JSON 文字列を返す設計（呼び出し側は `json.loads` 前提） |
| disk キャッシュの長 TTL 上書き（CACHE-01） | **実害なし** | `stock_disk_cache.set` に既定より長い TTL を渡す呼び出しが存在しない |
| mode 2 の定期フルスナップショットが `id:` 無し・イベントログ未記録 | **仕様** | 差分（delta）のみ replay 対象。スナップショットは毎回全量で自己完結し、再接続時の replay 契約を壊さない |
| `SSEEventLog` のブロードキャスト重複 | **なし** | `(seq, frame)` タプル + replay ログの dedup で二重送出なし（`tests/test_sse_replay.py`） |
| `chat_history.close_all()` のスレッド跨ぎ `ProgrammingError` | **設計済み** | `close_all()` はシャットダウン/テストリセット専用。`_conn_epoch` で stale 接続を破棄し再接続（`tests/test_review_2026_11_17_comprehensive.py::test_bug09_...`） |
| `app_factory/hooks.py` の inconsistent-return | **正常** | Flask `before_request` は `None` 返却が「続行」の意味 |
| 未使用引数 `market`（`bg/sse_interpolator`）等の nit | **変更せず** | 挙動に影響せず、依頼範囲外のリファクタリングになるため |

---

## 5. 横断検査の結果（ゼロ件を含む）

- `innerHTML` の直接使用: **0 件**（`textContent`/`createElement` 経由。XSS 面は閉じている）。
- テンプレートのインライン `<script>`: 全件 `nonce="{{ csp_nonce }}"` 付与。インラインイベントハンドラ: **0 件**。
- FE→BE の URL 契約: 未定義エンドポイント **0 件**。
- デバッグ用 `print(`: 非テストソースにはエントリポイント（`wsgi.py`, `native_host/start_backend.py`）の意図的な出力のみ。`.agents/`（エージェント用ツール、CI 対象外）は JSON プロトコル出力。
- TODO / FIXME / HACK: 実装コードに **0 件**（`utils/tradingview_mapper.py` の "XXX" ヒットは `TSE:XXXX` というプレースホルダ表記）。
- 型: `mypy` 126 ファイル / `pyrefly` 0 errors。`any` の新規導入なし。
- 例外分類: `except Exception` の新規追加なし（既存の境界handlerのみ）。

---

## 6. 残存リスク・非コード所見（未修正と理由）

1. **コミット `8b90580` のメッセージ本文に無関係な文字列が混入**（本文中に `乐彩finalburugburu` / `kontaktannonser` / `qq的天天中彩票` / `enziswa`、および本文の重複）。コードには該当文字列は存在しない（`git grep` 0 件）。履歴書き換えは破壊的かつ依頼範囲外のため**報告のみ**。今後コミットメッセージを生成する工程での混入防止が必要。
2. `.agent/`（未追跡のエージェント用ツール）に flake8 F841 が 1 件。プロジェクトの CI/静的解析対象外であり、追跡ファイルではないため変更しない。
3. `pylint` を有効化すると `fcntl` の import-error が出るが、POSIX 専用分岐を Windows で静的解析しているだけ（実行時は `os.name` で分岐済み）。
4. カバレッジ下位ファイル（`services/preview_responses.py` 61%、`utils/storage.py` 72%、`utils/disk_cache.py` 76%、`services/stock/retry.py` 76%、`services/stock/provider.py` 78%、`utils/http_utils.py` 79%）は、いずれも分岐の多い防御コード。全体 85% で閾値 75% を満たすため追加対応はしない。

---

## 7. 未確認事項（正直な制約）

- ブラウザでの実操作確認（クリック・目視・スクリーンショット）は未実施。Unit 5 は jsdom ベースの回帰テストで検証した。
- 実際の yfinance / 各スクレイパーへの外部通信は未実施（すべてモック）。ネットワーク起因の挙動は未検証。
- Windows の `msvcrt` ファイルロックによる**実プロセス間**競合は未検証（同一プロセス内の競合はテスト済み）。
- `.agent/` 配下は変更対象外のため、その品質は本レビューの対象外。
