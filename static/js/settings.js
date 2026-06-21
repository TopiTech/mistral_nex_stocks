// DEFAULT_SYMBOLS and APP_CONFIG are initialized by config_init.js

const dragInitialized = new Set();

// getSortOrderとsaveSortOrderはutils.jsで定義済み（全ページ共通）
// saveSortOrderはlocalStorageに保存するユーティリティ
function saveSortOrder(market, order) {
  localStorage.setItem(`sort_${market}`, JSON.stringify(order));
}

function sortIndex(order, symbol) {
  const idx = order.indexOf(symbol);
  return idx === -1 ? Number.MAX_SAFE_INTEGER : idx;
}

// escapeHtml関数は index.js から利用可能

function ensureDragContainer(container, market) {
  if (dragInitialized.has(market)) return;
  container.addEventListener('dragover', (e) => {
    e.preventDefault();
    const after = getDragAfterElement(container, e.clientY);
    const dragging = container.querySelector('.stock-item.dragging');
    if (!dragging) return;
    if (after == null) container.appendChild(dragging);
    else container.insertBefore(dragging, after);
  });
  dragInitialized.add(market);
}

async function loadStocks() {
  try {
    const res = await fetch('/api/stocks');
    const payload = await res.text();
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}: ${res.statusText}`);
    }
    const data = payload ? JSON.parse(payload) : {};
    const stocksObj = data.stocks || data;
    const userUS = (stocksObj.us || []).filter((s) => !DEFAULT_SYMBOLS.us.includes(s.symbol));
    const userJP = (stocksObj.jp || []).filter((s) => !DEFAULT_SYMBOLS.jp.includes(s.symbol));
    const userIdx = (stocksObj.idx || []).filter((s) => !DEFAULT_SYMBOLS.idx.includes(s.symbol));
    renderList('us', userUS);
    renderList('jp', userJP);
    renderList('idx', userIdx);
  } catch (e) {
    logger.error('Failed to load stocks:', e);
    showSettingsMessage('銘柄リストの取得に失敗しました。しばらくして再度お試しください。');
  }
}

function renderList(market, stocks) {
  const listEl = document.getElementById(`${market}-list`);
  listEl.textContent = '';
  ensureDragContainer(listEl, market);
  if (!stocks.length) {
    const empty = document.createElement('li');
    empty.className = 'empty-message';
    empty.textContent = '追加銘柄はありません';
    listEl.appendChild(empty);
    return;
  }
  const order = getSortOrder(market);
  const sorted = [...stocks].sort((a, b) => sortIndex(order, a.symbol) - sortIndex(order, b.symbol));
  sorted.forEach((stock) => {
    const li = document.createElement('li');
    li.className = 'stock-item';
    li.draggable = true;
    li.dataset.symbol = stock.symbol;

    const left = document.createElement('div');
    left.className = 'stock-left';

    const handle = document.createElement('span');
    handle.className = 'drag-handle';
    handle.textContent = '☰';
    left.appendChild(handle);

    const symbolEl = document.createElement('span');
    symbolEl.className = 'stock-symbol';
    symbolEl.textContent = stock.symbol || '';
    left.appendChild(symbolEl);

    const nameEl = document.createElement('span');
    nameEl.className = 'stock-name';
    nameEl.textContent = stock.name || '';
    left.appendChild(nameEl);

    const deleteBtn = document.createElement('button');
    deleteBtn.className = 'delete-btn';
    deleteBtn.type = 'button';
    deleteBtn.textContent = '削除';

    li.appendChild(left);
    li.appendChild(deleteBtn);

    addDragEvents(listEl, li, market);
    deleteBtn.addEventListener('click', () => deleteStock(market, stock.symbol));
    listEl.appendChild(li);
  });
}

function addDragEvents(container, item, market) {
  item.addEventListener('dragstart', (e) => {
    item.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
  });
  item.addEventListener('dragend', () => {
    item.classList.remove('dragging');
    const symbols = [...container.querySelectorAll('.stock-item')].map((li) => li.dataset.symbol);
    saveSortOrder(market, symbols);
  });
}

function getDragAfterElement(container, y) {
  const items = [...container.querySelectorAll('.stock-item:not(.dragging)')];
  return items.reduce((closest, child) => {
    const box = child.getBoundingClientRect();
    const offset = y - box.top - box.height / 2;
    if (offset < 0 && offset > closest.offset) return { offset, element: child };
    return closest;
  }, { offset: Number.NEGATIVE_INFINITY }).element;
}

async function deleteStock(market, symbol) {
  if (!confirm(`${symbol} を削除しますか？`)) return;
  try {
    const res = await fetch('/api/stocks/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol, market })
    });
    const payload = await res.text();
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}: ${res.statusText}`);
    }
    const data = payload ? JSON.parse(payload) : {};
    if (data.error) throw new Error(data.error);
    loadStocks();
    showSettingsMessage('銘柄を削除しました', false);
  } catch (e) {
    logger.error(e);
    showToast(`削除に失敗しました: ${e.message || '不明なエラー'}`, "#ff7d7d");
  }
}

