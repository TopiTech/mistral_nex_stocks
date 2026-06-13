/**
 * 統一 API クライアント with SSE ハートビート監視
 * フロントエンドの fetch 呼び出しを単一化
 * SSE接続にハートビート監視と自動再接続を実装
 *
 * 使用例:
 *  const api = new APIClient();
 *  const stocks = await api.get('/api/stocks');
 *  const sse = api.openSSE('/stocks/stream', onMessage, onError);
 */

class APIClient {
  constructor(baseURL = "/api") {
    this.baseURL = baseURL;
    this.timeout = 25000; // 個人利用向けに最適化: 25秒

    // SSE ハートビート監視設定
    this.sseHeartbeatTimeout = 60000; // 個人利用向けに最適化: 60秒（サーバー30秒ハートビートに大幅な余裕を持たせる）
    this.sseReconnectBaseDelay = 2000; // 指数バックオフの基本遅延（2秒）
    this.sseReconnectMaxDelay = 30000; // 最大待機時間（30秒）
    this.sseReconnectAttempt = 0; // 再接続試行回数
    this.sseHeartbeatTimer = null;
    this.currentEventSource = null;
    this.ssePendingReconnectTimeout = null; // 再接続スケジュール用タイマー

    // スリープ検知ロジック (Watchdog)
    this.lastCheckTime = Date.now();
    this.watchdogInterval = 10000; // 10秒ごとにチェック
    this.watchdogTimer = null;

    // Page Visibility / Network 状態管理
    this.isVisibilityPaused = false;
    this._lastSSEParams = null;
    this._visibilityTimeout = null;

    // 進行中のリクエストを追跡（重複防止）
    this.pendingRequests = new Map();

    // イベントハンドラーの参照を保持（削除用）
    this._visibilityHandler = null;
    this._onlineHandler = null;
    this._offlineHandler = null;

    this._setupEventListeners();
  }

  /**
   * 各種イベントリスナー（Visibility, Online/Offline, Sleep）を一括設定
   */
  _setupEventListeners() {
    // Page Visibility (タブ切り替え・最小化)
    this._visibilityHandler = () => {
      if (document.hidden) {
        if (this.currentEventSource || this.ssePendingReconnectTimeout) {
          console.info("Page hidden: Setting deferred pause timer for SSE");
          if (this._visibilityTimeout) clearTimeout(this._visibilityTimeout);
          this._visibilityTimeout = setTimeout(() => {
            if (document.hidden) {
              console.info("Page still hidden: Pausing SSE to save resources");
              this.isVisibilityPaused = true;
              this._closeSSEInternal();
            }
          }, 30000); // 30秒間非表示なら切断
        }
      } else {
        if (this._visibilityTimeout) {
          clearTimeout(this._visibilityTimeout);
          this._visibilityTimeout = null;
        }
        if (this.isVisibilityPaused && this._lastSSEParams) {
          console.info("Page visible: Resuming SSE connection...");
          this.isVisibilityPaused = false;
          this._resumeSSE();
        }
      }
    };

    document.addEventListener("visibilitychange", this._visibilityHandler);

    // ネットワーク復帰 (オフラインからの回復)
    this._onlineHandler = () => {
      if (this._lastSSEParams && !this.currentEventSource && !this.isVisibilityPaused) {
        console.info("Network back online: Immediate SSE reconnection attempt");
        this._resumeSSE(true); // forceReconnect = true
      }
    };
    window.addEventListener("online", this._onlineHandler);

    // ネットワーク切断 (ログのみ)
    this._offlineHandler = () => {
      console.warn("Network offline: SSE connection likely lost");
    };
    window.addEventListener("offline", this._offlineHandler);
  }

  /**
   * スリープ監視を開始 (JavaScriptの実行が停止＝スリープを検知)
   */
  _startSleepWatchdog() {
    this._stopSleepWatchdog();
    this.lastCheckTime = Date.now();
    this.watchdogTimer = setInterval(() => {
      const now = Date.now();
      const diff = now - this.lastCheckTime;
      // 10秒のインターバルに対して 30秒以上経っていたらスリープ復帰とみなす（緩和）
      if (diff > this.watchdogInterval + 20000) {
        console.warn(
          `Sleep recovery detected: CPU was frozen for ${Math.round(diff / 1000)}s. Resetting SSE.`,
        );
        if (this._lastSSEParams && !this.isVisibilityPaused) {
          this._resumeSSE(true);
        }
      }
      this.lastCheckTime = now;
    }, this.watchdogInterval);
  }

  _stopSleepWatchdog() {
    if (this.watchdogTimer) {
      clearInterval(this.watchdogTimer);
      this.watchdogTimer = null;
    }
  }

