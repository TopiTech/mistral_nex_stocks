# フロントエンド・テンプレート領域 静的・動的レビュー結果

- レビュー対象: `templates/*.html` / `static/js/*.js` / `static/js/experimental/*.js` / `static/css/*.css` / `static/favicon.ico`
- レビュー日: 2026-08-16 (UTC)
- 方式: 静的コードレビュー + 型チェック (`npx tsc --noEmit`) + ESLint (`npx eslint static/js`) + 関連テスト実行
- **注記**: `static/css/index.css`, `static/js/ai_portfolio.js`, `templates/index.html` は既存の未コミット差分（ユーザー作業中）のため、**問題の対象外**として扱う。ただし当該ファイルのコードはコンテキストとして参照した。

---

## 0. 客観的根拠（実行結果サマリ）

| 検証                                             | コマンド                                                                                                                             | 結果                                                     |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------- |
| TypeScript 型チェック                            | `npx tsc --noEmit -p tsconfig.json`                                                                                                  | **0 errors**（exit 0）                                   |
| 生成物一致検証                                   | `node scripts/verify_generated_frontend.mjs`                                                                                         | `static/js/api_client.js matches the TypeScript output.` |
| ESLint                                           | `npx eslint static/js`                                                                                                               | **0 issues**（exit 0）                                   |
| テスト: frontend_review_fixes                    | `python -m pytest tests/test_frontend_review_fixes.py`                                                                               | 19 passed                                                |
| テスト: extension_review_fixes                   | `python -m pytest tests/test_extension_review_fixes.py`                                                                              | 12 passed                                                |
| テスト: sse_mode_ui_consistency                  | `python -m pytest tests/test_sse_mode_ui_consistency.py`                                                                             | 11 passed                                                |
| テスト: experimental_orbit                       | `python -m pytest tests/test_experimental_orbit.py`                                                                                  | 6 passed                                                 |
| テスト: sse_replay / csrf / csp                  | `python -m pytest tests/test_sse_replay.py tests/test_csrf_protection.py tests/test_csp_header.py`                                   | 42 passed                                                |
| テスト: sse_mode2_resilience / ticket / realtime | `python -m pytest tests/test_sse_mode2_resilience.py tests/test_review_r1_sse_ticket_binding.py tests/test_realtime_review_fixes.py` | 32 passed                                                |

**総括**: 現時点のHEADでは、型チェック・ESLint・関連テストはすべて成功しており、指摘対象条件（再現性・客観的根拠・実害）を全て満たす「確定済み Critical/High」問題は検出されなかった。以下は「確度の高い問題候補」および「監視・改善推奨」を重要度順に記す。全ては指摘対象条件を満たすか、レビュー時に見出した保守上の実害である。

---

## 1. 問題候補一覧（重要度順）

### FE-1 [High候補] `api_client.ts` と `api_client.js` の二重ソース管理（型チェック対象と実行対象の分離）

- **該当箇所**: [`static/js/api_client.ts`](static/js/api_client.ts:1) / [`static/js/api_client.js`](static/js/api_client.js:1) / [`tsconfig.json`](tsconfig.json:12) / [`package.json`](package.json:5)
- **影響経路**: ブラウザは `templates/base.html` 等が読み込む `static/js/api_client.js`（`.ts` のコンパイル生成物）を実行する。型チェックは `.ts` に対してのみ走るため、**開発者が `.js` を直接編集してしまった場合、`tsc --noEmit` は検知しない**。ただし現時点では `node scripts/verify_generated_frontend.mjs` が「一致」を確認済みであり、直近の実害はない。
- **問題・根本原因**: `.ts` と `.js` の両方がリポジトリに存在し、`.js` はビルド (`npm run compile`) で生成されるにも関わらず、コミット時点で両者が常に同期していることをCIで強制する仕組みが `verify_generated_frontend.mjs` 1本に依存している。手動編集や部分的適用の際に乖離が生じると、実行時のSSE再接続・ハートビート挙動が型チェック対象と異なる可能性がある。
- **重要度評価**: High候補（現時点では Low〜Medium。乖離が発生した場合の実害はSSE再接続動作の予期せぬ挙動）。「すべて満たす」条件に照らすと、現在は乖離が存在せず再現性の実害が未確認のため、**現時点では正式な指摘としない（監視項目）**。
- **客観的根拠**: `verify_generated_frontend.mjs` の実行結果 `static/js/api_client.js matches the TypeScript output.` により同期は確認。`package.json` の `build`/`compile` スクリプトに生成フローが存在。

