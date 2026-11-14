/**
 * SSE クライアント with ハートビート監視
 * REST 呼び出しは apiFetch（api.js）と csrfFetch（utils.js）が所有する
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
  sseHeartbeatTimeout: 45000,
  sseReconnectBaseDelay: 2000,
  sseReconnectMaxDelay: 30000,
  watchdogInterval: 10000,
  visibilityTimeout: 30000,
} as const;

type APIClientConfig = Partial<typeof DEFAULT_CONFIG>;
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

type WindowWithAPI = Window & {
  APIClient: typeof APIClient;
  APIError: typeof APIError;
};

class APIClient {
  baseURL: string;
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
  isConnecting: boolean;
  private _lastSSEParams: SSEParams | null;
  private _visibilityTimeout: Timer | null;
  visibilityTimeout: number;
  private _visibilityHandler: (() => void) | null;
  private _onlineHandler: (() => void) | null;
  private _offlineHandler: (() => void) | null;
  /** Token of the newest in-flight _openWithResolvedUrl chain (L11, 2026-11-07). */
  private _openChainToken: object | null = null;

  constructor(baseURL = "/api", config: APIClientConfig = {}) {
    this.baseURL = baseURL;
    const mergedConfig = { ...DEFAULT_CONFIG, ...config };
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
    this.isConnecting = false;
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
        this._armVisibilityPause();
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
        !this.isVisibilityPaused &&
        // (L11, 2026-11-07 review) never re-enter while a URL resolution is
        // still pending: _resumeSSE would mint a second ticket POST and the
        // stale chain's finally would clear isConnecting early — together
        // they could permanently bypass the visibility pause.
        !this.isConnecting
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

  /**
   * Arm (or re-arm) the deferred visibility pause.
   *
   * The timer must survive reconnect churn: ``_teardownSSE()`` clears it, and a
   * transport error while the tab is hidden would otherwise cancel the pause
   * permanently (``visibilitychange`` does not fire again until the tab becomes
   * visible), leaving a live EventSource running in a background tab — the very
   * resource use the deferred pause exists to prevent.
   */
  private _armVisibilityPause(): void {
    if (this.isVisibilityPaused) return;
    if (typeof document === "undefined" || !document.hidden) return;
    if (
      !this.currentEventSource &&
      !this.ssePendingReconnectTimeout &&
      !this.isConnecting
    ) {
      return;
    }
    _log.info("Page hidden: Setting deferred pause timer for SSE");
    if (this._visibilityTimeout) clearTimeout(this._visibilityTimeout);
    this._visibilityTimeout = setTimeout(() => {
      this._visibilityTimeout = null;
      if (document.hidden) {
        _log.info("Page still hidden: Pausing SSE to save resources");
        this.isVisibilityPaused = true;
        this._teardownSSE();
      }
    }, this.visibilityTimeout);
  }

  private _resumeSSE(force = false): void {
    if (!this._lastSSEParams) return;
    this._teardownSSE();
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
    this.isConnecting = true;
    // (L11, 2026-11-07 review) serialize concurrent connect chains: only the
    // LATEST chain owns the isConnecting flag — a stale chain finishing while
    // a newer chain is still resolving used to clear the flag early and let
    // a hidden Session open an EventSource that the visibility pause would
    // never tear down.
    const chainToken = {};
    this._openChainToken = chainToken;
    // A hidden tab that starts (or retries) a connection must still pause: the
    // resolved-URL callback below defers opening while ``isVisibilityPaused``.
    this._armVisibilityPause();
    let urlOrPromise: string | Promise<string | null> | null;
    try {
      urlOrPromise = options.urlProvider ? options.urlProvider() : params.url;
    } catch (err) {
      urlOrPromise = Promise.reject(err);
    }
    Promise.resolve(urlOrPromise)
      .then((resolvedUrl: string | null) => {
        // Abort if closeSSE()/mode-switch happened while the URL was being
        // resolved (e.g. the ticket POST was still in flight): opening now
        // would create a stale EventSource that is never cleaned up.
        if (
          this._lastSSEParams !== params ||
          this._openChainToken !== chainToken
        ) {
          return;
        }
        if (this.isVisibilityPaused) {
          _log.info(
            "SSE: URL resolved while visibility paused, deferring connection",
          );
          return;
        }
        if (!resolvedUrl) {
          _log.warn("SSE: Resolved stream URL is empty");
          try {
            onError(new Error("SSE: Resolved stream URL is empty"));
          } catch (err) {
            _log.error("SSE: onError callback threw an exception", err);
          }
          this._handleReconnect(onError);
          return;
        }
        this.openSSE(resolvedUrl, onMessage, onError, options);
      })
      .catch((err: unknown) => {
        _log.warn("SSE: Failed to resolve stream URL", err);
        if (
          this._lastSSEParams !== params ||
          this._openChainToken !== chainToken
        ) {
          return;
        }
        if (this.isVisibilityPaused) {
          _log.info(
            "SSE: URL resolution rejected while visibility paused, deferring reconnect",
          );
          return;
        }
        try {
          onError(err instanceof Error ? err : new Error(String(err)));
        } catch (cbErr) {
          _log.error("SSE: onError callback threw an exception", cbErr);
        }
        this._handleReconnect(onError);
      })
      .finally(() => {
        if (this._openChainToken === chainToken) {
          this.isConnecting = false;
          this._openChainToken = null;
        }
      });
  }

  resolveURL(url: string): string {
    if (url.startsWith("http://") || url.startsWith("https://")) {
      return url;
    }
    const cleanBase = this.baseURL.replace(/\/+$/, "");
    if (cleanBase && (url === cleanBase || url.startsWith(`${cleanBase}/`))) {
      return url;
    }
    const cleanUrl = url.replace(/^\/+/, "");
    return cleanBase ? `${cleanBase}/${cleanUrl}` : `/${cleanUrl}`;
  }

  resetHeartbeat(onError?: SSEErrorHandler): void {
    const handler = onError || this._lastSSEParams?.onError;
    if (handler) {
      this._resetHeartbeatTimer(handler);
    }
  }

  /**
   * Schedule a reconnect through the same exponential-backoff/jitter policy
   * used for transport failures.
   *
   * ``attempt`` is the 1-based resync cycle number. Passing it lets a caller
   * continue a backoff sequence across connections even though ``onopen``
   * resets the client's ordinary reconnect counter.
   */
  reconnectWithBackoff(onError?: SSEErrorHandler, attempt?: number): void {
    const handler = onError || this._lastSSEParams?.onError;
    if (!handler) return;
    if (
      typeof attempt === "number" &&
      Number.isFinite(attempt) &&
      attempt > 0
    ) {
      this.sseReconnectAttempt = Math.floor(attempt) - 1;
    }
    this._handleReconnect(handler);
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
        try {
          onError(new Error("SSE: Auto-reconnect is disabled"));
        } catch (cbErr) {
          _log.error("SSE: onError callback threw an exception", cbErr);
        }
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
      // _teardownSSE() above cleared the pending-reconnect timer *and* the
      // _reconnecting flag that guards re-entrant calls. Re-assert the flag
      // here, where the replacement timer is installed, so the guard keeps
      // meaning "a reconnect is already scheduled": without it a second
      // trigger during the wait (up to 60s under the slow-retry policy) would
      // tear the pending timer down and restart the backoff from scratch.
      // The timer callback and every early exit below clear it again.
      this._reconnecting = true;
      this.ssePendingReconnectTimeout = setTimeout(() => {
        this._reconnecting = false;
        if (!this._lastSSEParams) return;
        // Resolve a fresh URL (new SSE ticket) on every reconnect so a
        // consumed/expired ticket can never stall the stream.
        this._openWithResolvedUrl(this._lastSSEParams);
      }, delayMs);
      // _teardownSSE() above cleared any pending pause; re-arm it so a
      // reconnect while the tab stays hidden still ends in a pause.
      this._armVisibilityPause();
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
    const initialParams: SSEParams = { url, onMessage, onError, options };
    if (!this.isVisibilityPaused) {
      this._lastSSEParams = initialParams;
    }
    this._teardownSSE();
    this._reconnecting = false;
    // The very first connection may not have a URL yet: the stream URL is
    // minted asynchronously (a short-lived, single-use SSE ticket), and that
    // request can fail (rate limit, backend restart, expired session). Route
    // the initial open through the same resolve -> open -> onError/reconnect
    // path used for reconnects, so an initial ticket failure starts fallback
    // polling and retries with backoff instead of leaving the page marked as
    // streaming while no EventSource exists. Previously only reconnects used
    // this path; api.js awaited the ticket itself and silently returned on
    // failure, freezing the UI until a manual reload or mode switch.
    if (!url && options.urlProvider) {
      // An explicit connect request supersedes a deferred visibility pause:
      // keep it as the params _resumeSSE reopens so the requested mode wins.
      this._lastSSEParams = initialParams;
      this._openWithResolvedUrl(initialParams);
      return null;
    }
    const fullURL = this.resolveURL(url);
    try {
      const eventSource = new EventSource(fullURL);
      this.currentEventSource = eventSource;
      this._armVisibilityPause();

      // Wrap addEventListener and removeEventListener so any custom SSE event
      // (realtime_update, pts_update, etc.) automatically resets the heartbeat
      // timer to prevent false timeout disconnects, while supporting clean detachment.
      const origAddEventListener = (
        eventSource.addEventListener as (
          type: string,
          listener: unknown,
          options?: boolean | AddEventListenerOptions,
        ) => void
      ).bind(eventSource);
      const origRemoveEventListener = (
        eventSource.removeEventListener as (
          type: string,
          listener: unknown,
          options?: boolean | EventListenerOptions,
        ) => void
      ).bind(eventSource);
      const listenerMap = new WeakMap<
        object,
        Map<string, Array<EventListener>>
      >();

      type EventSourceListener =
        | ((this: EventSource, event: MessageEvent) => void)
        | EventListenerOrEventListenerObject;

      interface CustomEventSource {
        addEventListener: (
          type: string,
          listener: EventSourceListener | null,
          options?: boolean | AddEventListenerOptions,
        ) => void;
        removeEventListener: (
          type: string,
          listener: EventSourceListener | null,
          options?: boolean | EventListenerOptions,
        ) => void;
      }

      const customEventSource = eventSource as unknown as CustomEventSource;

      customEventSource.addEventListener = (
        type: string,
        listener: EventSourceListener | null,
        eventListenerOptions?: boolean | AddEventListenerOptions,
      ): void => {
        if (type !== "error" && type !== "open" && listener) {
          const wrappedListener: EventListener = (event: Event) => {
            if (event && (event as MessageEvent).lastEventId) {
              this.lastEventId =
                Number((event as MessageEvent).lastEventId) || 0;
            }
            this._resetHeartbeatTimer(onError);
            try {
              if (typeof listener === "function") {
                listener.call(eventSource, event as MessageEvent);
              } else if (typeof listener.handleEvent === "function") {
                listener.handleEvent(event);
              }
            } catch (err: unknown) {
              _log.error(
                "Unhandled error in EventSource custom listener:",
                err,
              );
            }
          };
          if (typeof listener === "object" || typeof listener === "function") {
            let typeMap = listenerMap.get(listener);
            if (!typeMap) {
              typeMap = new Map();
              listenerMap.set(listener, typeMap);
            }
            let wrappers = typeMap.get(type);
            if (!wrappers) {
              wrappers = [];
              typeMap.set(type, wrappers);
            }
            wrappers.push(wrappedListener);
          }
          return origAddEventListener(
            type,
            wrappedListener,
            eventListenerOptions,
          );
        }
        if (listener) {
          return origAddEventListener(type, listener, eventListenerOptions);
        }
      };

      customEventSource.removeEventListener = (
        type: string,
        listener: EventSourceListener | null,
        eventListenerOptions?: boolean | EventListenerOptions,
      ): void => {
        let target: EventSourceListener | EventListener | null = listener;
        if (
          listener &&
          (typeof listener === "object" || typeof listener === "function")
        ) {
          const typeMap = listenerMap.get(listener);
          const wrappers = typeMap?.get(type);
          if (wrappers && wrappers.length > 0) {
            target = wrappers.pop()!;
            if (wrappers.length === 0 && typeMap) {
              typeMap.delete(type);
              if (typeMap.size === 0) {
                listenerMap.delete(listener);
              }
            }
          }
        }
        if (target) {
          return origRemoveEventListener(type, target, eventListenerOptions);
        }
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
      // (L7, 2026-11-07 review) the server emits fully-formed `event: error`
      // frames (e.g. "too many SSE connections") that the browser delivers as
      // MessageEvents on the same channel as transport errors; per WHATWG
      // they reach addEventListener("error") too. Without this listener the
      // payload — including the server's reason — was silently dropped.
      eventSource.addEventListener("error", (event: Event) => {
        const maybeMessage = event as Partial<MessageEvent>;
        if (typeof maybeMessage.data !== "string" || !maybeMessage.data) return;
        try {
          const payload = JSON.parse(maybeMessage.data) as Record<
            string,
            unknown
          >;
          const message =
            typeof payload.message === "string"
              ? payload.message
              : "サーバーからSSEエラーが通知されました";
          _log.warn("SSE: Server error frame:", message);
          try {
            onError(new Error(message));
          } catch (cbErr) {
            _log.error("SSE: onError callback threw an exception", cbErr);
          }
        } catch (parseErr) {
          _log.debug("SSE: Non-JSON server error frame ignored", parseErr);
        }
      });
      options.onReconnect?.(eventSource);
      return eventSource;
    } catch (error: unknown) {
      _log.error("SSE: Failed to open", error);
      this._handleReconnect(onError);
      return null;
    }
  }

  /**
   * Return whether an SSE connection is open, connecting, pending reconnect,
   * or visibility-paused (meaning visibilitychange/resume already owns recovery).
   */
  hasActiveSSE(): boolean {
    const src = this.currentEventSource;
    const readyState = src ? src.readyState : -1;
    const openState = typeof EventSource !== "undefined" ? EventSource.OPEN : 1;
    const connectingState =
      typeof EventSource !== "undefined" ? EventSource.CONNECTING : 0;
    return (
      readyState === openState ||
      readyState === connectingState ||
      this.isConnecting ||
      this.ssePendingReconnectTimeout !== null ||
      this._reconnecting ||
      Boolean(this.isVisibilityPaused && this._lastSSEParams)
    );
  }

  closeSSE(): void {
    this._lastSSEParams = null;
    this.isVisibilityPaused = false;
    this.sseReconnectAttempt = 0;
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
    this._reconnecting = false;
    this.isConnecting = false;
    this._openChainToken = null;
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

(window as unknown as WindowWithAPI).APIClient = APIClient;
(window as unknown as WindowWithAPI).APIError = APIError;
