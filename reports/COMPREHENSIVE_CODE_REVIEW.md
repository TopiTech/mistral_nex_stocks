# 包括的コードレビュー: Mistral NeX Stocks v3.0.0

> レビュー日: 2026-06-23
> 対象: 全プロジェクトファイル

---

## 総評

Flaskベースの個人向け株式ダッシュボードとして非常に高品質な実装。セキュリティ意識が高く、SSEによるリアルタイム配信、AI連携、ポートフォリオ管理、トレンド分析まで広範な機能をカバーしている。コードの大部分は堅牢でよく整理されているが、いくつかの改善点と潜在的な問題が確認された。特にCSPポリシーの強化とデータ書き込みのアトミック性保証は優先的に対処すべき。

---

## 1. 🔴 重大: すぐに対処すべき問題

### 1.1 CSP `'unsafe-inline'` が style-src に含まれている

**ファイル**: `app.py` (CSP_DEFAULT_POLICY)

```python
CSP_DEFAULT_POLICY = os.environ.get(
    "CSP_DEFAULT_POLICY",
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    ...
)
```

**問題**: `'unsafe-inline'` が `style-src` に含まれているため、CSPが有効でもインラインスタイルが許可されてしまう。XSS対策として、すべてのスタイルは非CSPソースから読み込むか、nonceを使用すべき。

**推奨**: `'unsafe-inline'` を削除し、全スタイルを外部CSSファイルで管理する。どうしても必要な場合はnonce属性を使用する。

### 1.2 ユーザーストック保存の書き込み競合

**ファイル**: `app_helpers.py` (`save_user_stocks`)

```python
with app_state.user_stocks_lock:
    data = {
        "us": copy.deepcopy(app_state.user_us),
        "jp": copy.deepcopy(app_state.user_jp),
        "idx": copy.deepcopy(app_state.user_idx),
    }
# ⚠️ ロック解放後
encoded = json.dumps(data, ensure_ascii=False, indent=2)
protected = protect_data(encoded, key_name="user_stocks")
# この間に他スレッドが user_us/user_jp/user_idx を変更しても無視される
with open(tmp_file, "w", encoding="utf-8") as f:
    json.dump(protected, f, ensure_ascii=False, indent=2)
os.replace(tmp_file, USER_STOCKS_FILE)
```

**問題**: ロックをデータコピー後に解放しているため、ファイル書き込み中に別スレッドがストックデータを変更すると、変更が失われる。

**推奨**: データコピーからファイル書き込み完了までを同一ロック内で行う。または楽観ロック（更新タイムスタンプの比較）を導入する。

### 1.3 DPAPI復号失敗時のフォールバックがない

**ファイル**: `config_utils.py` (`_dpapi_unprotect`)

```python
except OSError:
    logger.debug("DPAPI unprotect failed; data may be corrupted or encrypted by another user")
    return b""
```

**問題**: DPAPI復号に失敗すると空バイト列を返すが、呼び出し元は「設定が空」なのか「復号に失敗した」のか区別できない。結果として、暗号化された資格情報があるにもかかわらず「未設定」と誤認識される可能性がある。

**推奨**: 型で区別する（`None` vs `""`）か、専用の例外を投げて呼び出し元で適切にハンドリングする。

---

## 2. 🟠 中程度: 早急な対応が推奨される問題

### 2.1 テストカバレッジの穴

**ファイル**: `tests/`

テストは充実しているが、以下の領域が未カバー:

| テスト対象 | テストファイル | 現状 |
|-----------|--------------|------|
| SSE配信 `_build_sse_light_stocks_payload` | なし | 未テスト |
| `build_stock_payload` の異常データ入力 | `test_core_logic.py` | 一部のみ |
| CLI/スタートアップスクリプト | なし | 未テスト |
| `NewsFormatter` 全メソッド | なし | 未テスト |
| 市場休場時（週末）の動作 | なし | 未テスト |
| `extract_batch_history` の異常系 | なし | 未テスト |

**推奨**: 上記の領域にテストを追加する。

### 2.2 `app.py` の単一ファイル肥大化

**ファイル**: `app.py` (~600行)

FlaskアプリケーションとしてFactoryパターン（`create_app()`）を使用しておらず、以下の関心事が全て `app.py` で定義されている:
- ロギング設定
- CSP/Talismanセキュリティ設定
- グローバルエラーハンドラ
- バックグラウンドスレッド起動
- before_request/after_requestフック
- ブループリント登録
- ウォームアップ処理

**推奨**: `create_app()` Factoryパターンに移行し、テスト時に設定の注入ができるようにする。

### 2.3 レート制限のグローバル辞書がメモリリークの可能性

**ファイル**: `route_helpers.py` (`_rate_limit_store`, `_rate_limit_window_by_key`)