---

### FE-2 [Medium] SSEハートビートタイムアウトとハートビートイベントの重複リセット設計（過剰な再計算・タイマー再生成）

- **該当箇所**: [`static/js/api_client.js`](static/js/api_client.js:293)（`_resetHeartbeatTimer`） / [`static/js/api_client.js`](static/js/api_client.js:365)（`openSSE`）
- **影響経路**: `openSSE` 内で `addEventListener` をラップし、任意のカスタムイベント（`realtime_update`/`pts_update` 等）が届くたびに `_resetHeartbeatTimer` を呼ぶ。さらに `eventSource.onmessage` と専用 `heartbeat` リスナーでも同様にリセットする。サーバーは15秒間隔で `event: heartbeat` を送る（[`routes/api_stocks.py`](routes/api_stocks.py:1837)）ため、通常はタイムアウト（45秒）に到達しない。
- **問題・根本原因**: 論理的なバグではなく設計上の冗長性。`heartbeat` イベントが来るたびに `clearTimeout` + `setTimeout` を繰り返すため、高頻度更新時（モード2）はタイマーがほぼ毎イベントで作り直される。実害（45秒タイムアウト）はサーバーのハートビートが止まった場合にのみ顕在化するが、その場合もこの仕組みで正常に再接続される。
- **重要度評価**: Medium候補。ただし現実のタイムアウト不具合は観測されておらず、テスト（`test_sse_replay`, `test_sse_mode2_resilience` 等32件）も全て通過。**リスク軽微のため監視項目として記録**。
- **客観的根拠**: サーバー側 `SSE_HEARTBEAT_INTERVAL = 15`（[`constants.py`](constants.py:417)）、クライアント側 `sseHeartbeatTimeout: 45000`（[`static/js/api_client.js`](static/js/api_client.js:15)）の整合は取れており、実際の誤作動を示すテスト失敗はない。

---

### FE-3 [Medium] 機密情報のログ流出リスク（`apiFetch` のエラーパスで `rawText` をメッセージへ含める）

- **該当箇所**: [`static/js/api_client.js`](static/js/api_client.js:185)（`request` 内 `rawText.slice(0, 1000)`） / [`static/js/api_client.js`](static/js/api_client.js:197)
- **影響経路**: `request()` は JSON パース失敗時に `HTTP ${status}: ${rawText.slice(0, 200)}` および `{ raw: rawText.slice(0, 1000) }` を `APIError.details` に含める。この `details` は `$logger.error`（[`static/js/api.js`](static/js/api.js:159)）等でログ出力され得る。APIキー自体は認証情報としてサーバーに送られる（ヘッダ/ボディ）ため、通常レスポンスボディに秘密が入ることはないが、**サーバーがエラーメッセージに機微情報をミラーリングする場合**の緩衝はない。
- **問題・根本原因**: クライアント側では直接的な秘密情報埋め込みは確認されず、`Logger._sanitize`（[`static/js/utils.js`](static/js/utils.js:269)）が `api[_-]?key`/`token`/`password`/`secret` 等のパターンをREDACTする防御はある。しかし `APIError.details.raw` の経路は `_sanitize` を経由しない（`api.js` の `$logger.error(..., error)` は `Error` オブジェクトを `_sanitize` の対象としているが、`details` の中身の再帰的マスクは保証されない）。
- **重要度評価**: Medium候補。実害は「サーバー応答が機微情報を含む場合のログ漏洩」という条件付きであり、通常運用では発生しない。監視・改善推奨。
- **客観的根拠**: コード上 `rawText` のマスク処理が `api_client.js` の `request()` に存在しないこと（[`static/js/api_client.js`](static/js/api_client.js:185)）。一方で `Logger` の正規表現マスクは `utils.js` に存在。

