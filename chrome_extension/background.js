// --- Security Utilities ---
/**
 * ルートパスをサニタイズ
 * @param {string} route - サニタイズ対象のルートパス
 * @returns {string} サニタイズされたルートパス
 */
function sanitizeRoute(route) {
  if (!route || typeof route !== 'string') {
    return '/';
  }

  // 先頭のスラッシュを確保
  let sanitized = route.trim();
  if (!sanitized.startsWith('/')) {
    sanitized = '/' + sanitized;
  }

  // パストラバーサル攻撃の防止
  sanitized = sanitized.replace(/\.\./g, '/');

  // 連続スラッシュを正規化
  sanitized = sanitized.replace(/\/+/g, '/');

  // 危険な文字を削除
  sanitized = sanitized.replace(/[<>"'|]/g, '');

  return sanitized;
}

const HOST_NAME = 'com.mistral_nex_stocks.host';
const BACKEND_URLS = ['http://127.0.0.1:5000', 'http://localhost:5000'];

async function checkHealth() {
  for (const base of BACKEND_URLS) {
    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 3000); // 個人利用向けに最適化: 3秒タイムアウト
      try {
        const res = await fetch(`${base}/api/health`, { 
          method: 'GET', 
          cache: 'no-store',
          signal: controller.signal 
        });
        clearTimeout(timeoutId);
        if (!res.ok) continue;
        const data = await res.json();
        return { ok: true, base, data };
      } finally {
        clearTimeout(timeoutId);
      }
    } catch (e) { console.debug('checkHealth attempt failed:', e); }
  }
  return { ok: false };
}

function detectBrowserName() {
  const ua = navigator.userAgent || '';
  if (ua.includes('Edg/')) return 'Microsoft Edge';
  if (ua.includes('Chrome/')) return 'Google Chrome';
  return 'Chromium Browser';
}

function sendNativeMessage(message) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendNativeMessage(HOST_NAME, message, (response) => {
      const err = chrome.runtime.lastError;
      if (err) {
        console.error('Native messaging error:', err);
        reject(new Error(err.message || 'Error when communicating with the native messaging host'));
      } else {
        resolve(response || { ok: false, error: 'No response' });
      }
    });
  });
}

async function openRoute(route) {
  const health = await checkHealth();
  const base = health.ok ? health.base : BACKEND_URLS[0];
  const sanitizedRoute = sanitizeRoute(route);
  await chrome.tabs.create({ url: `${base}${sanitizedRoute}` });
  return { ok: true, base, route: sanitizedRoute };
}

const DEFAULT_BADGE_COLOR = '#4d8fff';
const DEFAULT_BADGE_DURATION = 2500;

function setBadgeMessage(text, color = DEFAULT_BADGE_COLOR, durationMs = DEFAULT_BADGE_DURATION) {
  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color });
  setTimeout(() => {
    chrome.action.setBadgeText({ text: '' });
  }, durationMs);
}

// ------------------------------------------------------------------
// Context Menus
// ------------------------------------------------------------------
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: "add-us-stock",
      title: "Mistral NeX: 米国株に追加 '%s'",
      contexts: ["selection"]
    }, () => {
      if (chrome.runtime.lastError) {
        console.error('Failed to create context menu add-us-stock:', chrome.runtime.lastError.message);
      }
    });
    chrome.contextMenus.create({
      id: "add-jp-stock",
      title: "Mistral NeX: 日本株に追加 '%s'",
      contexts: ["selection"]
    }, () => {
      if (chrome.runtime.lastError) {
        console.error('Failed to create context menu add-jp-stock:', chrome.runtime.lastError.message);
      }
    });
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const symbol = (info.selectionText || "").trim();
  if (!symbol) return;

  const market = info.menuItemId === "add-jp-stock" ? "jp" : "us";
  const health = await checkHealth();
  if (!health.ok) {
     console.error("Backend not running. Cannot add stock.");
     setBadgeMessage('NG', '#ff7d7d');
     return;
  }

  try {
    const res = await fetch(`${health.base}/api/stocks/add_ext`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol, market })
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data?.ok === false) {
      console.error('Add stock API failed:', data?.error || `HTTP ${res.status}`);
      setBadgeMessage('NG', '#ff7d7d');
      return;
    }
    setBadgeMessage('OK', '#7dffb0');
  } catch (e) {
    console.error("Add stock failed:", e);
    setBadgeMessage('NG', '#ff7d7d');
  }
});

