/**
 * Setup/onboarding page controller.
 * Handles API key registration, legacy credential migration,
 * and redirect to main dashboard after successful setup.
 */

// APP_CONFIG is initialized by config_init.js
const legacyMistralKey =
  sessionStorage.getItem("MISTRAL_API_KEY") ||
  localStorage.getItem("MISTRAL_API_KEY") ||
  "";
const legacyLangsearchKey =
  sessionStorage.getItem("LANGSEARCH_API_KEY") ||
  localStorage.getItem("LANGSEARCH_API_KEY") ||
  "";
const legacyTavilyKey =
  sessionStorage.getItem("TAVILY_API_KEY") ||
  localStorage.getItem("TAVILY_API_KEY") ||
  "";

const getEl = (id) => document.getElementById(id);
const setErrorMessage = (message) => {
  const errorMsg = getEl("errorMsg");
  if (!errorMsg) return;
  errorMsg.textContent = message;
  errorMsg.style.display = message ? "block" : "none";
};

async function storeCredentials(mistralApiKey, langsearchApiKey, tavilyApiKey) {
  const payload = {
    mistral_api_key: mistralApiKey,
  };
  if (langsearchApiKey) {
    payload.langsearch_api_key = langsearchApiKey;
  }
  if (tavilyApiKey) {
    payload.tavily_api_key = tavilyApiKey;
  }

  const response = await fetch("/api/credentials", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
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
  const response = await fetch("/api/credentials", { cache: "no-store" });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) {
    throw new Error(
      data.error || data.message || "APIキー状態の取得に失敗しました",
    );
  }
  return data;
}

async function bootstrapLegacyCredentials() {
  try {
    const state = await getCredentialState();
    if (state.has_mistral_api_key) {
      clearLegacyBrowserCredentials({ mistral: true, langsearch: false });
      window.location.href = "/main";
      return;
    }
  } catch (error) {
    console.warn("Failed to read backend credential state:", error);
  }

  if (APP_CONFIG.has_mistral_api_key) {
    clearLegacyBrowserCredentials({ mistral: true, langsearch: false });
    window.location.href = "/main";
    return;
  }

  if (!legacyMistralKey) {
    return;
  }

  try {
    await storeCredentials(
      legacyMistralKey,
      legacyLangsearchKey,
      legacyTavilyKey,
    );
    window.location.href = "/main";
  } catch (error) {
    console.warn("Legacy credential migration failed:", error);
  }
}

async function saveKey() {
  const keyInput = getEl("apiKey");
  const langsearchInput = getEl("langsearchApiKey");
  const tavilyInput = getEl("tavilyApiKey");

  if (!keyInput) {
    setErrorMessage("APIキー入力欄が見つかりません");
    return;
  }

  const key = (keyInput.value || "").trim();
  const langsearchKey = (langsearchInput?.value || "").trim();
  const tavilyKey = (tavilyInput?.value || "").trim();
  if (!key) {
    setErrorMessage("APIキーを入力してください");
    keyInput.focus();
    return;
  }

  try {
    await storeCredentials(key, langsearchKey, tavilyKey);
    setErrorMessage("");
    window.location.href = "/main";
  } catch (error) {
    setErrorMessage(error.message || "APIキーの保存に失敗しました");
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
  btn.querySelector(".toggle-icon").textContent = isPassword
    ? "\u{1F441}"
    : "\u{1F576}";
}

document.addEventListener("DOMContentLoaded", () => {
  getEl("saveBtn")?.addEventListener("click", saveKey);
  getEl("apiKey")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") saveKey();
  });
  getEl("langsearchApiKey")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") saveKey();
  });
  getEl("tavilyApiKey")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") saveKey();
  });

  // Password visibility toggles
  document.querySelectorAll(".password-toggle").forEach((btn) => {
    const targetId = btn.dataset.target;
    if (targetId) {
      btn.addEventListener("click", () => togglePasswordVisibility(targetId));
    }
  });

  bootstrapLegacyCredentials();
});