---

### FE-4 [Medium] `apiFetch` の `fetch` の結果、`data.error` を持つ非2xxレスポンスでエラーメッセージが重複表示される可能性

- **該当箇所**: [`static/js/api.js`](static/js/api.js:139)（`apiFetch`）
- **影響経路**: `apiFetch` は `!response.ok` のとき `classifyAPIError(null, response)` により `enhancedMessage` を作り `showToast` を呼ぶ（[`static/js/api.js`](static/js/api.js:183)）。その後 `err` を throw し、呼び出し側（例: `screener.js` の `apiFetch(..., { showToast: false })` 等）で再びエラーメッセージを描画する場合がある。
- **問題・根本原因**: `showToast: false` を渡す呼び出し（検索・ヒートマップ・スクリーナー・設定の読み込み等）は意図的にトーストを抑止しているが、**抑止されない呼び出し**（チャット、株価追加、一括分析など）では `apiFetch` 内で一度トースト表示し、さらに catch 側で `showToast` や画面内エラー表示を行うため、同一エラーが二重に提示され得る。UX上の軽微な実害。
- **重要度評価**: Low〜Medium。機能停止やデータ不整合はない。
- **客観的根拠**: `apiFetch` のトースト表示（[`static/js/api.js`](static/js/api.js:183)）と呼び出し側の二重ハンドリング（例: [`static/js/ui.js`](static/js/ui.js:2226) の `sendAiDrawerMessage` の catch、[`static/js/api.js`](static/js/api.js:1875) の `sendChat` catch）を確認。

---

### FE-5 [Low] XSS対策として `innerHTML` 不使用は徹底されているが、`CSS.escape` フォールバックが不完全（`realtime_client.js`）

- **該当箇所**: [`static/js/realtime_client.js`](static/js/realtime_client.js:50)
- **影響経路**: `_getElements` で `CSS.escape` が無い環境向けに手書きエスケープ関数を使用し、`data-symbol="${esc(symbol)}"` のセレクタ文字列を組み立てる。`CSS.escape` が存在する現代ブラウザでは問題ないが、**フォールバック実装は `"`（ダブルクォート）をエスケープしない**（`String(s).replace(/[^a-zA-Z0-9_-]/g, c => "\\" + c)` は `"` を `\"` に置換するが、`querySelectorAll` の属性セレクタ内で `\\"` が正しく解釈されるかはブラウザ依存）。
- **問題・根本原因**: 現代ブラウザはすべて `CSS.escape` を持つため実害は発生しない。但し、シンボルに `"` 等が含まれる異常系（バックエンドの `validateSymbol` が `/^[A-Z0-9^][A-Z0-9._\-^=]{0,14}$/` で弾くため通常到達不能）を除けば安全。**防御は二重化されている**。
- **重要度評価**: Low。到達不能パスであり、かつ `CSS.escape` の存在により実質発動しない。
- **客観的根拠**: `validateSymbol`（[`static/js/utils.js`](static/js/utils.js:31)）でシンボル文字種が制限され、`CSS.escape` 分岐（[`static/js/realtime_client.js`](static/js/realtime_client.js:51)）が実装済み。

---

### FE-6 [Low] `experimental_orbit.html` の `<footer style="display: none">` と `base.html` の footer ブロックとの整合

