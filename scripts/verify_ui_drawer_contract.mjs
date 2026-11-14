import { readFileSync } from "node:fs";
import vm from "node:vm";

// UI のドロワー移動(reparent)契約の回帰ゲート。
//
// 銘柄詳細ドロワーが開くと openStockDetailDrawer() は .chart-container を
// #drawerTabChartContent 配下へ移動する。したがって「ラッパー直下だけ」を
// 見るヘルパはドロワー開示中に null を返し、チャートのエラー/情報バナーが
// 不可視になる(またはドロワー内に取り残されて消せなくなる)。実ソースから
// 該当関数を取り出し、最小のフェイク DOM 上で両方向の挙動を検証する。

const UI_PATH = "static/js/ui.js";
const CHART_PATH = "static/js/chart.js";

function fail(message) {
  throw new Error(message);
}

function extractFunction(source, name, sourcePath) {
  const marker = `function ${name}(`;
  const start = source.indexOf(marker);
  if (start === -1) fail(`${sourcePath} no longer defines ${name}()`);
  const open = source.indexOf("{", start);
  if (open === -1) fail(`${sourcePath} has no body for ${name}()`);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    const char = source[index];
    if (char === "{") {
      depth += 1;
    } else if (char === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  return fail(`${sourcePath} has unbalanced braces in ${name}()`);
}

function requireClassSelector(selector) {
  const name = /^\.([\w-]+)$/.exec(selector);
  if (!name) fail(`fake DOM does not support selector '${selector}'`);
  return name[1];
}

class FakeClassList {
  constructor() {
    this.names = new Set();
  }

  add(...names) {
    for (const name of names) this.names.add(name);
  }

  remove(...names) {
    for (const name of names) this.names.delete(name);
  }

  contains(name) {
    return this.names.has(name);
  }

  values() {
    return [...this.names];
  }

  replace(names) {
    this.names = new Set(names);
  }
}

class FakeElement {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.parentNode = null;
    this.children = [];
    this.dataset = {};
    this.attributes = new Map();
    this.listeners = new Map();
    this.textContent = "";
    this.classList = new FakeClassList();
  }

  get className() {
    return this.classList.values().join(" ");
  }

  set className(value) {
    this.classList.replace(
      String(value)
        .split(/\s+/)
        .filter((name) => name.length > 0),
    );
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(handler);
  }

  appendChild(child) {
    if (child.parentNode) child.parentNode.detachChild(child);
    child.parentNode = this;
    this.children.push(child);
    return child;
  }

  detachChild(child) {
    const index = this.children.indexOf(child);
    if (index !== -1) this.children.splice(index, 1);
    child.parentNode = null;
  }

  remove() {
    if (this.parentNode) this.parentNode.detachChild(this);
  }

  hasClass(name) {
    return this.classList.contains(name);
  }

  querySelector(selector) {
    const name = requireClassSelector(selector);
    let found = null;
    const walk = (node) => {
      for (const child of node.children) {
        if (found) return;
        if (child.hasClass(name)) {
          found = child;
          return;
        }
        walk(child);
      }
    };
    walk(this);
    return found;
  }
}

function createFakeDocument() {
  const byId = new Map();
  const document = {
    createElement: (tag) => new FakeElement(tag),
    getElementById: (id) => byId.get(id) ?? null,
    querySelector: (selector) => {
      const descendant = /^#([\w-]+)\s+(\.?[\w-]+)$/.exec(selector);
      if (!descendant) {
        fail(`fake DOM does not support selector '${selector}'`);
      }
      const root = byId.get(descendant[1]);
      if (!root) return null;
      return root.querySelector(descendant[2]);
    },
  };
  return { byId, document };
}

function findByClass(root, className) {
  let found = null;
  const walk = (node) => {
    for (const child of node.children) {
      if (found) return;
      if (child.hasClass(className)) {
        found = child;
        return;
      }
      walk(child);
    }
  };
  walk(root);
  return found;
}

