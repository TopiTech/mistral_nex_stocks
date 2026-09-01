/**
 * 統一 API クライアント with SSE ハートビート監視
 * フロントエンドの fetch 呼び出しを単一化
 * SSE接続にハートビート監視と自動再接続を実装
 */

type JsonObject = Record<string, unknown>;
type Timer = ReturnType<typeof setTimeout>;
type Interval = ReturnType<typeof setInterval>;

type Logger = {
  debug: (...args: unknown[]) => void;
  info: (...args: unknown[]) => void;
  warn: (...args: unknown[]) => void;
  error: (...args: unknown[]) => void;
};

const _log: Logger = {
  debug: (...args) => console.debug("[APIClient]", ...args),
  info: (...args) => console.info("[APIClient]", ...args),
  warn: (...args) => console.warn("[APIClient]", ...args),
  error: (...args) => console.error("[APIClient]", ...args),
};

const DEFAULT_CONFIG = {
  timeout: 25000,
  sseHeartbeatTimeout: 45000,
  sseReconnectBaseDelay: 2000,
  sseReconnectMaxDelay: 30000,
  watchdogInterval: 10000,
  visibilityTimeout: 30000,
} as const;

type APIClientConfig = Partial<typeof DEFAULT_CONFIG>;
type RetryOptions = { maxRetries?: number };
type SSEOptions = {
  autoReconnect?: boolean;
  maxReconnectAttempts?: number;
  onReconnect?: (eventSource: EventSource) => void;
  /**
   * Resolve a fresh stream URL for every (re)connection.
   *
   * SSE tickets are short-lived and single-use, so a reconnect must mint a
   * brand-new ticket instead of reusing the consumed/expired one from the
   * previous connection. When set, this provider is invoked before each
   * connection attempt (including heartbeat-timeout, error, visibility-resume
   * and online-resume paths).
   */
  urlProvider?: () => string | null | Promise<string | null>;
};
type SSEMessageHandler = (data: unknown) => void;
type SSEErrorHandler = (error: Error) => void;
type SSEParams = {
  url: string;
  onMessage: SSEMessageHandler;
  onError: SSEErrorHandler;
  options: SSEOptions;
};

type APIResponse = JsonObject;

type WindowWithAPI = Window & {
  APIClient: typeof APIClient;
  APIError: typeof APIError;
};

class APIClient {
  baseURL: string;
  timeout: number;
  sseHeartbeatTimeout: number;
  sseReconnectBaseDelay: number;
  sseReconnectMaxDelay: number;
  sseReconnectAttempt: number;
  sseHeartbeatTimer: Timer | null;
  currentEventSource: EventSource | null;
  ssePendingReconnectTimeout: Timer | null;
  // Id of the most recent SSE event processed (used for Last-Event-ID resume:
  // the stream URL on the next (re)connect carries this so the server can
  // replay any events missed during the gap instead of resending a full
  // snapshot).
  lastEventId: number;
  private _reconnecting: boolean;
  lastCheckTime: number;
  watchdogInterval: number;
  watchdogTimer: Interval | null;
  isVisibilityPaused: boolean;
  private _lastSSEParams: SSEParams | null;
  private _visibilityTimeout: Timer | null;
  visibilityTimeout: number;
  private _visibilityHandler: (() => void) | null;
  private _onlineHandler: (() => void) | null;
  private _offlineHandler: (() => void) | null;

