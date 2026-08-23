# Chrome拡張・Native Host 領域 静的・動的レビュー結果

- レビュー日: 2026-08-16
- レビュー担当: Chrome拡張 / Native Host 領域
- 対象: `chrome_extension/` と `native_host/`（バックエンド側の関連コードを参考）
- 方針: コード変更なし・秘密情報の出力なし・Native Host の実ファイル書き込み/外部実行なし（テンプレート検証のみ）

---

## 1. 実行したテスト（裏取り）

`uv run --locked --group test python -m pytest <テストファイル群> -q --timeout=60` を実行。

| テストファイル                          | 結果        |
| --------------------------------------- | ----------- |
| `tests/test_native_host.py`             | ✅ 全件成功 |
| `tests/test_native_host_security.py`    | ✅ 全件成功 |
| `tests/test_extension_review_fixes.py`  | ✅ 全件成功 |
| `tests/test_coverage_storage_native.py` | ✅ 全件成功 |
| `tests/test_detected_stock_add.py`      | ✅ 全件成功 |
| `tests/test_csrf_protection.py`         | ✅ 全件成功 |

- `tests/test_storage_native.py` は存在しないためスキップ（タスク指示に含まれていたが、リポジトリに無いことを確認）。
- 追加で `_sanitize_log_message` の実挙動を直接実行し、マスキング漏れを確認（下記 NH-1）。

---

## 2. 問題候補一覧