  /**
   * 保存されたパラメータでSSEを再開
   * @param {boolean} force - 試行回数をリセットして即時再開するか
   */
  _resumeSSE(force = false) {
    if (!this._lastSSEParams) return;

    // Always clear any pending reconnect timeout to prevent multiple concurrent reconnects
    if (this.ssePendingReconnectTimeout) {
      clearTimeout(this.ssePendingReconnectTimeout);
      this.ssePendingReconnectTimeout = null;
    }

    if (force) {
      this.sseReconnectAttempt = 0;
    }

    this.openSSE(
      this._lastSSEParams.url,
      this._lastSSEParams.onMessage,
      this._lastSSEParams.onError,
      this._lastSSEParams.options,
    );
  }

  /**
   * タイムアウト付きのリクエスト送信（リトライ機構付き）
   * @param {string} url - エンドポイント
   * @param {Object} options - fetchオプション
   * @param {number} maxRetries - 最大リトライ回数（デフォルト2）
   * @returns {Promise<Object>} 解析されたJSONデータ
   * @throws {APIError} APIエラー発生時
   */
  async request(url, options = {}, maxRetries = 2) {
    const fullURL = url.startsWith("http") ? url : `${this.baseURL}${url}`;
    let lastError = null;

    for (let attempt = 0; attempt <= maxRetries; attempt++) {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), this.timeout);