  constructor(baseURL = "/api", config: APIClientConfig = {}) {
    this.baseURL = baseURL;
    const mergedConfig = { ...DEFAULT_CONFIG, ...config };
    this.timeout = mergedConfig.timeout;
    this.sseHeartbeatTimeout = mergedConfig.sseHeartbeatTimeout;
    this.sseReconnectBaseDelay = mergedConfig.sseReconnectBaseDelay;
    this.sseReconnectMaxDelay = mergedConfig.sseReconnectMaxDelay;
    this.sseReconnectAttempt = 0;
    this.sseHeartbeatTimer = null;
    this.currentEventSource = null;
    this.ssePendingReconnectTimeout = null;
    this.lastEventId = 0;
    this._reconnecting = false;
    this.lastCheckTime = Date.now();
    this.watchdogInterval = mergedConfig.watchdogInterval;
    this.watchdogTimer = null;
    this.isVisibilityPaused = false;
    this._lastSSEParams = null;
    this._visibilityTimeout = null;
    this.visibilityTimeout = mergedConfig.visibilityTimeout;
    this._visibilityHandler = null;
    this._onlineHandler = null;
    this._offlineHandler = null;
    this._setupEventListeners();
  }

  private _setupEventListeners(): void {
    this._visibilityHandler = () => {
      if (document.hidden) {
        if (this.currentEventSource || this.ssePendingReconnectTimeout) {
          _log.info("Page hidden: Setting deferred pause timer for SSE");
          if (this._visibilityTimeout) clearTimeout(this._visibilityTimeout);
          this._visibilityTimeout = setTimeout(() => {
            if (document.hidden) {
              _log.info("Page still hidden: Pausing SSE to save resources");
              this.isVisibilityPaused = true;
              this._teardownSSE();
            }
          }, this.visibilityTimeout);
        }
      } else {
        if (this._visibilityTimeout) {
          clearTimeout(this._visibilityTimeout);
          this._visibilityTimeout = null;
        }
        if (this.isVisibilityPaused && this._lastSSEParams) {
          _log.info("Page visible: Resuming SSE connection...");
          this.isVisibilityPaused = false;
          this._resumeSSE();
        }
      }
    };
    document.addEventListener("visibilitychange", this._visibilityHandler);

    this._onlineHandler = () => {
      if (
        this._lastSSEParams &&
        !this.currentEventSource &&
        !this.isVisibilityPaused
      ) {
        _log.info("Network back online: Immediate SSE reconnection attempt");
        this._resumeSSE(false);
      }
    };
    window.addEventListener("online", this._onlineHandler);

    this._offlineHandler = () => {
      _log.warn("Network offline: SSE connection likely lost");
    };
    window.addEventListener("offline", this._offlineHandler);
  }

  private _startSleepWatchdog(): void {
    this._stopSleepWatchdog();
    this.lastCheckTime = Date.now();
    this.watchdogTimer = setInterval(() => {
      const now = Date.now();
      const diff = now - this.lastCheckTime;
      if (diff > this.watchdogInterval + 20000) {
        _log.warn(
          `Sleep recovery detected: CPU was frozen for ${Math.round(diff / 1000)}s. Resetting SSE.`,
        );
        if (this._lastSSEParams && !this.isVisibilityPaused) {
          this._resumeSSE(true);
        }
      }
      this.lastCheckTime = now;
    }, this.watchdogInterval);
  }

  private _stopSleepWatchdog(): void {
    if (this.watchdogTimer) {
      clearInterval(this.watchdogTimer);
      this.watchdogTimer = null;
    }
  }

  private _resumeSSE(force = false): void {
    if (!this._lastSSEParams) return;
    if (this.ssePendingReconnectTimeout) {
      clearTimeout(this.ssePendingReconnectTimeout);
      this.ssePendingReconnectTimeout = null;
    }
    this._reconnecting = false;
    if (force) this.sseReconnectAttempt = 0;
    this._openWithResolvedUrl(this._lastSSEParams);
  }