async function resetAllStocks() {
  if (!confirm('追加した全ての銘柄を削除しますか？\nこの操作は元に戻せません。')) return;
  try {
    const res = await fetch('/api/stocks/reset', { method: 'POST' });
    const payload = await res.text();
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}: ${res.statusText}`);
    }
    const data = payload ? JSON.parse(payload) : {};
    if (data.error) throw new Error(data.error);
    localStorage.removeItem('sort_us');
    localStorage.removeItem('sort_jp');
    localStorage.removeItem('sort_idx');
    loadStocks();
    showSettingsMessage('銘柄リストを初期化しました', false);
  } catch (e) {
    logger.error(e);
    showToast(`初期化に失敗しました: ${e.message || '不明なエラー'}`, "#ff7d7d");
  }
}

function logout() {
  if (!confirm('APIキーを削除してログアウトしますか？')) return;

  // Clear browser storage immediately to ensure it's always removed
  sessionStorage.removeItem('MISTRAL_API_KEY');
  sessionStorage.removeItem('LANGSEARCH_API_KEY');
  localStorage.removeItem('MISTRAL_API_KEY');
  localStorage.removeItem('LANGSEARCH_API_KEY');

  // Attempt to clear server-side credentials
  fetch('/api/credentials', { method: 'DELETE' })
    .then((response) => {
      if (!response.ok) {
        throw new Error(`Server error: ${response.status}`);
      }
      // Server-side clear succeeded, navigate to setup
      location.href = '/setup';
    })
    .catch((error) => {
      logger.error('Server-side logout failed:', error);
      // Browser storage already cleared, still proceed to setup
      location.href = '/setup';
    });
}

document.addEventListener('DOMContentLoaded', () => {
  loadStocks();

  const backBtn = document.getElementById('back-btn');
  if (backBtn) backBtn.addEventListener('click', () => { location.href = '/main'; });

  const resetBtn = document.getElementById('reset-btn');
  if (resetBtn) resetBtn.addEventListener('click', resetAllStocks);

  const logoutBtn = document.getElementById('logout-btn');
  if (logoutBtn) logoutBtn.addEventListener('click', logout);

  const promptInput = document.getElementById('custom-prompt-input');
  const savePromptBtn = document.getElementById('save-prompt-btn');
  const promptStatus = document.getElementById('prompt-save-status');

  if (promptInput && savePromptBtn) {
    // Load existing custom prompt
    fetch('/api/credentials')
      .then(res => res.json())
      .then(data => {
        if (data.ok && data.custom_ai_prompt) {
          promptInput.value = data.custom_ai_prompt;
        }
      })
      .catch(err => logger.error("Failed to load prompt:", err));

    // Save prompt
    savePromptBtn.addEventListener('click', async () => {
      savePromptBtn.disabled = true;
      savePromptBtn.textContent = '保存中...';
      try {
        const res = await fetch('/api/credentials', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ custom_ai_prompt: promptInput.value })
        });
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.details?.reason || data.error || '保存に失敗しました');
        
        promptStatus.textContent = '✓ 保存しました';
        setTimeout(() => { promptStatus.textContent = ''; }, 3000);
      } catch (err) {
        logger.error("Save prompt error:", err);
        showToast(`プロンプトの保存に失敗しました: ${err.message}`, "#ff7d7d");
      } finally {
        savePromptBtn.disabled = false;
        savePromptBtn.textContent = '保存';
      }
    });
  }
});

// showToastはutils.jsで定義済み（全ページ共通）

// Alias for backward compatibility with existing code
const showSettingsMessage = (message, isError = true) => {
  showToast(message, isError ? "#ff7d7d" : "#6bb6ff");
};
