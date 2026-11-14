# 2026-11-14 継続レビュー — 新規課題と修正結果

本ドキュメントは、アクティブゴール（全ファイル網羅レビュー＋根本原因修正サイクル）の
継続パスとして 2026-11-14 に実施した追加レビューの記録である。
前回までの成果は `REVIEW_FINDINGS_2026_11_11.md` に記録済みで、本パスでは
ベースライン（pytest 4313 tests / ruff / flake8 E9F / mypy 125 files / pyrefly /
bandit medium / npm build / npm audit high すべてクリーン、git clean）を確認したうえで、
さらに全ファイルを横断スキャンして新たに見つかった課題を洗い出し、修正した。

## 1. 新規課題一覧

| ID | 領域 | 重要度 | 対象 | 課題 | 根本原因 | 修正 | 回帰テスト |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| FE-2026-11-14-1 | UI/ストレージ | Medium | `static/js/api.js` `applyAnalysisResult` | 銘柄ごとの `ai_prev_${stockKey}` localStorage エントリが、銘柄削除（SSE `_removed` / 設定画面の delete / reset）後も残留し続ける。長時間セッションでのストレージ肥大＋削除済み銘柄のAI分析データが端末に残るプライバシー問題 | 「銘柄キー派生リソースのライフサイクル」が一元管理されておらず、削除導線ごとに個別対応していた | `state.js` に `purgeStockKeyCaches(stockKey)` を単一オーナーとして追加し、`api.js`（SSE `_removed`）、`ui.js`（renderStocks の reconciliation）、`settings.js`（delete/reset）から呼出。reset は `ai_prev_` プレフィックスを一括掃引 | `tests/test_review_2026_11_14_continuation.py`（Node vm harness + 静的検査） |
| FE-2026-11-14-2 | UI/メモリ | Low | `static/js/state.js` | `stockRealtimeUpdateAt` / `sparklineUpdateAt` / `sparklineSignatureMap` / `stockDetailsCache` / `historyPrefetchCache` の Map が銘柄削除時に delete されず無制限に肥大し得る | 上記と同じく per-stockKey キャッシュの削除導線が分散していた | `purgeStockKeyCaches` に全 Map の delete を集約 | 同上 |
| BE-2026-11-14-1 | バックエンド/SSE | Low | `bg/sse_interpolator.py` `_fluctuate_indices` | 指数の `prev_close == 0`（または非有限）のとき `percent` の else 分岐がなく、古い `percent` が残る / 将来 NaN が SSE フレーム（allow_nan=False）に混入し得る | ゼロ除算ガードが `if prev_close != 0:` のみで else が暗黙の「据え置き」だった | `math.isfinite` を条件に加え、else で price のみ更新して percent を触らない明示的分岐に | `tests/test_review_2026_11_14_continuation.py`（0 prev_close / 正常系 / FX ペア） |

## 2. 横断展開で確認したが問題なし（誤検知として除外）

- `priceEl.__updateTimer` 等の単発タイマー: beforeunload で未クリアでもページ破棄とともに消えるため実害なし（Low）。
- `SSEEventLog.replay_after` の巨大 `Last-Event-ID`: `last_id > self._seq` で早期 None 返却＋バッファ走査も maxlen bounded のため DoS ではない。
- `except Exception` の広さ: 234 箇所を目視確認し、いずれもフォールバック/ログ目的の意図的なもので `BaseException` 取りこぼしが致命的な箇所（chat_history の transaction）は既に `finally` で対処済み。
- `openai_base_url` の SSRF: `inspect_outbound_base_url` が scheme/userinfo/制御文字/ポート/解決IPピンニングまで検査済み。プライベートレンジを許可しているのはローカルLLM対応の意図された仕様（link-local のみ拒否）で妥当。
- レートリミット: 全 POST ルートに decorator 済み（api_analysis/ai_portfolio/portfolio/stream/views/credentials/csp_report/shutdown）。store は TTL クリーンアップ＋上限 eviction 済み。
- `type: ignore` は ctypes/WinDLL 等の Windows 専用スタブに限定され妥当。`Any` の不当拡大なし。
- CSRF exempt は `/api/shutdown` / `/api/stocks/add_ext` / `/api/csp-report` のみで、それぞれ loopback + Origin + 専用トークン検証付き。

## 3. 最終検証（修正後）

| チェック | 結果 |
| :--- | :--- |
| pytest 全件 | 4323 tests collected / 全パス（exit 0、`-n auto --dist=loadfile --timeout=120`） |
| ruff | All checks passed |
| flake8 (E9,F63,F7,F82) | 0 |
| mypy | Success: no issues found in 125 source files |
| pyrefly | 0 errors |
| bandit (medium+) | No issues identified |
| npm run build (tsc + verify-generated + eslint + prettier) | exit 0 |
| npm audit (high+) | 0 vulnerabilities |
| git diff --check | whitespace error 0 |
| 未追跡ファイル | `tests/test_review_2026_11_14_continuation.py` のみ（新規回帰テスト） |

## 4.備考

- `tests/test_code_review_goal_audit_2026_09_v2.py` のレガシー静的テストが
  settings.js の sort_* removeItem ブロックの逐語形状をピン留めしていたため、
  ai_prev_* スイープは別 try/catch ブロックに分離して後方互換を維持した。
