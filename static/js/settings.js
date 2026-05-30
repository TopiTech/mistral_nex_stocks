const DEFAULT_SYMBOLS = window.DEFAULT_SYMBOLS || { us: [], jp: [], idx: [] };

const dragInitialized = new Set();

function getSortOrder(market) {
  try {
    const parsed = JSON.parse(localStorage.getItem(`sort_${market}`) || '[]');
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((item) => typeof item === 'string');
  } catch {
    return [];
  }
}

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
    console.error('Failed to load stocks:', e);
    alert('銘柄リストの取得に失敗しました。しばらくして再度お試しください。');
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
  } catch (e) {
    console.error(e);
    alert(`削除に失敗しました: ${e.message || '不明なエラー'}`);
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
  } catch (e) {
    console.error(e);
    alert(`初期化に失敗しました: ${e.message || '不明なエラー'}`);
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
      console.error('Server-side logout failed:', error);
      // Browser storage already cleared, still proceed to setup
      location.href = '/setup';
    });
}

loadStocks();
