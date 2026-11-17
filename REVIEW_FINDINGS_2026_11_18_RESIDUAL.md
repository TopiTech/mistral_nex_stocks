# 2026-11-18 残余課題レビュー（最終パス）

本ドキュメントは、既存の `REVIEW_FINDINGS_2026_11_14*` / `REVIEW_FINDINGS_2026_11_15.md` /
`REVIEW_FINDINGS_2026_11_17_RESIDUAL.md` / `.agent/issue_list_2026_11_17.md` で記録済みの
全ファイル精査を前提に、現在の worktree に対して「現在も再現する欠陥」だけを再検証した
結果をまとめたものである。過去に意図的仕様・後方互換として除外された項目は再指摘していない。

## 1. 調査方法

- 追跡対象: Python 512 ファイル、JavaScript / TypeScript、Jinja2 テンプレート、
  Chrome/Edge 拡張、native host、CI 設定、テスト 386 ファイル。
- 既存の全レビュー文書と課題リストを読み、未対応・保留理由を確認したうえで、
  共通基盤（`utils/`）→ バックグラウンド競合（`bg/`）→ API 境界（`routes/`）の順に精査した。
- 候補は該当コード・呼び出し元・既存テストの契約を直接読み、再現条件を確認してから採否を決めた。
- 修正は対症療法ではなく根本原因に対して行い、各修正に回帰テストを追加した。
- 型弱体化（`Any` 拡大・不当な型アサーション）、エラー抑制コメント、テスト skip、
  アサーション緩和は行っていない。

## 2. 修正した課題

| ID | 重要度 | 対象 | 課題 / 根本原因 | 修正 | 回帰テスト |
| :--- | :--- | :--- | :--- | :--- | :--- |
| R19-01 | Medium | `utils/disk_cache.py` | 期限切れ掃除がエントリ全体を `read_text` したうえで先頭 64 KiB を JSON として解析していた。`stored_at` が封筒の末尾にあったため、64 KiB 超のエントリは必ず解析に失敗し、鮮度判定が mtime に落ちていた。`get()` / `has()` は `stored_at` を使うため、同じエントリで判定が食い違う（コピー・リストア後は特に顕著）。掃除はキャッシュロック保持中に無制限読み込みを行う | 封筒のフィールド順を `stored_at` 先頭に変更し、先頭 64 バイトだけを読む有界リーダー `_read_entry_timestamp_prefix` を追加。旧形式は 64 KiB 以下のみ全体解析し、それ以上は従来どおり mtime にフォールバックする `_path_entry_age_seconds` に集約。`remove_fields_recursive` の書き戻しでも `stored_at` を先頭へ正規化 | `test_disk_cache_writes_stored_at_first`, `test_cleanup_keeps_large_fresh_entry_with_old_mtime`, `test_cleanup_removes_entry_with_old_stored_at_despite_fresh_mtime`, `test_cleanup_still_parses_small_legacy_envelopes`, `test_remove_fields_rewrites_envelope_in_prefix_readable_order` |
| R19-02 | High | `bg/leader_election.py` | R17-01 の「unlink+recreate されたら降格」契約が未実装だった。`_verify_leader_ownership` は保持 fd への書き込み成功しか確認せず、パスの inode 同一性を比較していなかったため、ロックファイルを差し替えられても孤児 inode 上で leader を主張し続け、二重マスターになり得た。逆に atomic マーカー方式は本来 fd を持たないのに fd 必須と判定していたため、10 秒ごとに降格→即再昇格を繰り返し、半分の時間を follower として動作していた | 獲得方式（`advisory` / `atomic`）とロックパスを保持し、advisory は `os.fstat(fd)` と `os.stat(path)` の `(st_dev, st_ino)` を比較、atomic はマーカーファイル内の pid を検証する。所有権喪失時は fd を閉じ、モード・パス・leader フラグを一括で解除する | `test_atomic_marker_ownership_does_not_require_a_handle`, `test_atomic_marker_ownership_demotes_when_marker_replaced`, `test_advisory_ownership_keeps_leader_when_identity_matches`, `test_advisory_ownership_demotes_when_lock_path_was_replaced` |
| R19-03 | Medium | `routes/api_analysis.py` | `/api/chat` / `/api/analyze-v2` の実行中ジョブ結合が `request_token` のみで行われ、fingerprint 検証は完了済みキャッシュ経路にしかなかった。同じ token で内容を変えた再送が実行中ジョブに合流し、**別リクエストの回答**（と履歴追記）を受け取れた。結果キャッシュ経路の契約と不整合 | 共通ヘルパー `_inflight_fingerprint_conflict` を追加し、両エンドポイントで結合前に fingerprint を比較。不一致は 409 を返し、user message の追記も新規課金ジョブの起動も行わない。fingerprint を持たない旧形式 holder は従来どおり結合する | `test_chat_inflight_token_conflict_is_rejected`, `test_chat_inflight_same_payload_still_joins`, `test_analyze_inflight_token_conflict_is_rejected`, `test_inflight_fingerprint_conflict_helper_contract` |

