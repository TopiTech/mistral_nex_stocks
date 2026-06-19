// Shared config initialization from embedded JSON script tags.
// Included by both index.html and settings.html to avoid duplication.

const DEFAULT_SYMBOLS = (() => {
  try {
    const el = document.getElementById("default-symbols-data");
    const data = el ? el.textContent : null;
    return data ? JSON.parse(data) : { us: [], jp: [], idx: [] };
  } catch {
    return { us: [], jp: [], idx: [] };
  }
})();
window.DEFAULT_SYMBOLS = DEFAULT_SYMBOLS;

const APP_CONFIG = (() => {
  try {
    const el = document.getElementById("app-config-data");
    return el && el.textContent ? JSON.parse(el.textContent) : {};
  } catch {
    return {};
  }
})();
window.APP_CONFIG = APP_CONFIG;
