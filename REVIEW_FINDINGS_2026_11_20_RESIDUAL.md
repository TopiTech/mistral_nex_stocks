# 2026-11-20 残余課題レビュー（最終パス）

本ドキュメントは、既存の `REVIEW_FINDINGS_2026_11_11.md` / `_11_14*` / `_11_15.md` /
`_11_17_RESIDUAL.md` / `_11_18_RESIDUAL.md` と `.agent/issue_list_2026_11_17.md` で
記録済みの全ファイル精査を前提に、現在の worktree（`master` / `e27734c` 起点）を
再精査した結果をまとめたものである。過去に意図的仕様・後方互換として除外された項目は
再指摘していない。

## 1. 調査方法

- 対象: Python バックエンド（`app.py`、`app_factory/`、`routes/`、`services/`、`utils/`、
  `bg/`、`config/`、`native_host/`）、JavaScript / TypeScript（`static/js/`、
  `chrome_extension/`）、Jinja2 テンプレート、設定・CI、テスト。
- 直近機能コミット `309677b` の差分（約 1,700 行）を重点的に再検証した。
- 候補は該当コード・呼び出し元・既存テストの契約を直接読み、再現条件を確認してから採否を決めた。
- 同種のアンチパターンを `git grep` で横断検索し、修正対象へ組み込んだ。
- 修正は対症療法ではなく根本原因に対して行い、各修正に回帰テストを追加した。
- 型弱体化（`Any` 拡大・不当な型アサーション）、エラー抑制コメント、テスト skip、
  アサーション緩和は行っていない。

## 2. 修正した課題

| ID | 重要度 | 対象 | 課題 / 根本原因 | 修正 | 回帰テスト |
| :--- | :--- | :--- | :--- | :--- | :--- |
| R20-01 | Medium-low | `services/ai_service.py` | 技術ラインの型正規化が `{support, resistance, trend, breakout}` の完全一致のみを許可し、それ以外を一律 `support` に変換していた。プロンプトは「トレンドライン」を要求し、`TechnicalLineItem.type` は自由文字列のため、モデルが返す `trendline` がサポート線（緑）として描画され、フロントエンドが `trendline` を青のトレンド線として扱う契約と食い違っていた。未知の型を `support` と偽る点も根本原因 | 型の別名辞書 `_TECH_LINE_TYPE_ALIASES` を追加し、`trendline` / `trend_line` を正規形 `trend` へ変換。色の既定値は正規化後の型から決まるため `#3399ff` になる。未知の型は従来どおり `support` にフォールバック | `test_technical_lines_maps_trendline_alias_to_trend` |
| R20-02 | Low (defense-in-depth) | `utils/validators.py` | `sanitize_provider_error_info` は R17-09 のコメントで「non-sensitive scalar diagnostics のみ」と明言しながら、許可キーの値の型を検証せず、ネストした dict / list をそのままクライアント・キャッシュへ返していた。プロバイダがリクエスト本文やヘッダをエラーにエコーすると、API 境界を越えて構造データが露出する | `_sanitize_provider_error_scalar` を追加し、`str` / `int` / `float` / `bool` のみ許可。文字列は `_sanitize_error_message` で機密パターンを除去し 200 文字に制限、機密キーワードを含む値は破棄。`None`・dict・list はキーごと削除 | `test_provider_error_info_drops_non_scalar_diagnostics`, `test_provider_error_info_sanitizes_and_caps_string_diagnostics` |

## 3. 横断展開の確認結果

