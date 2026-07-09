// #region Security Utilities

// --- DOM Cache Helper ---
const DOM = {
  get(id) {
    // Directly retrieve element without caching to avoid stale references
    return document.getElementById(id);
  },
};

// --- Security Utilities ---
// sanitizeHTML removed due to potential XSS vulnerability if misused. DOM API (textContent) is preferred.

/**
 * シンボル入力を検証
 * @param {string} symbol - 検証対象のシンボル
 * @returns {Object} {valid: boolean, value?: string, error?: string}
 */
function validateSymbol(symbol) {
  if (!symbol || typeof symbol !== "string") {
    return { valid: false, error: "シンボルを入力してください" };
  }

  const trimmed = symbol.trim().toUpperCase();

  if (trimmed.length < 1 || trimmed.length > 15) {
    return { valid: false, error: "シンボルは1-15文字である必要があります" };
  }

  if (!/^[A-Z0-9^][A-Z0-9._\-^=]{0,14}$/.test(trimmed)) {
    return { valid: false, error: "無効なシンボル形式です" };
  }

  // 安全でない文字のチェック
  if (/[/\\\0]|\.\./.test(trimmed)) {
    return { valid: false, error: "安全でない文字が含まれています" };
  }

  return { valid: true, value: trimmed };
}

/**
 * 数値入力を検証
 * @param {string|number} value - 検証対象の値
 * @param {number} min - 最小値
 * @param {number} max - 最大値
 * @returns {Object} {valid: boolean, value?: number, error?: string}
 */
function validateNumberInput(value, min, max) {
  const num = parseFloat(value);

  if (isNaN(num)) {
    return { valid: false, error: "有効な数値を入力してください" };
  }

  if (num < min || num > max) {
    return { valid: false, error: `${min}から${max}の間で入力してください` };
  }

  return { valid: true, value: num };
}

/**
 * テキスト入力をサニタイズ
 * @param {string} text - サニタイズ対象のテキスト
 * @param {number} maxLength - 最大長
 * @returns {string} サニタイズされたテキスト
 */
function sanitizeTextInput(text, maxLength = 1000) {
  if (!text || typeof text !== "string") {
    return "";
  }

  let sanitized = text.trim();

  // 長さ制限
  if (sanitized.length > maxLength) {
    sanitized = sanitized.substring(0, maxLength);
  }

  // 制御文字の削除（改行とタブは許可）
  sanitized = sanitized.replace(/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/g, "");

  return sanitized;
}

function isValidHexColor(value) {
  return typeof value === "string" && /^#[0-9A-Fa-f]{6}$/.test(value.trim());
}

function sanitizeHexColor(value, fallback = "#6bb6ff") {
  return isValidHexColor(value) ? value.trim() : fallback;
}

function clearLegacyApiKeyStorage() {
  ["MISTRAL_API_KEY", "LANGSEARCH_API_KEY", "TAVILY_API_KEY"].forEach((key) => {
    try {
      sessionStorage.removeItem(key);
    } catch {
      // Ignore storage access failures in restricted browser contexts.
    }
    try {
      localStorage.removeItem(key);
    } catch {
      // Ignore storage access failures in restricted browser contexts.
    }
  });
}

const toFiniteNumber = (value, fallback = 0) => {
  const num = Number(value);
  return Number.isFinite(num) ? num : fallback;
};

function parseRequiredNonNegativeNumber(value, label) {
  const raw = String(value ?? "").trim();
  if (!raw) return { ok: false, error: `${label}を入力してください` };
  const num = Number(raw);
  if (!Number.isFinite(num))
    return { ok: false, error: `${label}は数値で入力してください` };
  if (num < 0) return { ok: false, error: `${label}は0以上で入力してください` };
  return { ok: true, value: num };
}

function parseOptionalNonNegativeNumber(value, label) {
  const raw = String(value ?? "").trim();
  if (!raw) return { ok: true, value: null };
  const num = Number(raw);
  if (!Number.isFinite(num))
    return { ok: false, error: `${label}は数値で入力してください` };
  if (num < 0) return { ok: false, error: `${label}は0以上で入力してください` };
  return { ok: true, value: num };
}

/* --- Button Loading Helpers (Prevent XSS) --- */
function setButtonLoading(btn, loadingText) {
  if (!btn) return;
  if (!btn.dataset.originalText) {
    btn.dataset.originalText = btn.textContent || "";
  }
  btn.disabled = true;
  btn.textContent = "";
  const spinner = document.createElement("span");
  spinner.className = "loading-spinner";
  btn.appendChild(spinner);
  const textNode = document.createTextNode(loadingText);
  btn.appendChild(textNode);
}

function resetButton(btn) {
  if (!btn) return;
  btn.disabled = false;
  btn.textContent = btn.dataset.originalText || "";
}

/* --- Modal Helpers --- */
function openModal(modalId, onOpenCallback) {
  const modal = DOM.get(modalId);
  if (!modal) return;
  modal.classList.add("show");
  modal.style.display = "flex";
  if (typeof onOpenCallback === "function") {
    onOpenCallback(modal);
  }
}

function closeModal(modalId) {
  const modal = DOM.get(modalId);
  if (!modal) return;
  modal.classList.remove("show");
  modal.style.display = "none";
}