const uiSource = readFileSync(UI_PATH, "utf8");
const chartSource = readFileSync(CHART_PATH, "utf8");

function loadWidget() {
  const { byId, document } = createFakeDocument();
  const chartInstances = new WeakMap();
  const sandbox = {
    document,
    chartInstances,
    currentDrawerActiveWrapper: null,
    console: { warn: () => {}, log: () => {}, error: () => {} },
    setTimeout,
    clearTimeout,
  };
  const context = vm.createContext(sandbox);
  vm.runInContext(
    [
      extractFunction(chartSource, "createEl", CHART_PATH),
      extractFunction(uiSource, "destroyChart", UI_PATH),
      extractFunction(uiSource, "chartErrorHosts", UI_PATH),
      extractFunction(uiSource, "clearChartError", UI_PATH),
      extractFunction(uiSource, "showChartError", UI_PATH),
    ].join("\n\n"),
    context,
    { filename: "ui-chart-error-contract.js" },
  );
  const api = vm.runInContext(
    "({ showChartError, clearChartError, chartErrorHosts })",
    context,
  );
  return { api, sandbox, byId, chartInstances };
}

function buildWorld(options = {}) {
  const {
    wrapperHasContainer = true,
    drawerOpen = true,
    drawerStockKey = "AAPL",
    wrapperStockKey = "AAPL",
    linkActiveWrapper = false,
  } = options;

  const widget = loadWidget();
  const wrapper = new FakeElement("div");
  wrapper.className = "stock-wrapper";
  if (wrapperStockKey !== null) wrapper.dataset.stockKey = wrapperStockKey;

  let inlineContainer = null;
  let inlineCanvas = null;
  if (wrapperHasContainer) {
    inlineContainer = new FakeElement("div");
    inlineContainer.className = "chart-container";
    wrapper.appendChild(inlineContainer);
    inlineCanvas = new FakeElement("canvas");
    inlineCanvas.className = "chart-canvas";
    inlineContainer.appendChild(inlineCanvas);
  }

  const drawer = new FakeElement("div");
  drawer.className = "stock-detail-drawer";
  widget.byId.set("stock-detail-drawer", drawer);
  if (drawerOpen) drawer.classList.add("open");
  if (drawerStockKey !== null) drawer.dataset.stockKey = drawerStockKey;

  const drawerTab = new FakeElement("div");
  drawerTab.className = "drawer-tab-content";
  widget.byId.set("drawerTabChartContent", drawerTab);
  drawer.appendChild(drawerTab);

  const drawerContainer = new FakeElement("div");
  drawerContainer.className = "chart-container";
  drawerTab.appendChild(drawerContainer);
  const drawerCanvas = new FakeElement("canvas");
  drawerCanvas.className = "chart-canvas";
  drawerContainer.appendChild(drawerCanvas);

  if (linkActiveWrapper) {
    widget.sandbox.currentDrawerActiveWrapper = wrapper;
  }

  return {
    ...widget,
    wrapper,
    inlineContainer,
    inlineCanvas,
    drawer,
    drawerContainer,
    drawerCanvas,
  };
}

function expect(condition, message) {
  if (!condition) fail(message);
}

const scenarios = [];

scenarios.push(() => {
  // 1. ドロワー開示中(ラッパー側にコンテナが無い)でも可視コンテナへ出る。
  const world = buildWorld({ wrapperHasContainer: false });
  world.api.showChartError(world.wrapper, "取得に失敗しました", "error");
  const banner = findByClass(world.drawerContainer, "chart-error");
  expect(banner, "drawer-open error banner was not rendered into the drawer");
  expect(banner.hasClass("error"), "error banner lost its severity class");
  const message = findByClass(banner, "chart-error-msg");
  expect(message, "error banner has no message node");
  expect(
    message.textContent === "取得に失敗しました",
    `error banner message mismatch: ${message.textContent}`,
  );
  expect(
    !findByClass(world.wrapper, "chart-error"),
    "drawer-open error banner leaked into the hidden inline panel",
  );
});

