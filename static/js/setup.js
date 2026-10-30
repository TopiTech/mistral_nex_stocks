/**
 * Setup/onboarding page controller.
 * Handles API key registration, legacy credential migration,
 * and redirect to main dashboard after successful setup.
 */

// APP_CONFIG is initialized by config_init.js

const _appCfg = typeof APP_CONFIG !== "undefined" ? APP_CONFIG : {};
const MIN_KEY_LENGTHS = {
  mistral: Number(_appCfg.mistral_api_key_min_length) || 32,
  langsearch: Number(_appCfg.langsearch_api_key_min_length) || 20,
  tavily: Number(_appCfg.tavily_api_key_min_length) || 5,
};

let setupAbortController = null;

function getOrCreateSetupAbortController() {
  if (!setupAbortController || setupAbortController.signal.aborted) {
    setupAbortController = new AbortController();
  }
  return setupAbortController;
}

const getEl = (id) => document.getElementById(id);
const setErrorMessage = (message) => {
  const errorMsg = getEl("errorMsg");
  if (!errorMsg) return;
  errorMsg.textContent = message;
  errorMsg.style.display = message ? "block" : "none";
};

const OPENAI_PRESETS = {
  openai: {
    baseUrl: "https://api.openai.com/v1",
    model: "gpt-4o-mini",
    embeddingsModel: "text-embedding-3-small",
  },
  ollama: {
    baseUrl: "http://localhost:11434/v1",
    model: "llama3.2",
    embeddingsModel: "nomic-embed-text",
  },
  groq: {
    baseUrl: "https://api.groq.com/openai/v1",
    model: "llama-3.3-70b-versatile",
    embeddingsModel: "",
  },
  deepseek: {
    baseUrl: "https://api.deepseek.com/v1",
    model: "deepseek-chat",
    embeddingsModel: "",
  },
  openrouter: {
    baseUrl: "https://openrouter.ai/api/v1",
    model: "meta-llama/llama-3.3-70b-instruct",
    embeddingsModel: "",
  },
};

const getAiProvider = () => {
  const checked = document.querySelector('input[name="aiProvider"]:checked');
  return checked && checked.value === "openai_compatible"
    ? "openai_compatible"
    : "mistral";
};

function syncProviderUi() {
  const isMistral = getAiProvider() === "mistral";
  const mistralContainer = getEl("mistralSetupContainer");
  const openaiContainer = getEl("openaiSetupContainer");
  if (mistralContainer) mistralContainer.hidden = !isMistral;
  if (openaiContainer) openaiContainer.hidden = isMistral;
  setErrorMessage("");
}

function applyPreset(name) {
  const preset = OPENAI_PRESETS[name];
  if (!preset) return;
  const baseUrlInput = getEl("openaiBaseUrl");
  const modelInput = getEl("openaiModel");
  const embeddingsInput = getEl("openaiEmbeddingsModel");
  if (baseUrlInput) baseUrlInput.value = preset.baseUrl;
  if (modelInput) modelInput.value = preset.model;
  if (embeddingsInput) embeddingsInput.value = preset.embeddingsModel;
  const apiKeyInput = getEl("openaiApiKey");
  if (apiKeyInput) {
    if (name === "ollama") {
      apiKeyInput.placeholder = "ローカル接続（Ollama等）は未入力で利用可能";
    } else {
      apiKeyInput.placeholder = "sk-... (クラウドプロバイダのAPIキー)";
    }
  }
}