// #endregion Security Utilities

// #region Logger
// --- Logger ---
class Logger {
  constructor(module) {
    this.module = module;
    this.enabled = localStorage.getItem("debugEnabled") === "true";
    // 機密情報を検出するパターン
    this.sensitivePatterns = [
      /api[_-]?key['"]?\s*[:=]\s*['"]?[^\s'"]+/gi,
      /token['"]?\s*[:=]\s*['"]?[^\s'"]+/gi,
      /password['"]?\s*[:=]\s*['"]?[^\s'"]+/gi,
      /authorization['"]?\s*[:=]\s*['"]?[^\s'"]+/gi,
      /secret['"]?\s*[:=]\s*['"]?[^\s'"]+/gi,
    ];
  }

  /**
   * 機密情報をマスクしてログ出力
   */
  _sanitize(args) {
    return args.map((arg) => {
      if (typeof arg === "string") {
        let sanitized = arg;
        this.sensitivePatterns.forEach((pattern) => {
          sanitized = sanitized.replace(pattern, "[REDACTED]");
        });
        return sanitized;
      }
      if (typeof arg === "object" && arg !== null) {
        let targetObj = arg;
        if (arg instanceof Error) {
          targetObj = {
            name: arg.name,
            message: arg.message,
            stack: arg.stack,
          };
        }
        const str = JSON.stringify(targetObj);
        let sanitized = str;
        this.sensitivePatterns.forEach((pattern) => {
          sanitized = sanitized.replace(pattern, "[REDACTED]");
        });
        try {
          return JSON.parse(sanitized);
        } catch {
          return sanitized;
        }
      }
      return arg;
    });
  }

  debug(...args) {
    if (this.enabled)
      console.debug(`[${this.module}]`, ...this._sanitize(args));
  }

  info(...args) {
    console.info(`[${this.module}]`, ...this._sanitize(args));
  }

  warn(...args) {
    console.warn(`[${this.module}]`, ...this._sanitize(args));
  }

  error(...args) {
    console.error(`[${this.module}]`, ...this._sanitize(args));
  }
}

const logger = new Logger("Frontend");

// #endregion Logger

// #region Shared UI Utilities

/**
 * Display a toast notification.
 * Creates the toast container if it doesn't exist.
 * Auto-dismisses after 5 seconds with fade-out animation.
 *
 * @param {string} message - Notification text
 * @param {string} [color="#fff"] - Accent color for the toast border/text
 */
const _toastHistory = new Map();

function showToast(message, color = "#fff") {
  const now = Date.now();
  if (_toastHistory.has(message)) {
    const lastTime = _toastHistory.get(message);
    if (now - lastTime < 3000) {
      return;
    }
  }
  _toastHistory.set(message, now);
  if (_toastHistory.size > 50) {
    for (const [msg, ts] of _toastHistory.entries()) {
      if (now - ts > 10000) {
        _toastHistory.delete(msg);
      }
    }
  }

  const containerId = "toast-container";
  let container = document.getElementById(containerId);
  if (!container) {
    container = document.createElement("div");
    container.id = containerId;
    container.style.cssText =
      "position:fixed;bottom:20px;right:20px;z-index:9999;display:flex;flex-direction:column;gap:10px;";
    document.body.appendChild(container);
  }
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.style.setProperty("--toast-accent", color);
  toast.textContent = message;
  container.appendChild(toast);

  requestAnimationFrame(() => {
    toast.classList.add("show");
  });

  setTimeout(() => {
    if (!toast.isConnected) return;
    toast.classList.remove("show");
    toast.classList.add("hide");
    const onTransitionEnd = () => {
      toast.removeEventListener("transitionend", onTransitionEnd);
      if (toast.isConnected) toast.remove();
    };
    toast.addEventListener("transitionend", onTransitionEnd);
    setTimeout(() => {
      toast.removeEventListener("transitionend", onTransitionEnd);
      if (toast.isConnected) toast.remove();
    }, 350);
  }, 5000);
}

/**
 * Pure HTMLエスケープ。textContent に安全に設定可能な文字列を返す。
 * 改行変換は行わない。innerHTML には使用しないこと。
 * @param {*} text - エスケープ対象
 * @returns {string}
 */
function escapeHtml(text) {
  if (text === null || text === undefined) return "";
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

/**
 * localStorageからソート順を取得（全ページ共通）
 * @param {string} market - 市場識別子
 * @returns {string[]}
 */
function getSortOrder(market) {
  try {
    const parsed = JSON.parse(localStorage.getItem(`sort_${market}`) || "[]");
    return Array.isArray(parsed)
      ? parsed.filter((s) => typeof s === "string")
      : [];
  } catch {
    return [];
  }
}

/**
 * ブラウザに保存されているレガシーAPI認証情報を消去する
 * @param {{mistral?: boolean, langsearch?: boolean, tavily?: boolean}} options - 削除するキーの種類
 */
function clearLegacyBrowserCredentials(options = {}) {
  const mistral = options.mistral !== false;
  const langsearch = options.langsearch !== false;
  const tavily = options.tavily !== false;
  if (mistral || langsearch || tavily) {
    clearLegacyApiKeyStorage();
  }
}

// #endregion Shared UI Utilities