scenarios.push(() => {
  // 2. ドロワー側の canvas インスタンスも破棄される(リーク防止)。
  const world = buildWorld({ wrapperHasContainer: false });
  const instance = {
    destroyed: false,
    destroy() {
      this.destroyed = true;
    },
  };
  world.chartInstances.set(world.drawerCanvas, instance);
  world.api.showChartError(world.wrapper, "通信エラー", "error");
  expect(instance.destroyed, "drawer chart instance was not destroyed");
  expect(
    world.chartInstances.has(world.drawerCanvas) === false,
    "drawer chart instance stayed registered after the error",
  );
});

scenarios.push(() => {
  // 3. 移動で取り残された stale バナーを両側から掃除できる。
  const world = buildWorld({ wrapperHasContainer: true, drawerOpen: true });
  const staleInline = new FakeElement("div");
  staleInline.className = "chart-error error";
  world.inlineContainer.appendChild(staleInline);
  const staleDrawer = new FakeElement("div");
  staleDrawer.className = "chart-error info";
  world.drawerContainer.appendChild(staleDrawer);
  world.api.clearChartError(world.wrapper);
  expect(
    !findByClass(world.inlineContainer, "chart-error"),
    "clearChartError left a stale banner in the inline container",
  );
  expect(
    !findByClass(world.drawerContainer, "chart-error"),
    "clearChartError left a stale banner in the drawer container",
  );
});

scenarios.push(() => {
  // 4. 別銘柄のドロワーが開いているときは何もしない(誤爆防止)。
  const world = buildWorld({
    wrapperHasContainer: false,
    drawerOpen: true,
    drawerStockKey: "MSFT",
    wrapperStockKey: "AAPL",
  });
  world.api.showChartError(world.wrapper, "取得に失敗しました", "error");
  expect(
    !findByClass(world.drawerContainer, "chart-error"),
    "an unrelated drawer received another stock's error banner",
  );
});

scenarios.push(() => {
  // 5. ドロワー閉示中は従来どおりラッパー直下へ出る(後方互換)。
  const world = buildWorld({ wrapperHasContainer: true, drawerOpen: false });
  world.api.showChartError(world.wrapper, "データを読み込み中です...", "info");
  const banner = findByClass(world.inlineContainer, "chart-error");
  expect(banner, "drawer-closed info banner was not rendered inline");
  expect(banner.hasClass("info"), "info banner lost its severity class");
  expect(
    !findByClass(world.drawerContainer, "chart-error"),
    "drawer-closed banner leaked into the drawer",
  );
});

scenarios.push(() => {
  // 6. stockKey 未設定どうしで誤マッチしない。
  const world = buildWorld({
    wrapperHasContainer: false,
    drawerOpen: true,
    drawerStockKey: null,
    wrapperStockKey: null,
  });
  expect(
    world.api.chartErrorHosts(world.wrapper).length === 0,
    "keyless wrapper matched a keyless drawer",
  );
  world.api.showChartError(world.wrapper, "取得に失敗しました", "error");
  expect(
    !findByClass(world.drawerContainer, "chart-error"),
    "keyless drawer received a banner it does not own",
  );
});

scenarios.push(() => {
  // 7. currentDrawerActiveWrapper との一致だけでもドロワーへ追従する。
  const world = buildWorld({
    wrapperHasContainer: false,
    drawerOpen: false,
    linkActiveWrapper: true,
  });
  world.api.showChartError(world.wrapper, "取得に失敗しました", "error");
  expect(
    findByClass(world.drawerContainer, "chart-error"),
    "active-wrapper link did not route the banner into the drawer",
  );
});

let failed = 0;
for (const [index, scenario] of scenarios.entries()) {
  try {
    scenario();
  } catch (error) {
    failed += 1;
    console.error(`scenario ${index + 1} failed: ${error.message}`);
  }
}

if (failed > 0) {
  console.error(`ui drawer contract: ${failed} scenario(s) failed`);
  process.exit(1);
}

console.log("ui.js chart error banners follow the active stock detail drawer.");