      try {
        const response = await fetch(fullURL, {
          ...options,
          signal: controller.signal,
        });

        clearTimeout(timeoutId);

        const rawText = await response.text();
        let data = {};
        if (rawText && rawText.trim()) {
          try {
            data = JSON.parse(rawText);
          } catch {
            if (!response.ok) {
              throw new APIError(
                response.status,
                9999,
                `HTTP ${response.status}: ${rawText.slice(0, 200)}`,
                { raw: rawText.slice(0, 1000) },
              );
            }
            throw new APIError(response.status, 9999, "サーバー応答の解析に失敗しました", {
              raw: rawText.slice(0, 1000),
            });
          }
        }

        if (!response.ok) {
          // 5xxエラーまたはネットワークエラーの場合のみリトライ
          if (response.status >= 500 && attempt < maxRetries) {
            lastError = new APIError(
              response.status,
              data.error_code ?? 9999,
              data.message ?? data.error ?? `HTTP ${response.status}`,
              data.details,
            );
            const delay = Math.min(1000 * Math.pow(2, attempt), 5000);
            await new Promise((r) => setTimeout(r, delay));
            continue;
          }
          throw new APIError(
            response.status,
            data.error_code ?? 9999,
            data.message ?? data.error ?? `HTTP ${response.status}`,
            data.details,
          );
        }

        return data;
      } catch (error) {
        clearTimeout(timeoutId);
        if (error instanceof APIError) throw error;
        if (error.name === "AbortError") {
          // タイムアウト時もリトライ
          if (attempt < maxRetries) {
            lastError = new APIError(408, 1105, "リクエストがタイムアウトしました");
            const delay = Math.min(1000 * Math.pow(2, attempt), 5000);
            await new Promise((r) => setTimeout(r, delay));
            continue;
          }
          throw new APIError(408, 1105, "リクエストがタイムアウトしました");
        }
        // その他のネットワークエラー
        if (attempt < maxRetries) {
          lastError = new APIError(0, 9999, error.message);
          const delay = Math.min(1000 * Math.pow(2, attempt), 5000);
          await new Promise((r) => setTimeout(r, delay));
          continue;
        }
        throw new APIError(0, 9999, error.message);
      }
    }

    throw lastError || new APIError(0, 9999, "リクエストに失敗しました");
  }

  /**
   * GETリクエストの送信（自動リトライ付き）
   * @param {string} url - エンドポイント
   * @param {Object} params - クエリパラメータ
   * @param {Object} retryOptions - リトライオプション
   * @returns {Promise<Object>}
   */
  async get(url, params = {}, retryOptions = {}) {
    const queryString = new URLSearchParams(params).toString();
    const fullURL = queryString ? `${url}?${queryString}` : url;
    const maxRetries = retryOptions.maxRetries ?? 2;
    return this.request(fullURL, { method: "GET" }, maxRetries);
  }

  /**
   * POSTリクエストの送信
   * @param {string} url - エンドポイント
   * @param {Object} body - リクエストボディ
   * @returns {Promise<Object>}
   */
  async post(url, body = {}) {
    return this.request(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  async put(url, body = {}) {
    return this.request(url, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  async delete(url) {
    return this.request(url, { method: "DELETE" });
  }

  /**
   * ハートビートタイマーをリセット
   */
  _resetHeartbeatTimer(onError) {
    if (this.sseHeartbeatTimer) clearTimeout(this.sseHeartbeatTimer);

    this.sseHeartbeatTimer = setTimeout(() => {
      console.warn("SSE: Heartbeat timeout. Reconnecting...");
      this._handleReconnect(onError);
    }, this.sseHeartbeatTimeout);
  }

  /**
   * 指定した遅延とジッターで再接続をスケジュール
   */
  _handleReconnect(onError) {
    this._closeSSEInternal(); // 現在のコネクションを掃除

    if (!this._lastSSEParams) return;
    const { options } = this._lastSSEParams;
    const autoReconnect = options.autoReconnect !== false;
    const maxAttempts = options.maxReconnectAttempts || 7; // 個人利用向けに最適化

    if (autoReconnect && this.sseReconnectAttempt < maxAttempts) {
      this.sseReconnectAttempt++;

      // 指数バックオフ + ジッター (0.8 ~ 1.2倍の揺らぎ)
      const baseDelay =
        this.sseReconnectBaseDelay * Math.pow(2, Math.max(0, this.sseReconnectAttempt - 1));
      const jitter = 0.8 + Math.random() * 0.4;
      const delay = Math.min(baseDelay * jitter, this.sseReconnectMaxDelay);

      console.info(
        `SSE: Reconnect attempt ${this.sseReconnectAttempt}/${maxAttempts} in ${Math.round(delay)}ms...`,
      );

      this.ssePendingReconnectTimeout = setTimeout(() => {
        this.openSSE(
          this._lastSSEParams.url,
          this._lastSSEParams.onMessage,
          this._lastSSEParams.onError,
          this._lastSSEParams.options,
        );
      }, delay);
    } else if (onError) {
      const msg = !autoReconnect
        ? "SSE: Auto-reconnect is disabled"
        : "SSE: Max reconnection attempts reached";
      onError(new Error(msg));
    }
  }

  /**
   * SSE (Server-Sent Events) を開く（ハートビート監視付き）
   * @param {string} url - ストリームエンドポイント
   * @param {Function} onMessage - メッセージ受信時のコールバック
   * @param {Function} onError - エラー発生時のコールバック
   * @param {Object} options - 再接続やフックのオプション
   * @returns {EventSource|null}
   */
  openSSE(url, onMessage, onError, options = {}) {
    // 明示的な呼び出しの場合のみパラメータを保存
    if (!this.isVisibilityPaused) {
      this._lastSSEParams = { url, onMessage, onError, options };
    }

    // 重複接続の防止
    this._closeSSEInternal();

    const fullURL = url.startsWith("http") ? url : `${this.baseURL}${url}`;

    try {
      const eventSource = new EventSource(fullURL);
      this.currentEventSource = eventSource;
      this._startSleepWatchdog();

      eventSource.onopen = () => {
        console.info("SSE: Connection established");
        this.sseReconnectAttempt = 0;
        this._resetHeartbeatTimer(onError);
      };

      eventSource.onmessage = (event) => {
        this._resetHeartbeatTimer(onError);
        try {
          const data = JSON.parse(event.data);
          if (onMessage) onMessage(data);
        } catch (error) {
          console.error("SSE: Data parse error", error);
        }
      };

      eventSource.addEventListener("heartbeat", () => {
        this._resetHeartbeatTimer(onError);
        console.debug("SSE: Heartbeat received");
      });

      eventSource.onerror = (error) => {
        console.error("SSE: Stream error", error);
        this._handleReconnect(onError);
      };

      // イベントハンドラー登録後に onReconnect コールバックを実行
      if (options.onReconnect) options.onReconnect(eventSource);

      return eventSource;
    } catch (error) {
      console.error("SSE: Failed to open", error);
      this._handleReconnect(onError);
      return null;
    }
  }

  /**
   * SSE を完全に閉じる
   */
  closeSSE() {
    this._lastSSEParams = null;
    this.isVisibilityPaused = false;
    this._stopSleepWatchdog();
    this._closeSSEInternal();
  }

  /**
   * インスタンスを破棄し、全イベントリスナーを削除
   */
  destroy() {
    this.closeSSE();
    if (this._visibilityHandler) {
      document.removeEventListener("visibilitychange", this._visibilityHandler);
      this._visibilityHandler = null;
    }
    if (this._onlineHandler) {
      window.removeEventListener("online", this._onlineHandler);
      this._onlineHandler = null;
    }
    if (this._offlineHandler) {
      window.removeEventListener("offline", this._offlineHandler);
      this._offlineHandler = null;
    }
  }

  /**
   * SSEを内部的にクリーンアップ (再接続前や Visibility 用)
   */
  _closeSSEInternal() {
    this._stopSleepWatchdog();
    if (this.sseHeartbeatTimer) {
      clearTimeout(this.sseHeartbeatTimer);
      this.sseHeartbeatTimer = null;
    }
    if (this.ssePendingReconnectTimeout) {
      clearTimeout(this.ssePendingReconnectTimeout);
      this.ssePendingReconnectTimeout = null;
    }
    if (this.currentEventSource) {
      this.currentEventSource.close();
      this.currentEventSource = null;
    }
  }
}

/**
 * API固有のエラークラス
 */
class APIError extends Error {
  constructor(status, errorCode, message, details = {}) {
    super(message);
    this.status = status;
    this.errorCode = errorCode;
    this.message = message;
    this.details = details;
    this.name = "APIError";
  }

  toJSON() {
    return {
      status: this.status,
      error_code: this.errorCode,
      message: this.message,
      details: this.details,
    };
  }
}

// グローバルインスタンス（任意）
window.apiClient = new APIClient();
