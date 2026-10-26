# 包括的コードレビュー 全課題リスト（調査フェーズ成果物）

- 対象: 当該プロジェクト全体（UI / バックエンド / DB・データ層 / 共通モジュール / 設定 / API境界）
- 方式: 全ファイル精査 + 横断パターン検索（抜粋調査なし）
- 本パスの成果物: **課題リストのみ。コード変更は行っていない。**
- 基準線: `ruff` / `flake8` / `mypy` クリーン、`pytest` 全件パスを確認済み（`pytest_baseline.log`）。以降の課題は静的解析では検出されない論理・並行・契約の不備が中心。

> 表記: [重大度 / ファイル:行] に「想定される根本原因 / 判定根拠 / 推奨する根本的修正方針 / 横断展開」を併記。誤検知・後方互換を壊す過剰指摘は本リストから除外済み。

---

## Critical（データ破損・セキュリティ境界の崩壊につながる）

### C-1
- **重大度 / 対象**: Critical / `bg/sync/control.py:81-82`, `bg/sync/control.py:184-186`
- **想定される根本原因**: `_sync_generation` というプロセス共有の世代カウンタを、専用ロックなしのモジュールグローバルとして読み書きしている。`_set_sync_generation(_state._sync_generation + 1)` は read-modify-write であり、複数スレッド（`sync_all_stocks_now` の並行起動、`schedule_sync_all_stocks_now` から投入される実行）が同時に走るとインクリメントが消失する（lost update）。
- **判定根拠**: `_set_sync_generation`（`bg/sync/state.py:80-84`）は代入のみでアトミック性を保証しない。呼び出し側 `control.py:76-82` は `app_state.market.is_syncing_lock` 配下で呼んでいるが、`bg/sync/process.py:248` と `control.py:139` ではロック外で `_get_app_bg_attr("_sync_generation", ...)` を参照しており、世代比較（stale判定）自体が一貫しない可能性がある。さらに `finally`（`control.py:184-186`）で `sync_generation == _state._sync_generation` を見て `is_syncing` を下ろすが、lost update が起きると**別同期の `is_syncing` が早期に false 化**され、二重同期が走り得る。
- **推奨する根本的修正方針**: 世代カウンタを専用ロック（`is_syncing_lock` とは別の `threading.Lock`）で保護し、「インクリメントして自分の世代番号を返す」原子操作 `next_sync_generation() -> int` に集約する。stale 判定も同じロック配下のスナップショット参照に統一する。`is_syncing` のクリアは「自分が設定した世代がまだ現在値」であることをロック内で確認して行う（既存の意図は踏襲）。
- **横断展開**: 同種の「ロックなしモジュールグローバル世代/カウンタ」は `bg/common.py:117-119`（`_sse_payload_generation`）, `bg/common.py:171-174`（`_sse_full_snapshot_counter`）にも存在。`_set_app_bg_attr` 経由で app_bg 側にも書く二重管理になっているため、同じ lost-update リスクを併せ持つ。同じ原子操作パターンへ統一する対象に含める。