async function verifyOpenaiSetup() {
  const btn = getEl("verifyOpenaiSetupBtn");
  const statusEl = getEl("openaiSetupVerifyStatus");
  const baseUrl = (getEl("openaiBaseUrl")?.value || "").trim();
  const apiKey = (getEl("openaiApiKey")?.value || "").trim();

  if (!baseUrl) {
    if (statusEl) {
      statusEl.textContent = "Base URLを入力してください";
      statusEl.className = "verify-status error";
    }
    return;
  }

  if (btn) {
    btn.disabled = true;
    btn.textContent = "テスト中...";
  }
  if (statusEl) {
    statusEl.textContent = "接続確認中...";
    statusEl.className = "verify-status";
  }

  try {
    const res = await csrfFetch("/api/credentials/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        target_provider: "openai_compatible",
        base_url: baseUrl,
        api_key: apiKey,
      }),
      signal: getOrCreateSetupAbortController().signal,
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) {
      const latencyStr =
        typeof data.latency_ms === "number" ? ` (${data.latency_ms}ms)` : "";
      if (statusEl) {
        statusEl.textContent = `接続成功${latencyStr}！利用可能モデル: ${
          data.models?.length || 0
        }件`;
        statusEl.className = "verify-status success";
      }
    } else {
      const err =
        data.details?.reason ||
        data.error ||
        data.message ||
        "接続に失敗しました";
      if (statusEl) {
        statusEl.textContent = `エラー: ${err}`;
        statusEl.className = "verify-status error";
      }
    }
  } catch (err) {
    if (err?.name === "AbortError") return;
    if (statusEl) {
      statusEl.textContent = `エラー: ${err.message || "通信エラー"}`;
      statusEl.className = "verify-status error";
    }
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "接続テスト";
    }
  }
}

async function storeCredentials(options = {}) {
  const {
    aiProvider = "mistral",
    mistralApiKey = "",
    langsearchApiKey = "",
    tavilyApiKey = "",
    apiMode = "chat",
    agentId = "",
    openaiBaseUrl = "",
    openaiApiKey = "",
    openaiModel = "",
    openaiEmbeddingsModel = "",
  } = options;

  const payload = {
    ai_provider: aiProvider,
  };

  if (aiProvider === "openai_compatible") {
    payload.openai_base_url = openaiBaseUrl;
    payload.openai_model = openaiModel;
    payload.openai_embeddings_model = openaiEmbeddingsModel;
    if (openaiApiKey) {
      payload.openai_api_key = openaiApiKey;
    }
  } else {
    payload.mistral_api_key = mistralApiKey;
    payload.mistral_api_mode = apiMode;
    if (apiMode === "agents") {
      payload.mistral_agent_id = agentId;
    }
  }

  if (langsearchApiKey) {
    payload.langsearch_api_key = langsearchApiKey;
  }
  if (tavilyApiKey) {
    payload.tavily_api_key = tavilyApiKey;
  }

  const response = await csrfFetch("/api/credentials", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal: getOrCreateSetupAbortController().signal,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) {
    const errorMsg =
      data.details?.reason ||
      data.error ||
      data.message ||
      "APIキーの保存に失敗しました";
    throw new Error(errorMsg);
  }
  clearLegacyBrowserCredentials();
  return data;
}

async function getCredentialState() {
  const response = await fetchWithTimeout(
    "/api/credentials",
    {
      cache: "no-store",
      signal: getOrCreateSetupAbortController().signal,
    },
    FAST_FETCH_TIMEOUT_MS,
  );
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) {
    throw new Error(
      data.error || data.message || "APIキー状態の取得に失敗しました",
    );
  }
  return data;
}

async function bootstrapLegacyCredentials() {
  clearLegacyBrowserCredentials({
    mistral: true,
    langsearch: true,
    tavily: true,
  });

  const urlParams = new URLSearchParams(window.location.search);
  if (urlParams.get("logout") === "1" || urlParams.get("logged_out") === "1") {
    // Explicit logout transition: stay on setup page
    return;
  }

  try {
    const state = await getCredentialState();
    if (state.has_ai_access ?? state.has_mistral_api_key) {
      window.location.href = "/main";
      return;
    }
  } catch (error) {
    console.warn("Failed to read backend credential state:", error);
  }

  if (
    window.APP_CONFIG?.has_ai_access ??
    window.APP_CONFIG?.has_mistral_api_key
  ) {
    window.location.href = "/main";
    return;
  }
}

const getApiMode = () => {
  const checked = document.querySelector('input[name="apiMode"]:checked');
  return checked && checked.value === "agents" ? "agents" : "chat";
};

function syncApiModeUi() {
  const group = getEl("agentIdGroup");
  if (group) group.hidden = getApiMode() !== "agents";
  setErrorMessage("");
}