// ------------------------------------------------------------------
// Badge Updates (Nikkei 225)
// ------------------------------------------------------------------
async function updateBadge() {
  const health = await checkHealth();
  if (!health.ok) {
    chrome.action.setBadgeText({ text: "" });
    return;
  }

  try {
    const res = await fetch(`${health.base}/api/indices`, { method: 'GET', cache: 'no-store' });
    if (!res.ok) return;
    const data = await res.json();
    const n225 = data["N225"];
    if (n225 && n225.percent !== null && n225.percent !== undefined) {
      const pctValue = parseFloat(n225.percent);
      if (!Number.isNaN(pctValue)) {
        const text = (pctValue >= 0 ? "+" : "") + Math.round(pctValue).toString() + "%";
        const color = pctValue >= 0 ? "#7dffb0" : "#ff7d7d";
        
        chrome.action.setBadgeText({ text: text });
        chrome.action.setBadgeBackgroundColor({ color: color });
      } else {
        chrome.action.setBadgeText({ text: "" });
      }
    }
  } catch (e) {
    console.error("Badge update failed:", e);
  }
}

// MV3 Alarm to keep background active for polling
if (chrome.alarms) {
    chrome.alarms.create("badgeUpdate", { periodInMinutes: 3 });  // 個人利用向けに最適化
    chrome.alarms.onAlarm.addListener((alarm) => {
      if (alarm.name === "badgeUpdate") {
        updateBadge();
      }
    });
}

// Initial update - wrap in try-catch to prevent SW registration failure
try {
  updateBadge();
} catch (e) {
  console.error("Initial badge update failed:", e);
}

// ------------------------------------------------------------------
// Message Listeners
// ------------------------------------------------------------------
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || !message.action) {
    sendResponse({ ok: false, error: 'No action provided' });
    return true;
  }

  (async () => {
    try {
      if (message.action === 'health') {
        return sendResponse(await checkHealth());
      }
      if (message.action === 'getContext') {
        const health = await checkHealth();
        return sendResponse({ ok: true, hostName: HOST_NAME, extensionId: chrome.runtime.id, browserName: detectBrowserName(), backendUrls: BACKEND_URLS, health });
      }
      if (message.action === 'startBackend') {
        const res = await sendNativeMessage({ action: 'start_backend', extensionId: chrome.runtime.id });
        // Start badge update soon after backend starts
        setTimeout(updateBadge, 2000);
        return sendResponse(res);
      }
      if (message.action === 'stopBackend') {
        const health = await checkHealth();
        if (!health.ok) {
          return sendResponse({ ok: false, error: 'バックエンドは既に停止しています(未接続)' });
        }

        try {
          const res = await fetch(`${health.base}/api/shutdown`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ confirm: true }),
            cache: 'no-store'
          });
          const data = await res.json().catch(() => ({}));
          if (!res.ok || data?.ok === false) {
            const rawError = String(data?.error || `HTTP ${res.status}`);
            if (rawError.includes('shutdown token is not configured')) {
              return sendResponse({
                ok: false,
                error: '古いバックエンドが起動しています。バックエンドを再起動し、拡張機能を再読み込みしてください。'
              });
            }
            return sendResponse({ ok: false, error: rawError });
          }
          chrome.action.setBadgeText({ text: "" });
          return sendResponse({ ok: true });
        } catch (e) {
          return sendResponse({ ok: false, error: e.message || String(e) });
        }
      }
      if (message.action === 'openMain') {
        return sendResponse(await openRoute('/main'));
      }
      if (message.action === 'openSetup') {
        return sendResponse(await openRoute('/setup'));
      }
      if (message.action === 'openSettings') {
        return sendResponse(await openRoute('/settings'));
      }
      
      return sendResponse({ ok: false, error: 'Unknown action' });
    } catch (e) {
      return sendResponse({ ok: false, error: e.message || String(e) });
    }
  })();
  
  return true;
});