  /**
   * Open an SSE connection, resolving a fresh URL first.
   *
   * When ``options.urlProvider`` is present (e.g. to mint a new short-lived SSE
   * ticket via POST), it is invoked on every connection attempt so the stream
   * never reuses a consumed/expired ticket URL. Falls back to ``params.url``
   * when no provider is configured.
   */
  private _openWithResolvedUrl(params: SSEParams): void {
    const { onMessage, onError, options } = params;
    const urlOrPromise = options.urlProvider
      ? options.urlProvider()
      : Promise.resolve(params.url);
    Promise.resolve(urlOrPromise)
      .then((resolvedUrl: string | null) => {
        // Abort if closeSSE()/mode-switch happened while the URL was being
        // resolved (e.g. the ticket POST was still in flight): opening now
        // would create a stale EventSource that is never cleaned up.
        if (this._lastSSEParams !== params) return;
        if (!resolvedUrl) {
          _log.warn("SSE: Resolved stream URL is empty");
          onError(new Error("SSE: Resolved stream URL is empty"));
          this._handleReconnect(onError);
          return;
        }
        this.openSSE(resolvedUrl, onMessage, onError, options);
      })
      .catch((err: unknown) => {
        _log.warn("SSE: Failed to resolve stream URL", err);
        if (this._lastSSEParams !== params) return;
        onError(err instanceof Error ? err : new Error(String(err)));
        this._handleReconnect(onError);
      });
  }

