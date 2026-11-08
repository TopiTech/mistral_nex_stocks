"""Regression test for 3D heatmap viewport sizing.

``init3DScene`` runs while ``#heatmap-3d-canvas`` still carries ``hidden``
(``display: none``), so ``clientWidth``/``clientHeight`` are 0 and the renderer
falls back to 1000x600 regardless of the real container. Re-entering 3D after
resizing the window in 2D mode kept the stale size as well, because the resize
handler only updates the renderer while 3D is the active view.

The fix syncs the renderer/camera to the canvas' real size once the canvas is
visible, on both first entry and re-entry.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_SCRIPT = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const elements = new Map();
const threeCalls = { setSize: [], cameras: [] };

function makeEl(id, opts = {}) {
  const listeners = new Map();
  const classes = new Set(opts.classes || []);
  const el = {
    id,
    tagName: id,
    children: [],
    style: { setProperty() {} },
    dataset: {},
    textContent: "",
    title: "",
    _width: opts.width || 0,
    _height: opts.height || 0,
    get clientWidth() { return classes.has("hidden") ? 0 : this._width; },
    get clientHeight() { return classes.has("hidden") ? 0 : this._height; },
    classList: {
      add: (...cs) => cs.forEach((c) => classes.add(c)),
      remove: (...cs) => cs.forEach((c) => classes.delete(c)),
      toggle: (c, force) => {
        const on = force === undefined ? !classes.has(c) : !!force;
        if (on) classes.add(c); else classes.delete(c);
        return on;
      },
      contains: (c) => classes.has(c),
    },
    setAttribute() {},
    removeAttribute() {},
    getAttribute() { return null; },
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    removeEventListener(type, fn) {
      const arr = listeners.get(type);
      if (!arr) return;
      const i = arr.indexOf(fn);
      if (i >= 0) arr.splice(i, 1);
    },
    click() {
      (listeners.get("click") || []).forEach((fn) =>
        fn({ target: el, preventDefault() {} }),
      );
    },
    appendChild(child) { this.children.push(child); return child; },
    append(...kids) { this.children.push(...kids); },
    replaceChildren(...kids) { this.children = [...kids]; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    getBoundingClientRect() {
      return { width: this._width, height: this._height, left: 0, top: 0 };
    },
    focus() {},
    closest() { return null; },
  };
  return el;
}

const canvas2d = makeEl("heatmap-canvas", { width: 900, height: 600 });
const canvas3d = makeEl("heatmap-3d-canvas", {
  width: 800,
  height: 500,
  classes: ["hidden"],
});
elements.set("heatmap-canvas", canvas2d);
elements.set("heatmap-3d-canvas", canvas3d);

const document = {
  body: makeEl("body"),
  activeElement: null,
  _ready: null,
  addEventListener(type, fn) {
    if (type === "DOMContentLoaded") this._ready = fn;
  },
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, makeEl(id));
    return elements.get(id);
  },
  createElement(tag) { return makeEl(tag); },
  querySelector() { return null; },
  querySelectorAll() { return []; },
};

const window = {
  innerWidth: 1280,
  innerHeight: 900,
  devicePixelRatio: 2,
  addEventListener() {},
  removeEventListener() {},
};

class WebGLRenderer {
  constructor() {
    this.domElement = makeEl("three-canvas");
    this.shadowMap = {};
  }
  setSize(w, h) { threeCalls.setSize.push([w, h]); }
  setPixelRatio() {}
  render() {}
  dispose() {}
}

class PerspectiveCamera {
  constructor(fov, aspect) {
    this.aspect = aspect;
    this.position = { set() {} };
    this.updateProjectionMatrix = () => {};
    threeCalls.cameras.push(this);
  }
}

class Vec3 { set() {} }
class Scene {
  constructor() { this.children = []; }
  add(obj) { this.children.push(obj); }
  remove(obj) {
    const i = this.children.indexOf(obj);
    if (i >= 0) this.children.splice(i, 1);
  }
}
class Color {
  constructor(hex) { this.hex = hex; }
  lerpColors() { return this; }
  getHex() { return this.hex || 0; }
}
class Mesh {
  constructor(geometry, material) {
    this.geometry = geometry;
    this.material = material;
    this.position = new Vec3();
    this.children = [];
    this.userData = {};
  }
  add(child) { this.children.push(child); }
  remove(child) {
    const i = this.children.indexOf(child);
    if (i >= 0) this.children.splice(i, 1);
  }
}
class Geometry { dispose() {} }
class Material { dispose() {} }
class LineSegments extends Mesh {}
class Light {
  constructor() {
    this.position = new Vec3();
    this.castShadow = false;
  }
}
class Raycaster {
  setFromCamera() {}
  intersectObjects() { return []; }
}
class Vector2 {}

const THREE = {
  Scene,
  Color,
  FogExp2: function FogExp2() {},
  PerspectiveCamera,
  WebGLRenderer,
  AmbientLight: Light,
  DirectionalLight: Light,
  GridHelper: function GridHelper() { this.position = new Vec3(); },
  Raycaster,
  Vector2,
  PCFSoftShadowMap: 1,
  BoxGeometry: Geometry,
  EdgesGeometry: Geometry,
  MeshStandardMaterial: Material,
  LineBasicMaterial: Material,
  Mesh,
  LineSegments,
};

class AbortController {
  constructor() { this.signal = { aborted: false }; }
  abort() { this.signal.aborted = true; }
}

const context = {
  console,
  AbortController,
  document,
  window,
  THREE,
  Map,
  Set,
  Math,
  Number,
  JSON,
  Date,
  Promise,
  Error,
  setTimeout,
  clearTimeout,
  setImmediate,
  requestAnimationFrame: () => 1,
  cancelAnimationFrame: () => {},
  apiFetch: async () => ({ data: { stocks: [] } }),
};
context.globalThis = context;
context.self = context;
vm.createContext(context);
vm.runInContext(
  fs.readFileSync(path.join(process.cwd(), "static", "js", "heatmap.js"), "utf8"),
  context,
  { filename: "heatmap.js" },
);

function lastSize() {
  return threeCalls.setSize[threeCalls.setSize.length - 1] || [0, 0];
}

(async () => {
  if (typeof document._ready !== "function") {
    throw new Error("heatmap.js did not register DOMContentLoaded");
  }
  document._ready();
  await new Promise((resolve) => setImmediate(resolve));

  const view3d = elements.get("view-3d");
  const view2d = elements.get("view-2d");

  // First entry: the canvas is hidden until the switch completes, so the
  // renderer must be resized to the real 800x500 container, not 1000x600.
  view3d.click();
  await new Promise((resolve) => setImmediate(resolve));
  let size = lastSize();
  if (size[0] !== 800 || size[1] !== 500) {
    throw new Error(`first 3D entry sized ${size[0]}x${size[1]}, expected 800x500`);
  }
  const aspect1 = threeCalls.cameras[threeCalls.cameras.length - 1].aspect;
  if (Math.abs(aspect1 - 800 / 500) > 1e-9) {
    throw new Error(`first 3D entry aspect ${aspect1}, expected ${800 / 500}`);
  }

  // Resize while 2D is active, then re-enter 3D: the renderer must follow.
  view2d.click();
  canvas3d._width = 640;
  canvas3d._height = 480;
  view3d.click();
  await new Promise((resolve) => setImmediate(resolve));
  size = lastSize();
  if (size[0] !== 640 || size[1] !== 480) {
    throw new Error(`re-entry sized ${size[0]}x${size[1]}, expected 640x480`);
  }
  const aspect2 = threeCalls.cameras[threeCalls.cameras.length - 1].aspect;
  if (Math.abs(aspect2 - 640 / 480) > 1e-9) {
    throw new Error(`re-entry aspect ${aspect2}, expected ${640 / 480}`);
  }
  process.stdout.write("ok");
})().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
"""


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the heatmap 3D viewport test")
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT,
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_3d_heatmap_syncs_viewport_size_on_entry_and_reentry() -> None:
    assert _run_node(_SCRIPT) == "ok"