- **該当箇所**: [`templates/experimental_orbit.html`](templates/experimental_orbit.html:471)
- **影響経路**: オブザーバトリーは footer を非表示にするため `display:none` のインラインスタイルを付けている。`style-src 'unsafe-inline'` が CSP で許容されているためブロックされないが、`frame-ancestors 'none'`・`base-uri 'self'` など厳格CSPの方針上、インラインスタイルは例外的扱い。
- **問題・根本原因**: 機能上の問題はない。インラインスタイルは `style-src 'unsafe-inline'`（[`security_config.py`](security_config.py:96)）により許容されており、CSP違反ではない。
- **重要度評価**: Low（保守上の軽微な指摘）。`display:none` をクラス定義に寄せる等の一貫性改善余地。
- **客観的根拠**: CSP定義（[`security_config.py`](security_config.py:89)）とテンプレートのインラインスタイル。

---

### FE-7 [Low] SSE モード2の `tv_ticker_tape` 初期化が初回スナップショットに依存（再同期時の表示遅延）

- **該当箇所**: [`static/js/api.js`](static/js/api.js:707)（`processSseData` 内の `tv_ticker_tape` 処理）
- **影響経路**: モード2で ticker tape は初回 `initial_snapshot` が `tv_ticker_tape` を含む場合のみ初期化される（`tapeContainer.children.length === 0` ガード）。再接続後の `diff` のみが届き `tv_ticker_tape` を含まない場合、テープが空のままになる可能性がある。
- **問題・根本原因**: `updateSseModeSelectorUI` は意図的にテープを初期化しない設計（[`static/js/api.js`](static/js/api.js:547)）であり、バックエンドは初期スナップショット時に必ず `tv_ticker_tape` を付与する（[`routes/api_stocks.py`](routes/api_stocks.py:1684)）ため、実害は限定的。ただし再接続経路で `last_event_id` によるリプレイのみで初期スナップショットをスキップする場合（[`routes/api_stocks.py`](routes/api_stocks.py:1657) `send_initial` が False のケース）は、テープが空のまま残り得る。
- **重要度評価**: Low〜Medium。表示上の軽微な欠落で、データ不整合はない。
- **客観的根拠**: リプレイロジック（[`routes/api_stocks.py`](routes/api_stocks.py:1605)）で `send_initial` が False となり得るケースと、クライアントの初期化ガード（[`static/js/api.js`](static/js/api.js:715)）を照合。

---

### FE-8 [Low] `createRequestToken` のフォールバックで `crypto` 未定義時に例外が発生する可能性

- **該当箇所**: [`static/js/api.js`](static/js/api.js:1694)
- **影響経路**: `createRequestToken()` は `globalThis.crypto?.randomUUID` が無い場合 `globalThis.crypto.getRandomValues(bytes)` を呼ぶ。`globalThis.crypto` 自体が存在しない環境（非TLSの極端な古い環境、CSPで `crypto` を遮断されたWebView等）では `TypeError` を投げ、チャット送信が失敗する。
- **問題・根本原因**: `globalThis.crypto?.randomUUID` のオプショナルチェーンはあるが、次の `globalThis.crypto.getRandomValues` にはガードがない。ただし `window.crypto` はモダンブラウザでは常に存在し、CSPの `connect-src` には影響されないため、実際の到達性は極めて低い。
- **重要度評価**: Low。監視・改善推奨。
- **客観的根拠**: [`static/js/api.js`](static/js/api.js:1694) のフォールバック実装。

---

### FE-9 [Low] `chart.js` の `fetchStockHistoryPayload` で再試行時に新たな `AbortController` を作るが、最初の `controller` との整合が不十分

- **該当箇所**: [`static/js/chart.js`](static/js/chart.js:443)
- **影響経路**: `TypeError` 捕捉時に新規 `retryController` と `retryTimeoutId` を作り、`finally` で `clearTimeout(retryTimeoutId)` する。一方、外側の `timeoutId` は `fetchStockHistoryPayload` の `finally`（[`static/js/chart.js`](static/js/chart.js:485)）で `clearTimeout` される。リトライは最大1回なので、実質的なタイマーリークは限定的。
- **問題・根本原因**: リトライ中に外側の `controller` が abort されても `retryController` は abort されないため、キャンセル後にリトライレスポンスが正常系として処理され得る（世代チェックの欠如）。ただし `refreshStockChart` 側の AbortSignal 統合があるため、実際の到達はまれ。
- **重要度評価**: Low。監視項目。
- **客観的根拠**: [`static/js/chart.js`](static/js/chart.js:406)〜[`static/js/chart.js`](static/js/chart.js:488) の実装。