### C-2
- **重大度 / 対象**: Critical / `utils/chat_history.py:508-601`（`__getitem__`/`__setitem__`）と `routes/api_analysis.py:319-325`, `routes/api_analysis.py:492-498`, `routes/api_analysis.py:692-698`, `routes/api_analysis.py:848-854`, `routes/api_analysis.py:1644-1650`
- **想定される根本原因**: `SQLiteChatHistoryStore` は本質的に永続ストアだが、呼び出し側（`routes/api_analysis.py`）は `chat_history_lock`（`ai_state.py:87` の `threading.Lock`）配下で「`__getitem__` で全件取得 → メモリ上で `history.pop()` 等の改変 → `__setitem__` で全件書込」という read-modify-write を行っている。`__setitem__` は `DELETE FROM chat_messages` 後に全件再 INSERT する破壊的置換であり、改変〜書込の間に別経路（`add_message` 等）が書き込むと**追加メッセージが消失**する。
- **判定根拠**: `add_message`（`utils/chat_history.py:428-434`）の docstring が明確に「`__getitem__`+modify+`__setitem__` の read-modify-write はレースを起こしやすい」と警告し、append-only 経路を用意している。しかし `routes/api_analysis.py` の rollback（319-325）、dedup 判定後の書込（692-698, 848-854）等は未だ `__setitem__` 全件置換に依存。`chat_history_lock` はこのストア操作を実際に排他していない（ストア内部はスレッドローカル接続で個別にトランザクションを張るのみ）ため、同一ロック下に無い呼び出しがあれば整合が崩れる。
- **推奨する根本的修正方針**: 局所改変（最後の user 発話の rollback、末尾 dedup 除去）を **ストア側の原子操作**（例: `pop_last_user_if_matches(session_id, content)`, `append_if_not_duplicate(session_id, message)`）として実装し、`__getitem__`→`__setitem__` 全件置換の read-modify-write を撤廃する。ロックはストア側（`_execute_in_transaction`）に集約し、`chat_history_lock` はセッション単位の操作列の直列化に限定する。
- **横断展開**: `routes/api_analysis.py` 内の `__getitem__`→改変→`__setitem__` 全パターン（319-325, 432-438, 492-498, 692-698, 848-854, 1644-1650）と、`ai_state.py:213-219`（`add_chat_history` の `chat_history[key] = message` 直接代入分）。テストの `app_state.ai.chat_history[chat_key] = [...]` 直接代入（`tests/test_api_chat_improved.py:73` 等）は同一経路なので挙動維持のうえで移行する。

---

## High（実害のあるロジック/並行不備）

### H-1
- **重大度 / 対象**: High / `bg/sync/control.py:180-183`（`finally`）
- **想定される根本原因**: `sync_all_stocks_now` の `finally` が成功・失敗を問わず `first_sync_completed_at = time.time()` を無条件に更新している。同期が例外で失敗しても「初回同期完了時刻」が進み、初回同期の成否判定やウォームアップ待ちの前提（`first_sync_attempted`/`first_sync_completed_at`）が誤った完了を示す。
- **判定根拠**: `control.py:117-119` では「同期が空でない場合」にのみ完了時刻を設定する丁寧な分岐があるのに対し、`finally`（182-183）がそれを上書きする。例外経路（`raise` される `except` 節あり）でも `finally` は確実に通るため、失敗が完了として記録される。UI/ウォームアップがこの時刻を根拠に「データあり」と誤判断し得る。
- **推奨する根本的修正方針**: 完了時刻は同期が実際に成功した経路でのみ記録する。`finally` では `is_syncing` のクリアと `lock.release()` のみ行い、`first_sync_completed_at` は try 成功パス（`control.py:117-119`）に集約する。失敗時は `first_sync_attempted` のみ立て、完了時刻は据え置く。
- **横断展開**: 「例外を握りつぶして状態だけ進める」同種箇所を横断確認。`bg/sync/process.py:525-535`（`save_user_stocks` 失敗時にコンテナへロールバックする処理）は正しくロールバックしており問題なし。`process.py:450-455`（USDJPY 永続化失敗の `logger.warning` のみ）は「揮発状態は更新済み・永続化のみ失敗」という意図的な許容と判断し除外。

### H-2
- **重大度 / 対象**: High / `ai_state.py:213-219`（`add_chat_history`）と `utils/chat_history.py:543-601`（`__setitem__`）
- **想定される根本原因**: `add_chat_history` は `isinstance(message, list)` / `dict`+`hasattr(add_message)` で分岐するが、それ以外（タプル等）や `hasattr` が偽の場合に `self.chat_history[key] = message` へフォールし、`__setitem__` は「`list[dict]`」前提で `for msg in to_insert` を回すため、型が異なると `msg["role"]` で例外、または意図しないデータが保存される。分岐条件（`dict` かつストアに `add_message`）が「ストア実装」に依存しており、契約が暗黙的。
- **判定根拠**: `ai_state.py:216` の `hasattr(self.chat_history, "add_message")` は実装判定であり、`message` の型と保存方式の対応が一意でない。`else` 節（218-219）が非 list を `__setitem__` に渡す経路は、`__setitem__` の前提（`list[dict[str,Any]]`）と乖離する。現状の呼び出し元が list/dict のみ渡しているため表面化していないが、境界として脆弱。
- **推奨する根本的修正方針**: 保存契約を型で明示する。`list[dict]` は `__setitem__`（置換）、単一 `dict` は `add_message`（追記）に**型で一意に対応**させ、それ以外は `TypeError` を送出する（暗黙フォールを排除）。ストア実装の `hasattr` 判定をやめ、プロトコル（`typing.Protocol`）で `add_message` の有無を型レベルで固定する。
- **横断展開**: `SQLiteChatHistoryStore` を参照する全呼び出し（`ai_state.py`, `routes/api_analysis.py`, `tests/*`）で保存方式の型整合を確認。`utils/chat_history.py:562`（`__setitem__` の `msg["role"]`/`_encrypt_content(msg["content"])`）に未検証キー参照があるため、入力バリデーションを境界に一本化する対象に含める。