| 対象 | 確認内容 |
| :--- | :--- |
| 技術ライン型の全参照 | `_TECH_LINE_ALLOWED_TYPES` は `services/ai_service.py` 内の 1 箇所のみ。フロントエンド `static/js/ui.js` は `trendline` を既に許可し青で描画するため、バックエンドを正規形 `trend` に寄せても表示契約は変わらない。`trend_line` も同じ別名として吸収 |
| 色の既定値 | `_TECH_LINE_DEFAULT_COLORS` は正規形 4 種のみ。型正規化後に色を決める順序のため、`trendline` に緑のフォールバックが適用される経路は消えた |
| エラー情報の許可キー方式 | `_ALLOWED_*_KEYS` を用いるサニタイザは `sanitize_provider_error_info` のみ（`git grep`）。他の API エラー整形は `_sanitize_error_value` / `_sanitize_error_message` を通り、値の型を再帰的に処理している |
| 呼び出し元の互換性 | `routes/api_analysis.py` の 3 経路（analyze-v2 の provider error、`safe_parse_analysis_result`、再サニタイズ）を確認。`status_code` は `int()` + `try/except` で既に防御されており、非スカラー削除後も 502 フォールバックで動作する。`provider_error` は JSON シリアライズ可能なまま |
| 直近コミット `309677b` の他差分 | USDJPY レートの原子的更新、履歴キャッシュのバケット分離、埋め込み API の 4xx/429 中断、チャット失敗時の `None` キャッシュ、ウォームアップの copy-on-write、日経225JP パーサ共通化、PTS 0 非表示を再確認。いずれも呼び出し元・テスト契約と整合し、追加の欠陥はなし |
| API 境界の認可 | Flask の変異メソッド全ルートを AST 走査。未ガードは `/api/stocks/add_ext`（拡張トークン + Origin + loopback）、`/api/csp-report`、`/api/shutdown`（トークン）のみで、いずれも独自の認証を持つ |
| フロント / バックエンドのパス・DOM・型 | `/api` パス、テンプレートの DOM id、CSP nonce、履歴の `interval` / `period` 許可値、`eval` / `innerHTML` / インラインイベントハンドラの不使用を再確認。不一致なし |

## 4. 誤検知・過剰修正として除外した項目

| 項目 | 判断 |
| :--- | :--- |
| `_pts_worker_loop` の `updated_at` TypeError | 全プロデューサが `time.time()` を設定しており再現しない |
| `updateSingleIndexChip` の `.index-open` 等 | チップ内に該当要素はないが、ツールチップは別経路で生成される。表示欠陥なし |
| `api_stocks` の `fetching` 応答 | 非同期取得中の意図された契約。既存テストが固定 |
| `formatIndexNumber("--")` | 指数データに `"--"` は入らない |
| `StockDiskCache.has()` の全体読み込み | 単一キーの明示照会であり、ディレクトリ走査ではない。壊れたエントリを「無い」と判定する既存契約を保つため必要 |
| `/api/news` の token 再利用 | inflight キーは戦略単位で応答は市場全体のバンドル。別ペイロードの回答を返す構造ではない |
| health の loopback ゲート、`/api/chat` の `code` / `error_code` 非対称、同期 AI エンドポイント、config io/merge のロック順、Agents SDK の env キー共有、native host の同一ユーザー境界 | 既存テストが固定する意図的契約、または設計課題として過去ラウンドで除外済み |
| CSP の `style-src 'unsafe-inline'` | Chart.js / 第三者ライブラリの inline style に必要。script-src は nonce + strict-dynamic で制限済み |
| provider error の `retry_after` を秒数へ正規化すること | プロバイダごとに形式が異なり、クライアントは表示に使わない。契約変更は過剰 |

## 5. 最終検証結果

| チェック | 結果 |
| :--- | :--- |
| pytest 全件（`-n auto --dist=loadfile --timeout=180`） | exit 0、**4742 tests / 0 failures / 0 errors / 5 skipped**（JUnit XML で集計） |
| 新規回帰テスト 3 件 + `tests/test_ai_service.py` | 6 passed |
| ruff（`--line-length=100`） | All checks passed |
| mypy | Success: no issues found in 126 source files |
| pyrefly（既定 / `--python-platform win32`） | 0 errors / 0 errors |
| pylint（`--errors-only`） | exit 0 |
| bandit（`--severity-level medium`） | exit 0（Medium 以上なし） |
| `npm run build`（typecheck / verify-generated / compile / lint / prettier） | exit 0 |
| 作業ツリー | 変更 2 ファイル + 新規回帰テスト 1 ファイル + 本ドキュメントのみ。調査用ログ・XML・bandit レポートは削除済み |