async function startPreviewMode() {
  const btn = getEl("previewBtn");
  if (btn) btn.disabled = true;
  try {
    const response = await csrfFetch("/api/credentials", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ preview_mode: true }),
      signal: getOrCreateSetupAbortController().signal,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) {
      throw new Error(
        data.details?.reason ||
          data.error ||
          data.message ||
          "プレビューモードを開始できませんでした",
      );
    }
    setErrorMessage("");
    window.location.href = "/main";
  } catch (error) {
    if (error?.name === "AbortError") return;
    setErrorMessage(error.message || "プレビューモードを開始できませんでした");
    if (btn) btn.disabled = false;
  }
}

async function saveKey() {
  const saveBtn = getEl("saveBtn");
  const aiProvider = getAiProvider();
  const langsearchInput = getEl("langsearchApiKey");
  const tavilyInput = getEl("tavilyApiKey");

  const langsearchKey = (langsearchInput?.value || "").trim();
  const tavilyKey = (tavilyInput?.value || "").trim();

  if (langsearchKey && langsearchKey.length < MIN_KEY_LENGTHS.langsearch) {
    setErrorMessage(
      `LangSearch APIキーは${MIN_KEY_LENGTHS.langsearch}文字以上である必要があります（現在${langsearchKey.length}文字）`,
    );
    langsearchInput?.focus();
    return;
  }
  if (tavilyKey && tavilyKey.length < MIN_KEY_LENGTHS.tavily) {
    setErrorMessage(
      `Tavily APIキーは${MIN_KEY_LENGTHS.tavily}文字以上である必要があります（現在${tavilyKey.length}文字）`,
    );
    tavilyInput?.focus();
    return;
  }

  const options = {
    aiProvider,
    langsearchApiKey: langsearchKey,
    tavilyApiKey: tavilyKey,
  };

  if (aiProvider === "openai_compatible") {
    const baseUrlInput = getEl("openaiBaseUrl");
    const apiKeyInput = getEl("openaiApiKey");
    const modelInput = getEl("openaiModel");
    const embModelInput = getEl("openaiEmbeddingsModel");

    const baseUrl = (baseUrlInput?.value || "").trim();
    const apiKey = (apiKeyInput?.value || "").trim();
    const model = (modelInput?.value || "").trim();
    const embModel = (embModelInput?.value || "").trim();

    if (!baseUrl) {
      setErrorMessage("OpenAI Base URLを入力してください");
      baseUrlInput?.focus();
      return;
    }
    if (!baseUrl.startsWith("http://") && !baseUrl.startsWith("https://")) {
      setErrorMessage(
        "OpenAI Base URLは http:// または https:// で始まっている必要があります",
      );
      baseUrlInput?.focus();
      return;
    }
    if (!model) {
      setErrorMessage("チャットモデル名を入力してください");
      modelInput?.focus();
      return;
    }

    options.openaiBaseUrl = baseUrl;
    options.openaiApiKey = apiKey;
    options.openaiModel = model;
    options.openaiEmbeddingsModel = embModel;
  } else {
    const keyInput = getEl("apiKey");
    const agentInput = getEl("agentId");
    if (!keyInput) {
      setErrorMessage("APIキー入力欄が見つかりません");
      return;
    }

    const apiMode = getApiMode();
    const key = (keyInput.value || "").trim();
    const agentId = (agentInput?.value || "").trim();

    if (!key) {
      setErrorMessage("Mistral APIキーを入力してください");
      keyInput.focus();
      return;
    }
    if (key.length < MIN_KEY_LENGTHS.mistral) {
      setErrorMessage(
        `Mistral APIキーは${MIN_KEY_LENGTHS.mistral}文字以上である必要があります（現在${key.length}文字）`,
      );
      keyInput.focus();
      return;
    }
    if (apiMode === "agents" && !agentId) {
      setErrorMessage("Agents APIを使用するにはAgent IDを入力してください");
      agentInput?.focus();
      return;
    }

    options.mistralApiKey = key;
    options.apiMode = apiMode;
    options.agentId = apiMode === "agents" ? agentId : "";
  }

  if (saveBtn) {
    saveBtn.disabled = true;
    saveBtn.textContent = "保存中...";
  }

  try {
    await storeCredentials(options);
    setErrorMessage("");
    window.location.href = "/main";
  } catch (error) {
    if (error?.name === "AbortError") return;
    setErrorMessage(error.message || "APIキーの保存に失敗しました");
    if (saveBtn) {
      saveBtn.disabled = false;
      saveBtn.textContent = "保存して開始";
    }
  }
}

