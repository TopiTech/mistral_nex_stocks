const APP_CONFIG = (() => {
  try {
    const el = document.getElementById('app-config-data');
    return el && el.textContent ? JSON.parse(el.textContent) : {};
  } catch {
    return {};
  }
})();
window.APP_CONFIG = APP_CONFIG;
const legacyMistralKey = sessionStorage.getItem('MISTRAL_API_KEY') || localStorage.getItem('MISTRAL_API_KEY') || '';
const legacyLangsearchKey = sessionStorage.getItem('LANGSEARCH_API_KEY') || localStorage.getItem('LANGSEARCH_API_KEY') || '';

const getEl = (id) => document.getElementById(id);
const setErrorMessage = (message) => {
  const errorMsg = getEl('errorMsg');
  if (!errorMsg) return;
  errorMsg.textContent = message;
  errorMsg.style.display = message ? 'block' : 'none';
};

function clearLegacyBrowserCredentials(options = {}) {
  const mistral = options.mistral !== false;
  const langsearch = options.langsearch !== false;
  if (mistral) {
    sessionStorage.removeItem('MISTRAL_API_KEY');
    localStorage.removeItem('MISTRAL_API_KEY');
  }
  if (langsearch) {
    sessionStorage.removeItem('LANGSEARCH_API_KEY');
    localStorage.removeItem('LANGSEARCH_API_KEY');
  }
}

async function storeCredentials(mistralApiKey, langsearchApiKey) {
  const payload = {
    mistral_api_key: mistralApiKey,
  };
  if (langsearchApiKey) {
    payload.langsearch_api_key = langsearchApiKey;
  }

  const response = await fetch('/api/credentials', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) {
    const errorMsg = data.details?.reason || data.error || data.message || 'APIキーの保存に失敗しました';
    throw new Error(errorMsg);
  }
  clearLegacyBrowserCredentials();
  return data;
}

async function getCredentialState() {
  const response = await fetch('/api/credentials', { cache: 'no-store' });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || data.message || 'APIキー状態の取得に失敗しました');
  }
  return data;
}

async function bootstrapLegacyCredentials() {
  try {
    const state = await getCredentialState();
    if (state.has_mistral_api_key) {
      clearLegacyBrowserCredentials({ mistral: true, langsearch: false });
      window.location.href = '/main';
      return;
    }
  } catch (error) {
    console.warn('Failed to read backend credential state:', error);
  }

  if (APP_CONFIG.has_mistral_api_key) {
    clearLegacyBrowserCredentials({ mistral: true, langsearch: false });
    window.location.href = '/main';
    return;
  }

  if (!legacyMistralKey) {
    return;
  }

  try {
    await storeCredentials(legacyMistralKey, legacyLangsearchKey);
    window.location.href = '/main';
  } catch (error) {
    console.warn('Legacy credential migration failed:', error);
  }
}

async function saveKey() {
  const keyInput = getEl('apiKey');
  const langsearchInput = getEl('langsearchApiKey');

  if (!keyInput) {
    setErrorMessage('APIキー入力欄が見つかりません');
    return;
  }

  const key = (keyInput.value || '').trim();
  const langsearchKey = (langsearchInput?.value || '').trim();
  if (!key) {
    setErrorMessage('APIキーを入力してください');
    keyInput.focus();
    return;
  }

  try {
    await storeCredentials(key, langsearchKey);
    setErrorMessage('');
    window.location.href = '/main';
  } catch (error) {
    setErrorMessage(error.message || 'APIキーの保存に失敗しました');
  }
}

document.addEventListener('DOMContentLoaded', () => {
  getEl('saveBtn')?.addEventListener('click', saveKey);
  getEl('apiKey')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') saveKey();
  });
  getEl('langsearchApiKey')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') saveKey();
  });

  bootstrapLegacyCredentials();
});