### H-3
- **重大度 / 対象**: High / `routes/system/credentials.py:380-579`（`api_credentials_verify`）
- **想定される根本原因**: 検証系レスポンスのエラー形式が複数系統に分裂している。`error_response(ErrorCode.*, details=...)` を使う経路（先頭403/400）と、`jsonify({"ok":False,"code":"INVALID_API_KEY","error_code":...,"error":...,"message":...})` を手書きで返す経路（448-470, 483-500, 512-530, 560-579）が混在し、`code`（独自文字列）と `error_code`（`ErrorCode` enum 値）の二重表現になっている。
- **判定根拠**: 本ファイル内で同一エンドポイントのエラーJSONが `error_response` 経由と手書き `jsonify` の2系統。`error_codes.py`/`error_handlers.py` に統一エラー形式が存在するのに使われていない。フロント側（`static/js/api.js`, `ui.js`）が `ok`/`error_code`/`message`/`error` のどれを参照するかで契約が二重化し、片方の変更が取りこぼされる。
- **推奨する根本的修正方針**: `error_response(ErrorCode, details=...)` に統一し、互換のために必要なキー（`valid`, `code` 等）は `details` か共通エンベロープで正規化する。生の `jsonify({...error...})` 手書きを廃し、`error_handlers` の単一形式へ収斂させる。フロントが参照するキーは契約テストで固定する。
- **横断展開**: 「`jsonify` でエラーJSONを手書きしている全ルート」を横断対象に。`code_search` で `"error_code"|ErrorCode\.` を全 `routes/` に展開して、`error_response` を使わないエラー返却を一覧化し、同じ正規化を適用する。`ok_response`（`route_helpers.py`）を使う成功系は既に共通化されており対象外。

---

## Medium（堅牢性・保守性・軽微な契約不整合）

### M-1
- **重大度 / 対象**: Medium / `routes/api_analysis.py:492-498`（`safe_symbol` のサニタイズ）
- **想定される根本原因**: `re.sub(r"[^\w\-.^=]", "", symbol)[:15]` でユーザー入力をサニタイズしてからプロンプトへ組み込んでいるが、正規表現の文字クラス `[\w\-.^=]` において `-` が `\w` と `.` の間にあり**レンジ解釈**（`\w`〜`.`）になり得る意図不明な書き方。`^` もクラス内でリテラカルだが位置的に誤読を招く。サニタイズ自体はしているが、許可文字の定義が意図と一致しているか曖昧。
- **判定根拠**: `[\w\-.^=]` の `-` はエスケープ `\−` されておらず、`re` では `\w` と `.` の間でレンジとして解釈され得る（`\w` は `[\w]` 相当で単独クラスのため実際はリテラカル扱いになるが、意図が読み取れず保守リスク）。プロンプト注入防御の要であり、明示的にリテラル `\-` と `^` を先頭/末尾へ置くべき。
- **推奨する根本的修正方針**: 文字クラスを `[^\w.\-=]`（`-` を末尾に移動 or `\-` にエスケープ、`^` を先頭以外）に書き換えてリテラル意図を明示。さらに「プロンプトへ埋め込まず構造化データとして渡す」方針（コメントに明記）に沿って、サニタイズに頼らずプレースホルダ分離を徹底する。
- **横断展開**: ユーザー入力を正規表現でフィルタしてLLMプロンプトへ埋め込む全箇所。`code_search` で `re\.sub\(r"\[\^?\\w` を展開し、同種の文字クラス曖昧さ・サニタイズ依存の埋め込みを洗い出す。