## 3. 横断展開の確認結果

| 対象 | 確認内容 |
| :--- | :--- |
| `stored_at` 封筒の書き込み箇所 | `StockDiskCache.set` と `remove_fields_recursive` の 2 箇所のみ。両方を `stored_at` 先頭へ正規化した。読み取り側（`get` / `has` / `get_stale` / `_read_stale_payload`）はフィールド順に依存しない |
| token による実行中ジョブ結合 | 全 `*_fetch_inflight` を確認。`news` の inflight キーは戦略単位で市場全体のバンドルを返すため、ペイロード別の取り違えは発生しない。technical-lines / analyze-chart-image は同期実行で結合経路がない。chat の SSE 経路も同じ claim ブロックを通るため修正が適用される |
| leader ロックの獲得経路 | `_try_acquire_atomic_lock` / `_try_acquire_advisory_file_lock` の全成功経路でモードとパスを設定。`_release_leader_lock` / fork 子プロセス / 継承 fd のデタッチで解除することを確認 |
| 同種の所有権検証 | `_verify_leader_ownership` の呼び出し元は選挙ループのみ。他に「fd 保持だけを根拠に所有権を主張する」箇所がないことを確認 |

## 4. 誤検知・過剰修正として除外した項目

| 項目 | 判断 |
| :--- | :--- |
| `StockDiskCache.has()` の全体読み込み | 単一キーの明示照会であり、ディレクトリ全体を走査する掃除とは異なる。また壊れたエントリを「無い」と判定する既存契約（`get` と同一）を保つには本体の解析が必要なため、今回は変更しない |
| 期限切れ掃除での旧形式エントリの完全解析 | 64 KiB 以下に限定して維持。それ以上の旧形式は従来どおり mtime 判定で、新形式への移行時に正規化される |
| `/api/news` の token 再利用 | inflight キーが戦略単位で、応答は市場全体のバンドル。token はキャッシュキーの一部に過ぎず、別ペイロードの回答を返す構造ではない |
| health の loopback ゲート、`/api/chat` の `code` / `error_code` 非対称、同期 AI エンドポイント、config io/merge のロック順、Agents SDK の env キー共有、native host の同一ユーザー境界 | 既存テストが固定する意図的契約、または設計課題として過去ラウンドで除外済み。今回も変更しない |

## 5. 最終検証結果

| チェック | 結果 |
| :--- | :--- |
| pytest 全件（`-n auto --dist=loadfile --timeout=120`） | **4607 passed, 5 skipped, 47 subtests passed**（exit 0） |
| pytest 全件 + coverage（CI と同一の `--cov=. --cov-fail-under=75`） | exit 0、Total coverage **82.96%**（75% ゲート通過） |
| 新規回帰テスト 13 件 | 全パス |
| ruff（`--line-length=100`） | All checks passed |
| mypy | Success: no issues found in 125 source files |
| pyrefly | 0 errors |
| `npm run build`（typecheck / verify-generated / compile / lint / prettier） | exit 0 |
| 作業ツリー | 変更 3 ファイル + 新規回帰テスト 1 ファイルのみ。調査用ログ・デバッグコード・不要な未追跡ファイルなし |