  async request(
    url: string,
    options: RequestInit = {},
    maxRetries = 2,
  ): Promise<APIResponse> {
    const fullURL = url.startsWith("http") ? url : `${this.baseURL}${url}`;
    let lastError: APIError | null = null;
    const token = document
      .querySelector('meta[name="csrf-token"]')
      ?.getAttribute("content");
    const method = (options.method || "GET").toUpperCase();
    const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS", "TRACE"]);
    const callerSignal = options.signal;
    const throwIfCallerAborted = () => {
      if (callerSignal?.aborted) {
        throw new APIError(499, 1106, "リクエストがキャンセルされました");
      }
    };

    if (token && !SAFE_METHODS.has(method)) {
      const headers = new Headers(options.headers);
      if (!headers.has("X-CSRFToken") && !headers.has("X-CSRF-Token")) {
        headers.set("X-CSRFToken", token);
      }
      options = {
        ...options,
        headers,
        credentials: options.credentials ?? "same-origin",
      };
    }

    for (let attempt = 0; attempt <= maxRetries; attempt++) {
      throwIfCallerAborted();
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), this.timeout);
      const abortFromCaller = () => controller.abort();
      if (callerSignal) {
        if (callerSignal.aborted) {
          controller.abort();
        } else {
          callerSignal.addEventListener("abort", abortFromCaller, {
            once: true,
          });
        }
      }
      try {
        const response = await fetch(fullURL, {
          ...options,
          signal: controller.signal,
        });
        const reqId = response.headers.get("X-MNS-Request-Id") || "-";
        const rawText = await response.text();
        let data: JsonObject = {};
        if (rawText && rawText.trim()) {
          try {
            const parsed: unknown = JSON.parse(rawText);
            data = isJsonObject(parsed) ? parsed : {};
          } catch {
            throw new APIError(
              response.status,
              9999,
              response.ok
                ? "サーバー応答の解析に失敗しました"
                : `HTTP ${response.status}: ${rawText.slice(0, 200)}`,
              { raw: rawText.slice(0, 1000) },
              reqId,
            );
          }
        }

        if (!response.ok) {
          const errCode = toNumber(data.error_code, 9999);
          const errMsg = toStringValue(
            data.message ?? data.error,
            `HTTP ${response.status}`,
          );
          const errReason = toStringValue(
            (data.details as Record<string, unknown> | undefined)?.reason ?? "",
            "",
          );
          const isCsrfErr =
            (response.status === 400 || response.status === 403) &&
            (/csrf token/i.test(errMsg) || /csrf token/i.test(errReason));

          if (!SAFE_METHODS.has(method) && isCsrfErr && attempt === 0) {
            try {
              const freshRes = await fetch("/api/csrf-token", {
                method: "GET",
                credentials: "same-origin",
                headers: { "Cache-Control": "no-store" },
                signal: controller.signal,
              });
              if (freshRes.ok) {
                const freshData = (await freshRes.json()) as {
                  csrf_token?: string;
                };
                const freshToken =
                  typeof freshData?.csrf_token === "string"
                    ? freshData.csrf_token
                    : "";
                if (freshToken) {
                  const meta = document.querySelector(
                    'meta[name="csrf-token"]',
                  );
                  if (meta) meta.setAttribute("content", freshToken);
                  const updatedHeaders = new Headers(options.headers);
                  updatedHeaders.set("X-CSRFToken", freshToken);
                  options = {
                    ...options,
                    headers: updatedHeaders,
                    credentials: options.credentials ?? "same-origin",
                  };
                  throwIfCallerAborted();
                  continue;
                }
              }
            } catch (csrfErr) {
              if (csrfErr instanceof APIError) {
                throw csrfErr;
              }
              if (callerSignal?.aborted) {
                throw new APIError(
                  499,
                  1106,
                  "リクエストがキャンセルされました",
                );
              }
              // Ignore CSRF refresh error and proceed to standard error handling
            }
          }

          if (
            SAFE_METHODS.has(method) &&
            response.status >= 500 &&
            attempt < maxRetries
          ) {
            lastError = new APIError(
              response.status,
              errCode,
              errMsg,
              data.details,
              reqId,
            );
            await delay(
              Math.min(1000 * Math.pow(2, attempt), 5000),
              callerSignal,
            );
            throwIfCallerAborted();
            continue;
          }
          throw new APIError(
            response.status,
            errCode,
            errMsg,
            data.details,
            reqId,
          );
        }
        return data;
      } catch (error: unknown) {
        if (error instanceof APIError) throw error;
        const errorMessage = getErrorMessage(error);
        if (isAbortError(error)) {
          if (callerSignal?.aborted) {
            throw new APIError(499, 1106, "リクエストがキャンセルされました");
          }
          if (SAFE_METHODS.has(method) && attempt < maxRetries) {
            lastError = new APIError(
              408,
              1105,
              "リクエストがタイムアウトしました",
            );
            await delay(
              Math.min(1000 * Math.pow(2, attempt), 5000),
              callerSignal,
            );
            throwIfCallerAborted();
            continue;
          }
          throw new APIError(408, 1105, "リクエストがタイムアウトしました");
        }
        if (SAFE_METHODS.has(method) && attempt < maxRetries) {
          lastError = new APIError(0, 9999, errorMessage);
          await delay(
            Math.min(1000 * Math.pow(2, attempt), 5000),
            callerSignal,
          );
          throwIfCallerAborted();
          continue;
        }
        throw new APIError(0, 9999, errorMessage);
      } finally {
        clearTimeout(timeoutId);
        callerSignal?.removeEventListener("abort", abortFromCaller);
      }
    }
    throw lastError || new APIError(0, 9999, "リクエストに失敗しました");
  }

  async get(
    url: string,
    params: Record<string, string> = {},
    retryOptions: RetryOptions = {},
  ): Promise<APIResponse> {
    const queryString = new URLSearchParams(params).toString();
    const fullURL = queryString ? `${url}?${queryString}` : url;
    return this.request(
      fullURL,
      { method: "GET" },
      retryOptions.maxRetries ?? 2,
    );
  }

  async post(url: string, body: unknown = {}): Promise<APIResponse> {
    return this.request(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  async put(url: string, body: unknown = {}): Promise<APIResponse> {
    return this.request(url, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  async delete(url: string): Promise<APIResponse> {
    return this.request(url, { method: "DELETE" });
  }

  resetHeartbeat(onError?: SSEErrorHandler): void {
    const handler = onError || this._lastSSEParams?.onError;
    if (handler) {
      this._resetHeartbeatTimer(handler);
    }
  }

  private _resetHeartbeatTimer(onError: SSEErrorHandler): void {
    if (this.sseHeartbeatTimer) clearTimeout(this.sseHeartbeatTimer);
    this.sseHeartbeatTimer = setTimeout(() => {
      _log.warn("SSE: Heartbeat timeout. Reconnecting...");
      this._handleReconnect(onError);
    }, this.sseHeartbeatTimeout);
  }

  private _handleReconnect(onError: SSEErrorHandler): void {
    if (this._reconnecting) {
      _log.debug("SSE: Reconnect already in progress, skipping.");
      return;
    }
    this._reconnecting = true;
    try {
      this._teardownSSE();
      if (!this._lastSSEParams) {
        this._reconnecting = false;
        return;
      }
      const { options } = this._lastSSEParams;
      const autoReconnect = options.autoReconnect !== false;
      if (!autoReconnect) {
        onError(new Error("SSE: Auto-reconnect is disabled"));
        this._reconnecting = false;
        return;
      }

      this.sseReconnectAttempt++;
      const maxFastAttempts = options.maxReconnectAttempts || 7;
      let delayMs: number;
      if (this.sseReconnectAttempt <= maxFastAttempts) {
        const baseDelay =
          this.sseReconnectBaseDelay *
          Math.pow(2, Math.max(0, this.sseReconnectAttempt - 1));
        const jitter = 0.5 + Math.random() * 1.0;
        delayMs = Math.min(baseDelay * jitter, this.sseReconnectMaxDelay);
      } else {
        // Continuous slow reconnection policy: keep retrying at max delay (e.g. 30s)
        // Notify the caller exactly once when fast attempts are exhausted so the
        // fallback polling (api.js handleSseError) starts; without this the UI
        // would silently freeze on stale data while slow retries continue forever.
        if (this.sseReconnectAttempt === maxFastAttempts + 1) {
          try {
            onError(
              new Error(
                "SSE: Max fast reconnection attempts reached; continuing slow retries",
              ),
            );
          } catch (err) {
            _log.error(
              "SSE: onError callback failed during slow-reconnect transition",
              err,
            );
          }
        }
        const jitter = 0.8 + Math.random() * 0.4;
        delayMs = Math.min(this.sseReconnectMaxDelay * jitter, 60000);
      }

      _log.info(
        `SSE: Reconnect attempt ${this.sseReconnectAttempt} in ${Math.round(delayMs)}ms...`,
      );
      this.ssePendingReconnectTimeout = setTimeout(() => {
        this._reconnecting = false;
        if (!this._lastSSEParams) return;
        // Resolve a fresh URL (new SSE ticket) on every reconnect so a
        // consumed/expired ticket can never stall the stream.
        this._openWithResolvedUrl(this._lastSSEParams);
      }, delayMs);
    } catch (error: unknown) {
      _log.error("SSE: Error during reconnect", error);
      this._reconnecting = false;
    }
  }

  openSSE(
    url: string,
    onMessage: SSEMessageHandler,
    onError: SSEErrorHandler,
    options: SSEOptions = {},
  ): EventSource | null {
    if (!this.isVisibilityPaused) {
      this._lastSSEParams = { url, onMessage, onError, options };
    }
    this._teardownSSE();
    this._reconnecting = false;
    const fullURL = url.startsWith("http") ? url : `${this.baseURL}${url}`;
    try {
      const eventSource = new EventSource(fullURL);
      this.currentEventSource = eventSource;

      // Wrap addEventListener and removeEventListener so any custom SSE event
      // (realtime_update, pts_update, etc.) automatically resets the heartbeat
      // timer to prevent false timeout disconnects, while supporting clean detachment.
      const origAddEventListener =
        eventSource.addEventListener.bind(eventSource);
      const origRemoveEventListener =
        eventSource.removeEventListener.bind(eventSource);
      const listenerMap = new WeakMap<object, (event: Event) => void>();

      (eventSource as any).addEventListener = (
        type: string,
        listener: any,
        eventListenerOptions?: boolean | AddEventListenerOptions,
      ) => {
        if (type !== "error" && type !== "open" && listener) {
          const wrappedListener = (event: Event) => {
            if (event && (event as MessageEvent).lastEventId) {
              this.lastEventId =
                Number((event as MessageEvent).lastEventId) || 0;
            }
            this._resetHeartbeatTimer(onError);
            if (typeof listener === "function") {
              listener.call(eventSource, event);
            } else if (listener && typeof listener.handleEvent === "function") {
              listener.handleEvent(event);
            }
          };
          if (typeof listener === "object" || typeof listener === "function") {
            listenerMap.set(listener, wrappedListener);
          }
          return origAddEventListener(
            type as any,
            wrappedListener as any,
            eventListenerOptions,
          );
        }
        return origAddEventListener(
          type as any,
          listener,
          eventListenerOptions,
        );
      };

      (eventSource as any).removeEventListener = (
        type: string,
        listener: any,
        eventListenerOptions?: boolean | EventListenerOptions,
      ) => {
        const target = (listener && listenerMap.get(listener)) || listener;
        return origRemoveEventListener(
          type as any,
          target,
          eventListenerOptions,
        );
      };

      this._startSleepWatchdog();
      eventSource.onopen = () => {
        _log.info("SSE: Connection established");
        this.sseReconnectAttempt = 0;
        this._resetHeartbeatTimer(onError);
      };
      eventSource.onmessage = (event: MessageEvent<string>) => {
        if (event && event.lastEventId) {
          this.lastEventId = Number(event.lastEventId) || 0;
        }
        this._resetHeartbeatTimer(onError);
        try {
          const data: unknown = JSON.parse(event.data);
          onMessage(data);
        } catch (error: unknown) {
          _log.error("SSE: Data parse error", error);
        }
      };
      eventSource.addEventListener("heartbeat", (event: Event) => {
        if (event && (event as MessageEvent).lastEventId) {
          this.lastEventId = Number((event as MessageEvent).lastEventId) || 0;
        }
        this._resetHeartbeatTimer(onError);
        _log.debug("SSE: Heartbeat received");
      });
      eventSource.onerror = (error: Event) => {
        _log.error("SSE: Stream error", error);
        this._handleReconnect(onError);
      };
      options.onReconnect?.(eventSource);
      return eventSource;
    } catch (error: unknown) {
      _log.error("SSE: Failed to open", error);
      this._handleReconnect(onError);
      return null;
    }
  }

  closeSSE(): void {
    this._lastSSEParams = null;
    this.isVisibilityPaused = false;
    this._stopSleepWatchdog();
    this._teardownSSE();
  }

  destroy(): void {
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

  private _teardownSSE(): void {
    this._stopSleepWatchdog();
    if (this._visibilityTimeout) {
      clearTimeout(this._visibilityTimeout);
      this._visibilityTimeout = null;
    }
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

class APIError extends Error {
  status: number;
  errorCode: number;
  details: unknown;
  requestId: string;

  constructor(
    status: number,
    errorCode: number,
    message: string,
    details: unknown = {},
    requestId = "-",
  ) {
    super(message);
    this.status = status;
    this.errorCode = errorCode;
    this.message = message;
    this.details = details;
    this.requestId = requestId;
    this.name = "APIError";
  }

  toJSON(): JsonObject {
    return {
      status: this.status,
      error_code: this.errorCode,
      message: this.message,
      details: this.details,
      request_id: this.requestId,
    };
  }
}

function isJsonObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function toNumber(value: unknown, fallback: number): number {
  return typeof value === "number" ? value : fallback;
}

function toStringValue(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}

function getErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function delay(ms: number, signal?: AbortSignal | null): Promise<void> {
  return new Promise((resolve) => {
    let timer: Timer | null = null;
    const cleanup = () => {
      if (timer !== null) clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
    };
    const onAbort = () => {
      cleanup();
      resolve();
    };
    timer = setTimeout(() => {
      cleanup();
      resolve();
    }, ms);
    if (signal) {
      signal.addEventListener("abort", onAbort, { once: true });
      if (signal.aborted) onAbort();
    }
  });
}

(window as unknown as WindowWithAPI).APIClient = APIClient;
(window as unknown as WindowWithAPI).APIError = APIError;
