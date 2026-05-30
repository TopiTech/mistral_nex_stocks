document.addEventListener('DOMContentLoaded', () => {
    let currentMarket = 'us';
    const canvas = document.getElementById('heatmap-canvas');
    const loading = document.getElementById('heatmap-loading');
    const updateTimeEl = document.getElementById('update-time');

    // Toggle Buttons
    const btnUs = document.getElementById('toggle-us');
    const btnJp = document.getElementById('toggle-jp');

    // Utility

    btnUs.addEventListener('click', () => switchMarket('us'));
    btnJp.addEventListener('click', () => switchMarket('jp'));

    function switchMarket(market) {
        if (currentMarket === market) return;
        currentMarket = market;
        btnUs.classList.toggle('active', market === 'us');
        btnJp.classList.toggle('active', market === 'jp');
        loadHeatmap();
    }

    async function loadHeatmap() {
        loading.style.display = 'flex';
        canvas.textContent = '';

        try {
            const resp = await fetch(`/api/heatmap?market=${currentMarket}`);
            const data = await resp.json();

            if (data.stocks) {
                renderHeatmap(data.stocks);
                updateTimeEl.textContent = new Date().toLocaleTimeString();
            } else {
                canvas.textContent = '';
                const errP = document.createElement('p');
                errP.style.cssText = 'padding:20px;color:#ff4444;';
                errP.textContent = 'データの読み込みに失敗しました。';
                canvas.appendChild(errP);
            }
        } catch (err) {
            console.error('Heatmap fetch error:', err);
            canvas.textContent = '';
            const errP = document.createElement('p');
            errP.style.cssText = 'padding:20px;color:#ff4444;';
            errP.textContent = '市場データの取得に失敗しました。再度お試しください。';
            canvas.appendChild(errP);
        } finally {
            loading.style.display = 'none';
        }
    }

    // ─── Treemap (Grouped Recursive Binary Split) ───────────────────────────
    const VW = 1000;
    const VH = 1000;

    function renderHeatmap(stocks) {
        canvas.textContent = '';
        if (!stocks || stocks.length === 0) return;

        // 1. Group by sector
        const sectorsMap = {};
        let totalCap = 0;
        stocks.forEach(s => {
            const sec = s.sector || 'Other';
            if (!sectorsMap[sec]) sectorsMap[sec] = { name: sec, stocks: [], market_cap: 0 };
            sectorsMap[sec].stocks.push(s);
            sectorsMap[sec].market_cap += (s.market_cap || 1);
            totalCap += (s.market_cap || 1);
        });

        // 2. Prepare sector items for first-level treemap
        const sectorItems = Object.values(sectorsMap).map(sec => ({
            ...sec,
            weight: sec.market_cap / totalCap
        })).sort((a, b) => b.weight - a.weight);

        // 3. Layout sectors
        layoutTreemap(sectorItems, 0, 0, VW, VH, true, (sector, sx, sy, sw, sh) => {
            renderSectorGroup(sector, sx, sy, sw, sh);
        });
    }

    function renderSectorGroup(sector, x, y, w, h) {
        const groupEl = document.createElement('div');
        groupEl.className = 'heatmap-sector-group';
        groupEl.style.position = 'absolute';
        groupEl.style.left = (x / VW * 100).toFixed(4) + '%';
        groupEl.style.top = (y / VH * 100).toFixed(4) + '%';
        groupEl.style.width = (w / VW * 100).toFixed(4) + '%';
        groupEl.style.height = (h / VH * 100).toFixed(4) + '%';

        // Add sector label if big enough
        if (w > 80 && h > 40) {
            const label = document.createElement('div');
            label.className = 'sector-label';
            label.textContent = sector.name;
            groupEl.appendChild(label);
        }

        canvas.appendChild(groupEl);

        // 4. Layout stocks within sector
        const stockItems = sector.stocks.map(s => ({
            ...s,
            weight: (s.market_cap || 1) / sector.market_cap
        })).sort((a, b) => b.weight - a.weight);

        layoutTreemap(stockItems, 0, 0, 100, 100, (w > h), (stock, px, py, pw, ph) => {
            placeNode(stock, px, py, pw, ph, groupEl);
        });
    }

    /**
     * Generic Treemap Layout
     * @param {Array} items 
     * @param {number} x, y, w, h 
     * @param {boolean} horizontal 
     * @param {function} callback Called for each leaf
     */
    function layoutTreemap(items, x, y, w, h, horizontal, callback) {
        if (items.length === 0 || w <= 0 || h <= 0) return;

        if (items.length === 1) {
            callback(items[0], x, y, w, h);
            return;
        }

        const totalWeight = items.reduce((s, i) => s + i.weight, 0);
        let cumWeight = 0;
        let splitIdx = 1;
        for (let i = 0; i < items.length - 1; i++) {
            cumWeight += items[i].weight;
            if (cumWeight >= totalWeight / 2) {
                splitIdx = i + 1;
                break;
            }
        }

        const firstWeight = items.slice(0, splitIdx).reduce((s, i) => s + i.weight, 0);
        const ratio = firstWeight / totalWeight;

        if (horizontal) {
            const splitW = w * ratio;
            layoutTreemap(items.slice(0, splitIdx), x, y, splitW, h, !horizontal, callback);
            layoutTreemap(items.slice(splitIdx), x + splitW, y, w - splitW, h, !horizontal, callback);
        } else {
            const splitH = h * ratio;
            layoutTreemap(items.slice(0, splitIdx), x, y, w, splitH, !horizontal, callback);
            layoutTreemap(items.slice(splitIdx), x, y + splitH, w, h - splitH, !horizontal, callback);
        }
    }

    function placeNode(stock, x, y, w, h, parent) {
        const node = document.createElement('div');
        node.className = 'heatmap-node';
        node.style.position = 'absolute';
        node.style.left = x.toFixed(4) + '%';
        node.style.top = y.toFixed(4) + '%';
        node.style.width = w.toFixed(4) + '%';
        node.style.height = h.toFixed(4) + '%';

        const chg = typeof stock.change_percent === 'number' ? stock.change_percent : 0;
        node.style.backgroundColor = getColor(chg);

        const symbolEl = document.createElement('div');
        symbolEl.className = 'node-symbol';
        symbolEl.textContent = stock.symbol || '';
        node.appendChild(symbolEl);

        const changeEl = document.createElement('div');
        changeEl.className = 'node-change';
        changeEl.textContent = `${chg > 0 ? '+' : ''}${chg.toFixed(2)}%`;
        node.appendChild(changeEl);

        const nameEl = document.createElement('div');
        nameEl.className = 'node-name';
        nameEl.textContent = stock.name || '';
        nameEl.title = stock.name || '';
        node.appendChild(nameEl);

        node.title = `${stock.name || ''} (${stock.symbol || ''})\nSector: ${stock.sector || ''}\nPrice: ${stock.price ?? ''}\nChange: ${chg.toFixed(2)}%`;
        node.onclick = () => { window.location.href = `/main?q=${encodeURIComponent(stock.symbol || '')}`; };

        parent.appendChild(node);
    }

    function getColor(val) {
        // Linear Interpolation for smooth gradients
        // Green: #00e676 (Positive), Red: #d50000 (Negative), Neutral: #263238
        if (val === 0) return '#263238';

        const limit = 5.0;
        const ratio = Math.min(Math.abs(val) / limit, 1.0);

        if (val > 0) {
            // Mix #263238 (38, 50, 56) and #00e676 (0, 230, 118)
            const r = Math.round(38 + (0 - 38) * ratio);
            const g = Math.round(50 + (230 - 50) * ratio);
            const b = Math.round(56 + (118 - 56) * ratio);
            return `rgb(${r},${g},${b})`;
        } else {
            // Mix #263238 (38, 50, 56) and #d50000 (213, 0, 0)
            const r = Math.round(38 + (213 - 38) * ratio);
            const g = Math.round(50 + (0 - 50) * ratio);
            const b = Math.round(56 + (0 - 56) * ratio);
            return `rgb(${r},${g},${b})`;
        }
    }

    // Initial load
    loadHeatmap();
});