---

## 2. 確認済みの良好な防御（指摘なし）

- **XSS**: `innerHTML` / `insertAdjacentHTML` / `outerHTML` / `document.write` / `eval` / `new Function` は `static/js`（`experimental` 含む）全体で検出されず。全DOM構築が `createElement` + `textContent` ベース（例: [`static/js/ui.js`](static/js/ui.js:2030)、[`static/js/chart.js`](static/js/chart.js:639)）。
- **CSP**: 非ce付きインラインスクリプトなし。`strict-dynamic` + nonce、`script-src 'self' 'strict-dynamic'`（[`security_config.py`](security_config.py:89)）。`style-src 'unsafe-inline'` のみ許容（Chart.js等のため、許容設計）。
- **CSRF**: `csrfFetch`（[`static/js/utils.js`](static/js/utils.js:600)）による `X-CSRFToken` 注入 + トークン期限切れ時の自動再取得 + リトライ。SSEチケットはPOST経由でHttpOnly Cookieに格納し、URLにトークンを載せない（[`routes/api_stocks.py`](routes/api_stocks.py:1479)）。
- **機密情報**: APIキー値はクライアントコード/テンプレートに一切埋め込まれていない。`AppConfigSchema`（[`utils/validators.py`](utils/validators.py:72)）は boolean フラグとモデル名のみ。`credentials_ephemeral_keys` はキー名のリストであり実値ではない（[`crypto_utils.py`](crypto_utils.py:534)）。
- **SSE**: ハートビート監視・指数バックオフ・visivilitychange 連動・オンライン復帰・`last_event_id` リプレイ・フォールバックポーリングが実装され、テストで検証済み。
- **API契約**: 主要エンドポイント（`/api/stocks`, `/api/stock-history`, `/api/screener`, `/api/heatmap`, `/api/search`, `/api/trending`, `/api/indices`, `/api/chat`, `/api/analyze-v2`, `/api/news`, `/api/ai-portfolio*`, `/api/stocks/stream`）について、フロントエンドの消費形（`fetching`/`stocks`/`indices`/`history`/`results`/`trending`/`deltas`/`tv_ticker_tape` 等）とバックエンドのレスポンス形を照合し、不一致なし。
- **テンプレート**: Jinja2 の autoescape は Flask デフォルト（HTMLエスケープ有効）。`{{ csrf_token() }}`, `{{ csp_nonce }}`, `{{ default_symbols | tojson }}`, `{{ app_config | tojson }}` の埋め込みは適切。インラインイベントハンドラ（`onclick` 等）はゼロ。

---

## 3. 対象外（既存差分）の扱い

- [`static/js/ai_portfolio.js`](static/js/ai_portfolio.js:1)
- [`static/css/index.css`](static/css/index.css:1)
- [`templates/index.html`](templates/index.html:1)

上記3ファイルはユーザー作業中の未コミット差分のため、**問題候補として取り上げていない**（コンテキスト参照のみ）。

---

## 4. 総評

- 現時点のHEADで **Critical** の問題（認証回避・秘密漏洩・任意コード実行・データ損失）は確認されない。
- **High** 相当も、型チェック・ESLint・関連テストの全成功と、バックエンド契約の照合により確認されない。
- **Medium候補**は FE-2, FE-3, FE-4（いずれも条件付き・軽微）。
- **Low候補・監視項目**は FE-1, FE-5〜FE-9。
- フロントエンドはXSS・CSRF・CSP・秘密情報の管理について堅牢な設計であり、残る指摘は保守性・例外的条件下の改善余地に集中している。