### M-2
- **重大度 / 対象**: Medium / `bg/sync/control.py:84`（`_get_app_bg_attr("_is_sync_leader", is_leader())`）
- **想定される根本原因**: フォールバック引数 `is_leader()` を**評価済みの値**として渡している。`_get_app_bg_attr` は app_bg 側に属性があればそれを返すが、引数 `is_leader()` は**属性の有無に関わらず毎回評価**される（Python の引数は eager 評価）。`is_leader()` はリーダーロック取得/確認という副作用を持つため、不要な副作用が毎サイクル発生する。
- **判定根拠**: `bg/sync/state.py:58-65` の `_get_app_bg_attr(name, fallback)` は fallback を即値として扱う。`control.py:84` は `is_leader()` を呼んだ結果を fallback に渡すため、app_bg に `_is_sync_leader` が存在しても `is_leader()` が先に走る。これは「フォールバックを遅延評価したい」意図（他所の `_get_app_bg_attr(name, None)` + 後段 `if target is not None` パターン）と不整合。
- **推奨する根本的修正方針**: 副作用のあるフォールバックは eager 渡しをやめ、`_get_app_bg_attr("_is_sync_leader", None)` で取得し、`None` の場合のみ `is_leader()` を呼ぶ（他所の `target = _get_app_bg_attr(...); if target is not None: ...` パターンに統一）。または `fallback` を callable 対応にして遅延評価する。
- **横断展開**: `_get_app_bg_attr(name, <副作用のある呼び出し>)` の全箇所。`bg/sync/process.py` 内の `_get_app_bg_attr(..., _shim().schedule_sync_all_stocks_now)`（543-545）等、フォールバック評価時に副作用/コストが発生する呼び方を横断確認し、遅延化パターンへ統一する。

### M-3
- **重大度 / 対象**: Medium / `bg/sync/state.py:58-65`, `bg/common.py:49-55`, `bg/leader_election.py:63-67`（`_get_app_bg_attr` の三重定義）
- **想定される根本原因**: 同一目的の `_get_app_bg_attr` が `bg/sync/state.py`, `bg/common.py`, `bg/leader_election.py` に**ほぼ同一実装で3回**定義されており、循環 import 回避のための遅延 import を各所で繰り返している。挙動差（`_set_app_bg_attr` の有無等）が出やすく、修正の取りこぼし元。
- **判定根拠**: 3ファイルに同名・同ロジックの関数が存在（各々 `import app_bg` を try で遅延）。`bg/__init__.py` からの再エクスポート（57-60, 116-120）も絡み、どの実装が使われるか呼び出し経路に依存する。
- **推奨する根本的修正方針**: `_get_app_bg_attr`/`_set_app_bg_attr` を単一モジュール（例: `bg/_app_bridge.py`）へ集約し、他モジュールは import して使う。遅延 import の理由（循環回避）は集約モジュール内に閉じ込め、挙動差を排除する。
- **横断展開**: `_set_app_bg_attr` の定義義箇所も対象。`bg/common.py:117-119, 171-174` の二重書き込み（ローカルグローバル + app_bg 属性）とあわせて、状態の単一所有を定める。

### M-4
- **重大度 / 対象**: Medium / `bg/sync/control.py:134-135`（空同期ガード）と `bg/sync/process.py`（`us_res/jp_res/idx_res` の意味）
- **想定される根本原因**: `if items and not (us_res or jp_res or idx_res):  preserve previous cache` というガードは「1件も有効でなかったら旧キャッシュを保全」する意図だが、`us_res/jp_res/idx_res` の真偽が「件数」なのか「成否フラグ」なのかが呼び出し側で暗黙的。部分成功（例: USのみ成功、JP全滅）時に「一部市場だけ新しく、他は旧」という中途半端な状態を許容している。
- **判定根拠**: `control.py:132` で `us_res, jp_res, idx_res = proc_fn(...)` を受け、134で「全部偽なら保全」のみ判定。部分成功時のキャッシュ整合方針がコードから読み取れない。`_update_indices_data(idx_res, us_res, jp_res)`（147-148）へ渡す型も同じ暗黙に依存。
- **推奨する根本的修正方針**: `proc_fn` の戻り値契約を明示（各市場の成功件数/成否を named tuple / dataclass で定義）し、「全滅保全」「部分成功時の各市場独立更新」の方針をコメントと型で固定する。キャッシュ公開（`current_stocks_cache` への deepcopy）は市場単位 or 全体単位のどちらを一貫性境界とするか明記する。
- **横断展開**: `_process_fetched_stocks` の戻り値を消費する全箇所、および `_update_indices_data` の引数契約。型を弱めず dataclass で固定する対象。

