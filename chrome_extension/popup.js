// --- Security Utilities ---
/**
 * 安全なテキストコンテンツを設定
 * textContentは自動的にHTMLエスケープされるため、
 * 追加のサニタイズは不要
 * @param {HTMLElement} element - 対象の要素
 * @param {string} text - 設定するテキスト
 */
function setSafeText(element, text) {
  if (!element) return;
  element.textContent = String(text || '');
}

const $ = (id) => document.getElementById(id);
const healthPill = $('healthPill');
const healthMeta = $('healthMeta');
const browserPill = $('browserPill');
const diagBox = $('diagBox');

async function send(action) {
  return chrome.runtime.sendMessage({ action });
}

function setHealth(health) {
  if (health?.ok) {
    setSafeText(healthPill, '起動済み');
    healthPill.className = 'pill ok';
    setSafeText(healthMeta, `${health.base} / model=${health.data?.model || '-'} / badge=${health.data?.badge || '-'}`);
    $('startBtn').style.display = 'none';
    $('stopBtn').style.display = 'block';
  } else {
    setSafeText(healthPill, '未起動');
    healthPill.className = 'pill ng';
    setSafeText(healthMeta, 'バックエンドに接続できません');
    $('startBtn').style.display = 'block';
    $('stopBtn').style.display = 'none';
  }
}

function buildDiagnostics(ctx) {
  return [
    `browser      : ${ctx.browserName}`,
    `extensionId  : ${ctx.extensionId}`,
    `hostName     : ${ctx.hostName}`,
    `backendUrls  : ${ctx.backendUrls.join(', ')}`,
    `backendAlive : ${ctx.health?.ok ? 'yes' : 'no'}`,
    ctx.health?.ok ? `backendBase  : ${ctx.health.base}` : '',
    ctx.health?.ok ? `model        : ${ctx.health.data?.model || ''}` : '',
    ctx.health?.ok ? `badge        : ${ctx.health.data?.badge || ''}` : '',
  ].filter(Boolean).join('\n');
}

async function refresh() {
  const ctx = await send('getContext');
  if (!ctx?.ok) throw new Error(ctx?.error || '状態取得に失敗しました');
  setSafeText(browserPill, ctx.browserName);
  setHealth(ctx.health);
  setSafeText(diagBox, buildDiagnostics(ctx));
  return ctx;
}

async function withBusy(btn, fn) {
  const prev = btn.textContent;
  btn.disabled = true;
  try { return await fn(); }
  finally { btn.disabled = false; btn.textContent = prev; }
}

function handleUIError(error) {
  const message = (error && error.message) ? error.message : String(error || '不明なエラー');
  healthPill.textContent = 'エラー';
  healthPill.className = 'pill ng';
  healthMeta.textContent = message;
}

function bindAsyncButton(id, handler) {
  const el = $(id);
  if (!el) return;
  el.addEventListener('click', () => {
    Promise.resolve(handler()).catch((e) => {
      console.error(e);
      handleUIError(e);
    });
  });
}

async function waitForBackendReady(maxWaitMs = 30000) {
  const start = Date.now();
  while (Date.now() - start < maxWaitMs) {
    const ctx = await send('getContext');
    if (ctx?.health?.ok) {
      return ctx;
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error('バックエンド起動の待機がタイムアウトしました');
}

bindAsyncButton('refreshBtn', () => withBusy($('refreshBtn'), refresh));
async function waitForBackendStopped(maxWaitMs = 15000) {
  const start = Date.now();
  while (Date.now() - start < maxWaitMs) {
    const ctx = await send('getContext');
    if (!ctx?.health?.ok) {
      return ctx;
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error('バックエンド停止の待機がタイムアウトしました');
}

bindAsyncButton('startBtn', () => withBusy($('startBtn'), async () => {
  $('startBtn').textContent = '起動中...';
  const res = await send('startBackend');
  if (!res?.ok) throw new Error(res?.error || '起動に失敗しました');
  await waitForBackendReady();
  await refresh();
}));
bindAsyncButton('stopBtn', () => withBusy($('stopBtn'), async () => {
  if (!confirm('バックエンドを停止しますか？')) return;
  $('stopBtn').textContent = '停止中...';
  const res = await send('stopBackend');
  if (!res?.ok) throw new Error(res?.error || '停止に失敗しました');
  await waitForBackendStopped();
  await refresh();
}));
bindAsyncButton('openMainBtn', async () => {
  const res = await send('openMain');
  if (!res?.ok) throw new Error(res?.error || 'メイン画面を開けませんでした');
});
bindAsyncButton('openSetupBtn', async () => {
  const res = await send('openSetup');
  if (!res?.ok) throw new Error(res?.error || 'API設定画面を開けませんでした');
});
bindAsyncButton('openSettingsBtn', async () => {
  const res = await send('openSettings');
  if (!res?.ok) throw new Error(res?.error || '設定画面を開けませんでした');
});
bindAsyncButton('copyDiagBtn', async () => {
  try {
    await navigator.clipboard.writeText(diagBox.textContent || 'no diagnostics');
    $('copyDiagBtn').textContent = 'コピー済み';
    setTimeout(() => { $('copyDiagBtn').textContent = '診断情報をコピー'; }, 1200);
  } catch {
    $('copyDiagBtn').textContent = 'コピー失敗';
    setTimeout(() => { $('copyDiagBtn').textContent = '診断情報をコピー'; }, 1200);
  }
});

refresh().catch((e) => {
  handleUIError(e);
  diagBox.textContent = e.stack || e.message;
});