/**
 * Toggle password field visibility.
 * @param {string} inputId - ID of the password input field
 */
function togglePasswordVisibility(inputId) {
  const input = getEl(inputId);
  const btn = document.querySelector(
    `.password-toggle[data-target="${inputId}"]`,
  );
  if (!input || !btn) return;
  const isPassword = input.type === "password";
  input.type = isPassword ? "text" : "password";
  btn.classList.toggle("visible", isPassword);
  btn.setAttribute("aria-pressed", isPassword ? "true" : "false");
  btn.setAttribute(
    "aria-label",
    isPassword ? "APIキーを非表示にする" : "APIキーを表示する",
  );
  const icon = btn.querySelector(".toggle-icon");
  if (icon) {
    icon.textContent = isPassword ? "\u{1F441}" : "\u{1F576}";
  }
}

document.addEventListener("DOMContentLoaded", () => {
  getEl("saveBtn")?.addEventListener("click", saveKey);
  getEl("apiKey")?.addEventListener("keydown", (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter") {
      e.preventDefault();
      saveKey();
    }
  });
  getEl("openaiBaseUrl")?.addEventListener("keydown", (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter") {
      e.preventDefault();
      saveKey();
    }
  });
  getEl("openaiApiKey")?.addEventListener("keydown", (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter") {
      e.preventDefault();
      saveKey();
    }
  });
  getEl("openaiModel")?.addEventListener("keydown", (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter") {
      e.preventDefault();
      saveKey();
    }
  });
  getEl("openaiEmbeddingsModel")?.addEventListener("keydown", (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter") {
      e.preventDefault();
      saveKey();
    }
  });
  getEl("langsearchApiKey")?.addEventListener("keydown", (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter") {
      e.preventDefault();
      saveKey();
    }
  });
  getEl("tavilyApiKey")?.addEventListener("keydown", (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter") {
      e.preventDefault();
      saveKey();
    }
  });

  // Provider toggle listener
  document.querySelectorAll('input[name="aiProvider"]').forEach((radio) => {
    radio.addEventListener("change", syncProviderUi);
  });
  syncProviderUi();

  // Preset buttons
  document.querySelectorAll(".preset-pill-btn").forEach((btn) => {
    btn.addEventListener("click", () => applyPreset(btn.dataset.preset));
  });

  // Test OpenAI connection button
  getEl("verifyOpenaiSetupBtn")?.addEventListener("click", verifyOpenaiSetup);

  // API mode radio change listeners
  document.querySelectorAll('input[name="apiMode"]').forEach((radio) => {
    radio.addEventListener("change", syncApiModeUi);
  });
  syncApiModeUi();

  // Preview mode button
  getEl("previewBtn")?.addEventListener("click", startPreviewMode);

  // Agent ID Enter key
  getEl("agentId")?.addEventListener("keydown", (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter") {
      e.preventDefault();
      saveKey();
    }
  });

  // Password visibility toggles
  document.querySelectorAll(".password-toggle").forEach((btn) => {
    const targetId = btn.dataset.target;
    if (targetId) {
      btn.addEventListener("click", () => togglePasswordVisibility(targetId));
    }
  });

  // Ensure APP_CONFIG-dependent code never throws when config_init.js is blocked (CSP/adblock)
  if (typeof APP_CONFIG === "undefined") {
    window.APP_CONFIG = {};
  }
  bootstrapLegacyCredentials();
});

if (
  typeof window !== "undefined" &&
  typeof window.addEventListener === "function"
) {
  window.addEventListener("beforeunload", () => {
    if (setupAbortController) {
      setupAbortController.abort();
      setupAbortController = null;
    }
  });
}