| ID                   | 重要度 | 概要                                                                            |
| -------------------- | ------ | ------------------------------------------------------------------------------- |
| [NH-1](#nh-1-medium) | Medium | `_sanitize_log_message` のマスキング不完全（Bearer トークン・引用符内値の漏洩） |
| [NH-2](#nh-2-medium) | Medium | `get_extension_api_token` がバックエンド稼働状態を確認せずトークンを発行        |
| [NH-3](#nh-3-low)    | Low    | `native_host.log` がソースツリーに書き込まれる（デフォルト時）                  |
| [NH-4](#nh-4-low)    | Low    | `install_host_windows.ps1` の `Test-SafePath` が `^`/`!` を検出しない           |
| [EXT-1](#ext-1-low)  | Low    | content.js がページテキストを抽出（プライバシー考慮）                           |
| [EXT-2](#ext-2-low)  | Low    | `chrome.storage.session` 未対応ブラウザでの例外（try-catch 欠如）               |

Critical / High に該当する問題は検出されなかった。

---

## 3. 問題詳細

### NH-1 (Medium): `_sanitize_log_message` のマスキング不完全

- **該当箇所**: [`native_host/native_host.py`](native_host/native_host.py:71) の `_sanitize_log_message`（正規表現 `[^\s'\"]+` を使用）
- **影響経路**: ログレコードに `Authorization: Bearer <token>` 形式や引用符を含む値が含まれると、トークンが `native_host.log` に漏洩する。`SanitizedFormatter` は全ログレコードに適用されるため、防御機構としての役割を果たせない。
- **問題・根本原因**: マスキング正規表現が空白・引用符で値を区切るため、
  - `Authorization: Bearer abc.def.ghi` → `Bearer` のみマスクされ、トークン本体 `abc.def.ghi` が残る
  - `token=abc"def` → `abc` のみマスクされ、`"def` が残る
- **重要度評価**: Medium。現状の Native Host 自身のログ経路ではトークンを直接記録しないため実害は潜在的なものだが、「ログから機密情報を削除する」という明示的なセキュリティ制御が一般的な形式で機能しないことは客観的に確認された。
- **客観的根拠**: 直接実行で確認。
  - `_sanitize_log_message('Authorization: Bearer abc.def.ghi')` → `'[REDACTED] abc.def.ghi'`（トークン漏洩）
  - `_sanitize_log_message('token=abc"def')` → `'[REDACTED]"def'`（部分漏洩）
  - `_sanitize_log_message('api_key=sk-1234567890')` → `'[REDACTED]'`（正常）

### NH-2 (Medium): `get_extension_api_token` がバックエンド稼働状態を確認しない

- **該当箇所**: [`native_host/native_host.py`](native_host/native_host.py:940) の `get_extension_api_token` 分岐
- **影響経路**: バックエンド停止中でも拡張APIトークン（デフォルト90日有効）を発行・返却する。`get_or_create_extension_api_token()` はトークンが無ければ新規生成するため、停止中でもトークンが作成・返却される。
- **問題・根本原因**: 設計の非対称性。`get_shutdown_token` は「バックエンド停止中は秘密を渡さない」方針（[`native_host/native_host.py`](native_host/native_host.py:784) の `is_backend_healthy_once()` チェック）を適用しているが、`get_extension_api_token` には適用されていない。拡張APIトークンはシャットダウントークンより長命（90日）で繰り返し使用可能なため、停止中に発行される秘密の価値はむしろ高い。
- **重要度評価**: Medium。ブラウザ祖先検証（`_is_caller_authorized_browser`）とトークンアクションのレート制限（3回/30秒）がゲートとして機能するため実害は限定的だが、明示されたセキュリティモデルとの不整合は客観的に確認できる。
- **客観的根拠**: コード比較。`get_shutdown_token` は `is_backend_healthy_once()` を確認してから返却するが、`get_extension_api_token` は確認しない。

### NH-3 (Low): `native_host.log` がソースツリーに書き込まれる

- **該当箇所**: [`native_host/native_host.py`](native_host/native_host.py:96)
- **影響経路**: `MNS_DATA_DIR` / `MNS_APP_DATA_DIR` が未設定の場合、`_log_dir` が `Path(__file__).parent`（= `native_host/` ディレクトリ）にフォールバックし、`native_host.log` がソースツリー内に書き込まれる。誤ってコミット・配布される可能性がある。
- **問題・根本原因**: フォールバック先がソースツリー内。`config_store.APP_DATA_DIR`（[`config_store.py`](config_store.py:34)）はユーザー別ランタイムディレクトリへ解決するが、Native Host のログは環境変数未設定時にソースツリーへ落ちる。
- **重要度評価**: Low。ログは `SanitizedFormatter` でマスクされるため機密漏洩リスクは低いが、運用上の実害（ソースツリーの汚染・誤コミット）がある。
- **客観的根拠**: コードのフォールバック分岐を確認。

### NH-4 (Low): `install_host_windows.ps1` の `Test-SafePath` が `^`/`!` を検出しない

- **該当箇所**: [`native_host/install_host_windows.ps1`](native_host/install_host_windows.ps1:27) の `Test-SafePath`
- **影響経路**: Python 実行パスに cmd のエスケープ文字 `^` や遅延展開文字 `!` が含まれる場合、生成される `native_host.cmd` で意図しない解釈が起きる可能性がある。
- **問題・根本原因**: パス検証の正規表現 `'\.\.|[|><&;`"]'`が`..`、`|`、`>`、`<`、`&`、`;`、バッククォート、`"` のみを検出し、`^`と`!`を検出しない。ただし`%`は`%%` へのエスケープ（[`install_host_windows.ps1`](native_host/install_host_windows.ps1:166)）で対応済み。
- **重要度評価**: Low。Python パスはテンプレート内で二重引用符に囲まれるため、`^`/`!` の実害は限定的。遅延展開は既定で無効。
- **客観的根拠**: 正規表現の文字クラスを確認。

### EXT-1 (Low): content.js がページテキストを抽出（プライバシー考慮）

- **該当箇所**: [`chrome_extension/content.js`](chrome_extension/content.js:287) の `extractPageTextSnippets` / `detectTickers`
- **影響経路**: アクティブページのテキストノードからティッカーと周辺スニペット（前後25文字）を抽出し、ポップアップの「ページ検出」タブに表示する。ページ内の機密テキスト（口座番号・個人情報など）がスニペットとして表示される可能性がある。
- **問題・根本原因**: 設計上のプライバシー考慮。`SCRIPT`/`STYLE`/`INPUT`/`TEXTAREA` 等は除外されるが、通常の本文テキストは抽出対象。
- **重要度評価**: Low。ユーザーが「ページ検出」タブを開いた時のみ実行され、抽出結果はローカルのポップアップ表示に留まり外部送信はない。`activeTab` 権限によるオンデマンド注入（[`chrome_extension/popup.js`](chrome_extension/popup.js:581)）で、全サイトへの常時注入は行っていない。
- **客観的根拠**: コードの抽出ロジックと注入方式を確認。

### EXT-2 (Low): `chrome.storage.session` 未対応ブラウザでの例外

- **該当箇所**: [`chrome_extension/background.js`](chrome_extension/background.js:54) の `setMnsExtensionToken`、[`chrome_extension/background.js`](chrome_extension/background.js:76) の `chrome.storage.session.get`
- **影響経路**: `chrome.storage.session` が未対応のブラウザ（Chrome 102 未満等）で `set`/`get` が例外を投げ、サービスワーカー初期化が失敗する可能性がある。
- **問題・根本原因**: `setMnsExtensionToken` と `chrome.storage.session.get` に try-catch がない（`getOrFetchExtensionToken` 内の `remove` は try-catch あり）。
- **重要度評価**: Low。最新の Chrome/Edge では対応済みであり、実害は限定的。
- **客観的根拠**: コードの try-catch 有無を確認。

---

## 4. 問題なし（確認済み）項目

### Chrome拡張

- **manifest.json 権限の最小性**: [`chrome_extension/manifest.json`](chrome_extension/manifest.json:6) の `permissions`（nativeMessaging, contextMenus, alarms, sidePanel, storage, activeTab, scripting）は各機能に必要な最小限。`host_permissions` は `http://127.0.0.1/*` と `http://localhost/*` のループバックのみで、`<all_urls>` や `https://*/*` は含まれない。
- **CSP**: [`chrome_extension/manifest.json`](chrome_extension/manifest.json:25) の `script-src 'self'; style-src 'self'; object-src 'none'; connect-src http://127.0.0.1:* http://localhost:*`。`'unsafe-inline'` / `'unsafe-eval'` なし。`connect-src` はループバックのみ。
- **web_accessible_resources**: 未定義（過剰でない）。
- **content_scripts**: 未定義。`activeTab` + `scripting` によるオンデマンド注入（[`chrome_extension/popup.js`](chrome_extension/popup.js:581)）で、全サイトへの常時注入なし。
- **任意コード実行**: `eval` / `new Function` / `innerHTML` によるユーザーデータの挿入なし。`popup.js` は `textContent` を使用（[`chrome_extension/popup.js`](chrome_extension/popup.js:2) の `setSafeText`、[`chrome_extension/popup.js`](chrome_extension/popup.js:143) の `symbolSpan.textContent` 等）。
- **外部通信**: 通信先はループバックのバックエンドのみ。`addStockViaExtension`（[`chrome_extension/background.js`](chrome_extension/background.js:136)）と `stopBackend`（[`chrome_extension/background.js`](chrome_extension/background.js:624)）は `health.base`（ループバック）へ送信。
- **ルートサニタイズ**: [`chrome_extension/background.js`](chrome_extension/background.js:17) の `sanitizeRoute` がホワイトリスト方式。
- **銘柄シンボル検証**: [`chrome_extension/background.js`](chrome_extension/background.js:417) の `SYMBOL_RE`（`^[A-Za-z0-9.\-^=]{1,15}$`）で検証。
- **メッセージ送信者検証**: [`chrome_extension/background.js`](chrome_extension/background.js:546) で `sender.id !== chrome.runtime.id` を拒否。
- **トークン保存**: 拡張APIトークンは `chrome.storage.session`（メモリ内・ブラウザ再起動で消去）に保存。シャットダウントークンはメモリのみ（[`chrome_extension/background.js`](chrome_extension/background.js:47) のコメント参照）。
- **診断情報のマスキング**: [`chrome_extension/popup.js`](chrome_extension/popup.js:372) の `maskExtensionId` で拡張IDをマスク。

### Native Host

- **メッセージサイズ制限**: [`native_host/native_host.py`](native_host/native_host.py:193) の `MAX_MESSAGE_BYTES`（既定1MB、下限4096）と `MAX_DRAIN_BYTES`。過大フレームはドレイン後に `SKIP_FRAME`、ドレイン不能は `FATAL_FRAME` でチャネル終了（[`native_host/native_host.py`](native_host/native_host.py:641)）。
- **型検証**: [`native_host/native_host.py`](native_host/native_host.py:754) で `dict` 以外を拒否。
- **アクションホワイトリスト**: [`native_host/native_host.py`](native_host/native_host.py:259) の `ALLOWED_ACTIONS`（frozenset）。未知アクションは拒否。
- **コマンドインジェクション**: [`native_host/start_backend.py`](native_host/start_backend.py:354) の `subprocess.Popen([python_exe, str(APP)], ...)` は `shell=True` なし。`extension_id` は32文字英数字のみ許可（[`native_host/start_backend.py`](native_host/start_backend.py:228)）。
- **拡張ID検証**: [`native_host/native_host.py`](native_host/native_host.py:312) の `_validate_extension_id` が32文字小文字英数字 + マニフェストの `allowed_origins` と照合。プロセス引数のオリジン（`sys.argv[1]`）とも照合（[`native_host/native_host.py`](native_host/native_host.py:576)）。
- **プロセス祖先検証**: [`native_host/native_host.py`](native_host/native_host.py:514) の `_is_caller_authorized_browser` が Chrome/Edge の祖先を要求。PID再利用対策（作成時刻検証）付きで fail-closed。
- **レート制限**: 一般IPC（10回/秒）とトークンアクション（3回/30秒）のスライディングウィンドウ（[`native_host/native_host.py`](native_host/native_host.py:205)）。
- **秘密情報の扱い**: シャットダウントークンは Fernet 暗号化（`unprotect_data`）で復号。POSIX では所有者のみのパーミッション（0o600）へ自動修正（[`native_host/native_host.py`](native_host/native_host.py:828)）。使用済みマーカーで再発行を防止。
- **一時ファイル・パーミッション**: PID ファイルは `os.replace` による原子的書き込み（[`native_host/start_backend.py`](native_host/start_backend.py:356)）。インストーラは生成ファイルの ACL をハードニング（[`native_host/install_host_windows.ps1`](native_host/install_host_windows.ps1:44)）。
- **インストーラの安全対策**: LocalMachine スコープでユーザー書き込み可能ディレクトリへのインストールを拒否（[`native_host/install_host_windows.ps1`](native_host/install_host_windows.ps1:140)）。`-WhatIf` は副作用なし（テストで確認済み）。
- **バックエンド側の検証**: `/api/stocks/add_ext` はループバック + `X-MNS-Extension-Request` ヘッダ + Bearer トークン（定数時間比較）+ 信頼済み Origin の多層検証（[`routes/api_stocks.py`](routes/api_stocks.py:1215)）。`/api/shutdown` も同様（[`routes/api_system.py`](routes/api_system.py:777)）。

---

## 5. 総評

Chrome拡張・Native Host 領域は全体として堅牢に設計されており、Critical / High に該当する問題は検出されなかった。特に、拡張IDの多重検証（マニフェスト照合 + プロセス引数照合 + プロセス祖先検証）、メッセージサイズ制限、アクションホワイトリスト、レート制限、ループバック限定の通信先など、多層防御が適切に実装されている。

検出された問題は Medium 2件・Low 4件で、いずれも防御機構の不完全性（NH-1, NH-2）または運用上の軽微な実害（NH-3, NH-4, EXT-1, EXT-2）に留まる。優先的に対応すべきは NH-1（ログマスキングの不完全性）と NH-2（トークン発行の非対称性）である。