```python
_rate_limit_store: Dict[str, List[float]] = {}
_rate_limit_window_by_key: Dict[str, int] = {}
```

**問題**: これらはプロセス生存中に無限にエントリが蓄積される可能性がある。`_cleanup_rate_limit_store` が60秒ごとに呼ばれるが、大量のエンドポイント・IPアドレスの組み合わせがある環境ではメモリ使用量が増加する。

**推奨**: 定期的なクリーンアップ間隔をもっと短くする（10秒程度）か、TTLキャッシュ（cachetools）で管理する。

### 2.4 `bg_interpolate_loop` の市場状態チェックが高頻度

**ファイル**: `app_bg.py`

```python
us_market_open = is_market_open("us")
jp_market_open = is_market_open("jp")
idx_market_open = is_market_open("idx")
```

**問題**: ループのたび（0.5秒ごとまたは10秒ごと）に市場状態をチェックしている。`get_cached` で5秒キャッシュされているとはいえ、無駄にAPIを呼ぶ可能性がある。

**推奨**: 市場状態の更新は30秒〜60秒間隔で十分。

### 2.5 `__getattr__` による間接アクセスパターン

**ファイル**: `app_state.py`

```python
def __getattr__(self, name):
    for group_name in ("execution", "market", "ai", "cache"):
        group = getattr(self, group_name, None)
        if group is not None and hasattr(group, name):
            return getattr(group, name)
    raise AttributeError(...)
```

**問題**: ホットパス（`app_state.yfinance_lock`, `app_state.is_syncing` など）で毎回ループ+`hasattr`+`getattr`が呼ばれる。これは`__getattr__`が属性が存在しないときだけ呼ばれるはずだが、`__init__`で明示的に属性を設定していないものはすべてこのパスを通る。Pythonの`__getattr__`は通常の属性解決の後で呼ばれるため、`__init__`で明示的に設定されていない属性だけがこのパスを通るが、現状は多くの属性が動的に解決されている。

**推奨**: パフォーマンスクリティカルな属性は親クラスの`__dict__`に直接設定する。

---

## 3. 🟡 軽微: 注意すべき点

### 3.1 `BASE_DIR` の重複定義

**ファイル**: `constants.py` と `app_helpers.py` の両方で定義

```python
# constants.py
BASE_DIR = Path(__file__).resolve().parent
# app_helpers.py
BASE_DIR = Path(__file__).resolve().parent
```

**影響**: 互いに異なる可能性は低いが、DRY原則違反。`constants` の `BASE_DIR` をインポートして使用すべき。

### 3.2 `DOM._cache` が実質的に未使用

**ファイル**: `static/js/utils.js`

```javascript
const DOM = {
  _cache: new Map(),
  get(id) {
    const el = document.getElementById(id);
    this._cache.set(id, el);
    return el;
  },
};
```

**問題**: `_cache` に格納しているが、そこから読み取るコードがない。メモリリークにはならないが、無駄なMap。

**推奨**: キャッシュ機構を削除するか、実際にキャッシュとして使用する。

### 3.3 チャット履歴のクロスストック汚染

**ファイル**: `routes/api_analysis.py`

```python
with app_state.chat_history_lock:
    if chat_key in app_state.chat_history:
        app_state.chat_history.move_to_end(chat_key)
    ...
    if len(app_state.chat_history) > max_history:
        app_state.chat_history.popitem(last=False)
```

**問題**: `max_history=50` は全銘柄共有。ある銘柄の会話が50メッセージを超えると、最も古い他の銘柄の会話が削除される。

**推奨**: チャット履歴をキー単位で独立させるか、グローバルな上限を大幅に引き上げる。

### 3.4 フロントエンド: `findAllWrappersByStockKey` の重複問題

**ファイル**: `static/js/state.js`

**問題**: `wrapperRegistryMap` は `Set` を使用しているが、`createStockCard` で毎回 `registerWrapper` が呼ばれるため、同じstockKeyに対して複数のラッパーが登録される可能性がある。これはポートフォリオタブでstock cardが複製される際に発生する。

**推奨**: `renderPortfolio()` などで重複をチェックする。

### 3.5 `split_js.py` の存在意義が不明

`split_js.py` が存在するが、どこからも呼ばれていないように見える。もしCDN/ビルド用のスクリプトならREADME等で説明が必要。

---

## 4. 🟢 良い実装パターン（維持すべき）

### 4.1 セキュリティ: 入力検証の徹底

**ファイル**: `app_helpers.py` (`is_valid_symbol`)

Unicode正規化（NFKC）+ パターンマッチ + 危険文字チェックの3段階防御は模範的。

### 4.2 SSE接続管理の堅牢性

**ファイル**: `static/js/api_client.js` (`APIClient`)