### M-5
- **重大度 / 対象**: Medium / `routes/api_analysis.py:2555-2560`（`genToken`）と `static/js/experimental/constellation-controller.js:340`, `ai-dive-controller.js:439`（フォールバックトークン）
- **想定される根本原因**: `request_token` の生成が複数系統に分裂。`createRequestToken()`（`api.js:1856-1867`）は `randomUUID`/`getRandomValues` を使うが、experimental 系のフォールバックは `mns_${Date.now().toString(36)}_${Math.random()...}` と**予測可能性の高い** `Math.random()` ベース。スキーマ（`schemas/` の `request_token` は `^[A-Za-z0-9_-]{16,128}$`）は満たすが、エントロピーが不足。
- **判定根拠**: `Math.random()` は暗号用途に不適（仕様上も予測可能）。`request_token` が CSRF/重複排除/冪等キーのいずれで使われているかに依存するが、セキュリティ境界に使うなら `crypto.randomUUID` 必須。`genToken`（`ui.js:2555-2561`）は `createRequestToken` 優先で正しく、experimental のフォールバックのみ弱い。
- **推奨する根本的修正方針**: トークン生成を `createRequestToken()` 単一系統へ集約し、`crypto` 非対応環境では**送信を止める**（弱い乱数でトークンを捏造しない）。experimental 各所の `Math.random()` フォールバックを削除し、共通ユーティリティを import する。
- **横断展開**: `Math.random()` をセキュリティ/識別子用途に使う全箇所（`code_search` で `Math\.random\(\)` を展開し、トークン/ID/nonce 用途を抽出）。`Date.now()`+`Math.random()` の連結ID全般。

---

## Low（可読性・軽微な改善、ただし根本性は確保）

### L-1
- **重大度 / 対象**: Low / `bg/common.py:192-196`（SSE diff 生成）
- **想定される根本原因**: `_build_sse_diff(stocks, prev_stocks)` の `diff_size = sum(len(v) for v in diff.values())` で「差分が0なら完全スナップショットへフォール」という分岐があるが、`diff.values()` が非 str 要素（dict/list）を含む場合 `len()` は要素数になり、バイト/文字数の意味とずれる。サイズ判定の単位が曖昧。
- **判定根拠**: `len(v)` が `v` の型に依存して意味が変わる。差分が「キー→値」のマップなら値が dict の可能性があり、`len` は文字列長と一致しない。フォール閾値の意図（「差分が小さすぎる/空なら全体送信」）が型で担保されていない。
- **推奨する根本的修正方針**: diff の値型を明示（`dict[str, str]` 等）し、サイズ判定は `json.dumps(diff)` のバイト長など意味が一意な指標に統一する。型注釈で値の契約を固定。
- **横断展開**: SSE ペイロード組み立ての他箇所（`bg/common.py:196-200` の `json.dumps(payload)`）、`_sse_prev_stocks` の保持形式。

