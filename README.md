# Mistral NeX Stocks - Complete Fixed Package v3

この版では、これまでの修正をまとめて再パッケージしています。

## 実行要件
- Python 3.9 以上（zoneinfo / スレッド終了処理の互換性のため）

## Shutdown API について
- `/api/shutdown` は `POST` のみ対応です。
- ローカルリクエストのみ受け付けます。
- `Origin` / `Referer` がある場合は、許可済みオリジン（localhost または登録済み拡張機能）からのみ受け付けます。
- リクエストボディに `{ "confirm": true }` が必要です。

## この版に入っている修正
- popup.js の JavaScript 構文エラー修正
- install_host_windows.ps1 の `.Count` エラー修正
- Windows Native Host manifest を安全に JSON 生成
- CSS の適用崩れ修正
  - `settings.css` の `model-badge` 未定義を修正
  - `setup.css` のモバイル表示・入力フォーカス・ボタン表示を改善
  - `index.css` に軽い UI 正規化とモバイル時の崩れ対策を追加
- favicon.ico を追加
- HTML の CSS/JS 参照にキャッシュバスターを更新

## コードレビューに基づく改善
- **Mistral API JSONモードの有効化**: `repair_news_json_with_llm`と`repair_analysis_json_with_llm`関数で`response_format={"type": "json_object"}`を使用し、JSON出力の信頼性を向上
- **暗号化機能の強化**: keyringライブラリを使用したクロスプラットフォーム対応のAPIキー暗号化を追加（優先順位: keyring > DPAPI > plain）
- **依存関係の改善**: yfinanceのバージョン範囲を`>=0.2.51,<0.3`に固定し、予期せぬ変更を防止
- **セキュリティ向上**: APIキーの取り扱いを改善し、ログ出力をフィンガープリントのみに制限

## 最新のコードレビューとリファクタリング（2024年）
### 実装した改善点

#### 高優先度（セキュリティとバグ修正）
1. **APIキー検証の強化**（app.py）
   - Mistral APIキーの最小長チェック（32文字以上）を追加
   - 不正なAPIキーの早期検知

2. **HTTPステータスコードの型安全な処理**（app.py）
   - `getattr(res, 'status_code', None)`を使用し、Noneの場合に対応
   - より堅牢なエラーハンドリング

3. **Strict-Transport-Securityヘッダーの追加**（app.py）
   - HSTSヘッダーを追加し、HTTPSの強制を有効化
   - `max-age=31536000; includeSubDomains`

#### 中優先度（API利用効率の向上）
1. **DDGSタイムアウト値の環境変数化**（app.py）
   - `DDGS_TIMEOUT`環境変数で制御可能に
   - 環境ごとの最適化が可能
2. **LangSearchリトライ条件の明確化**（app.py）
   - 5xxエラーから503のみに限定
   - 不必要なリトライを削減

#### 低優先度（コードの保守性と柔軟性の向上）
1. **Chrome拡張機能のバッジメッセージ定数化**（background.js）
   - `DEFAULT_BADGE_COLOR`と`DEFAULT_BADGE_DURATION`定数を定義
   - コードの保守性向上
2. **ネイティブホストのメッセージサイズ環境変数化**（native_host.py）
   - `NATIVE_HOST_MAX_MESSAGE_BYTES`環境変数で制御可能に
   - 柔軟な設定
3. **trend_sources.pyのクエリマップ定数クラス化**（trend_sources.py）
   - `QueryTemplates`クラスでクエリテンプレートを管理
   - コードの保守性と型安全性の向上

### 参照したWebページ
- Mistral API: https://docs.mistral.ai/api/
- Flask Security: https://flask.palletsprojects.com/en/2.3.x/security/
- DuckDuckGo Search: https://github.com/deedy5/ddg-search
- LangSearch API: https://api.langsearch.com
- Chrome Extension Native Messaging: https://developer.chrome.com/docs/extensions/mv3/nativeMessaging
- yfinance: https://github.com/ranaroussi/yfinance
- Feedparser: https://github.com/kurtmckee/feedparser
- Keyring: https://github.com/jaraco/keyring
- Tenacity: https://github.com/jd/tenacity
- Cachetools: https://github.com/tkem/cachetools
- Pandas: https://pandas.pydata.org/
- Requests: https://requests.readthedocs.io/

## 重要
以前の `chrome_extension/` を読み込んでいる場合は、**必ず削除してこの版の `chrome_extension/` を読み込み直してください。**
Web アプリ側も CSS / JS のキャッシュが残る場合があるので、ブラウザのハードリロード推奨です。

また、Native Host の `path` を絶対パス生成に変更しているため、既に登録済みの場合は一度 `install_host_windows.ps1` を再実行して再登録してください。

## Windows 登録例
Chrome:
```powershell
cd .\native_host
powershell -ExecutionPolicy Bypass -File .\install_host_windows.ps1 -ExtensionIds <CHROME_EXTENSION_ID> -Browser Chrome
```

Edge:
```powershell
powershell -ExecutionPolicy Bypass -File .\install_host_windows.ps1 -ExtensionIds <EDGE_EXTENSION_ID> -Browser Edge
```

Chrome + Edge:
```powershell
powershell -ExecutionPolicy Bypass -File .\install_host_windows.ps1 -ExtensionIds <CHROME_EXTENSION_ID>,<EDGE_EXTENSION_ID> -Browser Both
```

再登録（上書き）例:
```powershell
powershell -ExecutionPolicy Bypass -File .\install_host_windows.ps1 -ExtensionIds <CHROME_EXTENSION_ID> -Browser Chrome -Force
```

## トラブルシューティング
- **バックエンドが起動しない**: Python環境を確認。`python app.py` で直接起動可能かテスト。
- **拡張機能が動作しない**: Chrome拡張機能マネージャーでリロード。ネイティブホストが正しく登録されているか確認。
- **株価データが更新されない**: 市場閉場時は更新間隔が長くなる（SSE: 10秒、フェッチ: 5分）。開場時に復帰。
- **APIエラー**: Mistral APIキーが正しいか確認。ネットワーク接続をチェック。
- **パフォーマンスが遅い**: 市場開場時にSSEが毎秒更新されるため、閉場時は自動で間隔が長くなる。