- ハートビート監視（60秒タイムアウト）
- 指数バックオフ + ジッター
- Page Visibility API連携
- スリープ復帰検知（Watchdog）
- ネットワーク復帰時の自動再接続

個人利用アプリとして非常に堅牢なSSEクライアント。

### 4.3 機密情報のログマスク（二重防御）

フロントエンド（`utils.js: Logger._sanitize`）とバックエンド（`app_helpers.py: _sanitize_error_message`）の両方でログ出力前の機密情報マスクを実装。

### 4.4 シーキューラブレーカーパターン

**ファイル**: `app_state.py` (`MarketDataState`)

Mistral API, LangSearch, yfinance の3サービスに対してサーキットブレーカーを実装。`CLOSED` → `OPEN` → `HALF_OPEN` の状態遷移を正しく実装している。

### 4.5 アトミックファイル書き込み + 一時ファイル

`os.replace(tmp_file, target_file)` パターンは全所で一貫して使用されており、ファイル破損リスクを低減している。

### 4.6 負荷分散: キャッシュスタンペード防止

**ファイル**: `app_helpers.py` (`get_cached`)

`fetch_events` ディクショナリで同一キーの同時フェッチを防止。最初のスレッドだけがフェッチを行い、他のスレッドはEventで待機してからキャッシュを読む。

---

## 5. 📊 ファイル別品質評価

| ファイル | LOC | 品質 | コメント |
|---------|-----|------|---------|
| `app.py` | ~600 | ⚠️ 良いが大きい | Factoryパターン推奨 |
| `app_bg.py` | ~350 | ✅ 良好 | 補間ロジックは堅牢 |
| `app_helpers.py` | ~700 | ✅ 良好 | 多くの責務を抱えている |
| `app_state.py` | ~500 | ✅ 良好 | グループ化は良い試み |
| `config_utils.py` | ~400 | ⚠️ DPAPI周り注意 | フォールバックなし |
| `route_helpers.py` | ~200 | ⚠️ グローバル状態 | メモリ管理に注意 |
| `services/search_service.py` | ~500 | ✅ 良好 | リトライ・フォールバック充実 |
| `static/js/api_client.js` | ~300 | ✅ 秀逸 | SSE管理が非常に堅牢 |
| `static/js/ui.js` | ~800 | ⚠️ 大きい | リファクタリング候補 |
| `static/js/state.js` | ~300 | ✅ 良好 | 中央集権的管理 |
| `static/js/utils.js` | ~250 | ✅ 良好 | 安全性高い |
| `static/css/index.css` | ~1600 | ⚠️ 非常に大きい | CSS分割推奨 |
| `tests/test_core_logic.py` | ~300 | ✅ 良好 | モックの使い方が良い |

---

## 6. 🏗️ アーキテクチャ上の考察

### 6.1 長所
- **段階的フェイルオーバー**: LangSearch → DDGS, yfinance batch → single → cache など
- **キャッシュの多層化**: TTLCache + LRUCache + ネガティブキャッシュ
- **循環インポートの回避**: `route_helpers.py` で分割
- **コンポーネント指向フロントエンド**: DOM APIベース（innerHTML不使用）

### 6.2 改善余地
- バックエンドの循環インポートの問題が完全には解決されていない（`app.py` → `app_helpers` → `app_state` → ... の依存関係が複雑）
- 状態管理が `app_state` の1巨大オブジェクトに集中しており、単一責任原則に反する
- フロントエンドのUIレンダリング（`ui.js: ~800行`）は分割して単一責任にする価値がある

---

## 7. ✅ 最終推奨アクション（優先度順）

| # | アクション | 優先度 | 対象ファイル |
|---|-----------|--------|-------------|
| 1 | CSPから `'unsafe-inline'` を削除する | 🔴 P0 | `app.py` |
| 2 | ユーザーストック保存をロック内で完結させる | 🔴 P0 | `app_helpers.py` |
| 3 | DPAPI復号失敗時のフォールバックを実装 | 🟠 P1 | `config_utils.py` |
| 4 | テストカバレッジを拡充（SSE, NewsFormatter） | 🟠 P1 | `tests/` |
| 5 | `__getattr__` のホットパス属性を明示的アクセスに | 🟠 P1 | `app_state.py` |
| 6 | レート制限辞書をTTLキャッシュ化 | 🟠 P1 | `route_helpers.py` |
| 7 | `create_app()` Factoryパターンに移行 | 🟠 P1 | `app.py` |
| 8 | `BASE_DIR` の重複を解消 | 🟡 P2 | `constants.py`, `app_helpers.py` |
| 9 | `DOM._cache` を削除 | 🟡 P2 | `static/js/utils.js` |
| 10 | チャット履歴のグローバル上限を見直し | 🟡 P2 | `routes/api_analysis.py` |