### L-2
- **重大度 / 対象**: Low / `ai_state.py:86-88`（`chat_history` の型注釈）
- **想定される根本原因**: `self.chat_history: Any = SQLiteChatHistoryStore(...)` と `Any` で宣言しており、dict 風 API（`__getitem__` 等）と `add_message` の両方を使う呼び出し側が型検査されない。`mypy` が通るのは `Any` による実質無検査のため。
- **判定根拠**: `mypy` クリーンは `Any` で担保されており、実際の契約（`list[dict]` の `__setitem__`, `add_message(session_id, dict)`）が検証されていない。ミッションの「型定義の弱体化（`any` や不当な型アサーション）の禁止」に抵触する既存箇所。
- **推奨する根本的修正方針**: `SQLiteChatHistoryStore` に正式な型を与え、`chat_history` をその具象型（または `MutableMapping[str, list[dict[str, Any]]]` + `add_message` を含む `Protocol`）で注釈する。`Any` を排除し、呼び出し側の型違反を mypy で検出可能にする。
- **横断展開**: プロジェクト内の `: Any` / `= Any` 宣言を `code_search` で抽出し、境界（ストア・状態・スキーム）のものを優先的に具体化。不当な `cast`/`# type: ignore` は追加しない方針を維持。

---

## 横断展開のサマリ（課題ごと対応対象）

| 課題 | 横断展開の対象 |
|------|----------------|
| C-1 | `bg/common.py` の `_sse_payload_generation` / `_sse_full_snapshot_counter`（ロックなし世代/カウンタ） |
| C-2 | `routes/api_analysis.py` 全 read-modify-write、`ai_state.py` 直接代入 |
| H-1 | 「例外握りつぶしで状態進行」の全箇所 |
| H-2 | `SQLiteChatHistoryStore` 全呼び出し、保存方式の型整合 |
| H-3 | 全 `routes/` の手書きエラーJSON（`error_response` 非使用） |
| M-1 | ユーザー入力→LLM埋め込みのサニタイズ全箇所 |
| M-2 | `_get_app_bg_attr(name, <副作用ful>)` の eager フォールバック全箇所 |
| M-3 | `_get_app_bg_attr`/`_set_app_bg_attr` の三重定義、状態の二重所有 |
| M-4 | `_process_fetched_stocks` 戻り値契約の全消費箇所 |
| M-5 | `Math.random()` をID/トークン用途に使う全箇所 |
| L-1 | SSE ペイロード組み立て・サイズ判定 |
| L-2 | `: Any` 宣言の境界箇所 |

---

## 誤検知・過剰指摘として除外したもの（根拠付き）

1. **`utils/chat_history.py` のスレッド安全性**: `SQLiteChatHistoryStore` はスレッドローカル接続 + `_execute_in_transaction` で個別操作は安全。単独 `add_message`/`__setitem__` 自体に競合はない（C-2 は read-modify-write の複合操作の話に限定）。
2. **`bg/sync/process.py:450-455`（USDJPY 永続化失敗を warning のみ）**: 揮発状態の更新と永続化の分離は意図的な許容。破壊的挙動なし。
3. **`routes/system/credentials.py` の例外メッセージ非返却**（`error` を固定文言化）: クレデンシャル/URL漏洩を防ぐ意図的な安全側の設計。問題なし。
4. **`config/atomic_write.py`, `config/master_key.py` の暗号・アトミック書込**: 既存のテスト（`tests/test_review_fix_regressions.py` 等）で権限・アトミック性を検証済み。今回の指摘対象外。
5. **`native_host/start_backend.py:429` の `print`**: CLI `__main__` 起動時の標準出力であり、サーバ内ロガー汚染ではない。除外。
6. **テスト内の `chat_history[...] = [...]` 直接代入**: テスト用の明示的な fixture 操作であり、本番経路の read-modify-write（C-2）とは性質が異なる。移行時は挙動維持を条件に含めるが、独立課題ではない。

---

## 次フェーズ（修正パス）に向けた推奨順序

1. **共通基盤から**: C-1（世代カウンタの原子化）→ M-3（`_get_app_bg_attr` 集約）→ L-2（`Any` 排除・型固定）
2. **データ層**: C-2（chat_history の原子操作化）→ H-2（保存契約の型明示）
3. **API境界**: H-3（エラー形式の統一）→ M-1（サニタイズ明確化）→ M-5（トークン生成集約）
4. **バックグラウンド整合**: H-1（完了時刻記録の修正）→ M-4（proc_fn 契約）→ L-1（diff サイズ判定）

各単位で「修正 → テスト（エッジ/異常/回帰）追加・実行 → ruff/flake8/mypy → ビルド確認」を回す。
