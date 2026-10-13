import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

import { createMediaPreview } from '../../web/js/mediaPreview.js';
import { createPathWidgets } from '../../web/js/pathWidgets.js';
import { createLatentPreview } from '../../web/js/latentPreview.js';
import { createUploadTransport } from '../../web/js/uploadTransport.js';

const coreURL = new URL('../../web/js/VHS.core.js', import.meta.url);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const reply = (body, status = 200) => new Response(JSON.stringify(body), { status });

class FakeElement {
  constructor(tagName = 'div') {
    this.localName = tagName;
    this.children = [];
    this.style = {};
    this.listeners = new Map();
    this.queries = new Map();
    this.hidden = false;
    this.className = '';
    this.innerText = '';
    this.value = '';
    this.parentNode = null;
  }
  set innerHTML(html) { this.html = html; this.children = []; }
  get innerHTML() { return this.html ?? ''; }
  get firstChild() { return this.children[0] ?? null; }
  appendChild(child) { this.children.push(child); child.parentNode = this; return child; }
  append(...children) { for (const child of children) this.appendChild(child); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((c) => c !== this); }
  addEventListener(type, handler) { this.listeners.set(type, [...(this.listeners.get(type) ?? []), handler]); }
  removeEventListener() {}
  dispatch(type, event) { for (const handler of this.listeners.get(type) ?? []) handler(event); }
  querySelector(selector) {
    if (!this.queries.has(selector)) {
      this.queries.set(selector, new FakeElement(selector === '.value' ? 'input' : 'div'));
    }
    return this.queries.get(selector);
  }
  focus() {}
  setAttribute() {}
  getContext() { return { drawImage() {} }; }
}

function installDocument() {
  const previous = globalThis.document;
  globalThis.document = {
    createElement: (tagName) => new FakeElement(tagName),
    body: new FakeElement('body'),
    head: new FakeElement('head'),
  };
  return () => { globalThis.document = previous; };
}

function chainCallback(object, property, callback) {
  const previous = object[property];
  object[property] = previous
    ? function (...args) { const result = previous.apply(this, args); callback.apply(this, args); return result; }
    : callback;
}

const settings = {
  'VHS.AdvancedPreviews': 'Never',
  'VHS.AdvancedPreviewsDefaultMute': true,
  'VHS.AdvancedPreviewsMinWidth': 0,
  'VHS.AdvancedPreviewsDeadline': 'realtime',
};

function createPreviewNode(fetchWithOptionalAuth) {
  const { addVideoPreview } = createMediaPreview({
    app: { ui: { settings: { getSettingValue: (id) => settings[id] } }, canvas: {} },
    api: { apiURL: (route) => route },
    chainCallback,
    allowDragFromWidget: () => {},
    fitHeight: () => {},
    shouldUseAdvancedPreview: () => false,
    debugLog: () => {},
    fetchWithOptionalAuth,
  });
  class NodeType {}
  addVideoPreview(NodeType);
  const node = new NodeType();
  node.size = [300, 200];
  node.widgets = [];
  node.addDOMWidget = (name, type, element, options) => {
    const widget = { name, type, element, options };
    node.widgets.push(widget);
    return widget;
  };
  node.onNodeCreated();
  return { node, previewWidget: node.widgets.find((widget) => widget.name === 'videopreview') };
}

test('forced preview refresh is honored even when parameters are unchanged', () => {
  const restore = installDocument();
  try {
    const { node, previewWidget } = createPreviewNode(async () => reply({}));
    let refreshes = 0;
    previewWidget.updateSource = () => { refreshes += 1; };
    const params = { filename: 'same.mp4', type: 'input', format: 'video/mp4' };

    node.updateParameters(params, true);
    node.updateParameters({ ...params }, true);

    assert.equal(refreshes, 2);
  } finally {
    restore();
  }
});

test('a stale metadata response cannot overwrite the latest preview query', async () => {
  const restore = installDocument();
  try {
    const pending = [];
    const { node, previewWidget } = createPreviewNode((route) => new Promise((resolve) => pending.push({ route, resolve })));

    previewWidget.value.params = { filename: 'first.mp4', type: 'input', format: 'video/mp4' };
    previewWidget.updateSource();
    previewWidget.value.params = { filename: 'second.mp4', type: 'input', format: 'video/mp4' };
    previewWidget.updateSource();
    assert.equal(pending.length, 2);

    pending[1].resolve(reply({ source: { name: 'second' } }));
    await sleep(5);
    pending[0].resolve(reply({ error: 'first failed late' }, 500));
    await sleep(5);

    assert.deepEqual(node.video_query, { source: { name: 'second' } });
    assert.equal(previewWidget.value.error, '');
  } finally {
    restore();
  }
});

function createSearchContext() {
  const app = {
    canvas: {
      ds: { scale: 1 },
      canvas: { width: 800, height: 600, getBoundingClientRect: () => ({ left: 0, top: 0 }) },
    },
  };
  const widgets = createPathWidgets({
    app, api: {}, fetchWithOptionalAuth: async () => reply([]), debugLog: () => {}, LiteGraph: {},
  });
  const widget = { value: '/contained/', options: { vhs_path_extensions: ['mp4'] }, prompt: false };
  const node = { graph: { setDirtyCanvas() {} }, size: [300, 100] };
  return { app, widgets, widget, node };
}

test('the path dialog opens without a pointer event using the host canvas size', async () => {
  const restore = installDocument();
  try {
    const { widgets, widget, node } = createSearchContext();
    const dialog = widgets.searchBox.call(widget, undefined, [0, 0], node);
    assert.equal(dialog.style.left, '380px');
    assert.equal(dialog.style.top, '280px');
    await sleep(20);
  } finally {
    restore();
  }
});

test('Tab with no listed options keeps the typed path', async () => {
  const restore = installDocument();
  try {
    const { widgets, widget, node } = createSearchContext();
    const dialog = widgets.searchBox.call(widget, { clientX: 40, clientY: 40 }, [0, 0], node);
    const input = dialog.querySelector('.value');
    input.value = '/contained/cl';
    let prevented = false;
    input.dispatch('keydown', {
      keyCode: 9, target: { localName: 'input' },
      preventDefault() { prevented = true; }, stopPropagation() {},
    });
    assert.equal(input.value, '/contained/cl');
    assert.equal(prevented, true);
    await sleep(20);
  } finally {
    restore();
  }
});

test('annotated number changes pass the originating event to the widget callback', async () => {
  const { widgets, node } = createSearchContext();
  const calls = [];
  const widget = {
    value: 5, width: 200, type: 'VHS.ANNOTATED', options: { step: 1 },
    callback: (...args) => calls.push(args),
  };
  const event = { type: 'pointerdown' };
  widgets.mouseAnnotated.call(widget, event, [200 - 15 - 10, 5], node);
  await sleep(40);
  assert.equal(widget.value, 6);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][3], event);
});

test('latent preview frames for an unknown id are ignored', async () => {
  const previous = { setInterval: globalThis.setInterval, window: globalThis.window };
  const listeners = new Map();
  let decoded = 0;
  try {
    globalThis.setInterval = () => 1;
    globalThis.window = { createImageBitmap: async () => { decoded += 1; return {}; } };
    const node = { graph: { rootGraph: {} }, widgets: [] };
    createLatentPreview({
      app: { canvas: { graph: { rootGraph: node.graph.rootGraph } } },
      api: { addEventListener: (name, handler) => listeners.set(name, handler) },
      getNodeById: () => node,
      allowDragFromWidget: () => {},
      fitHeight: () => {},
    });
    listeners.get('VHS_latentpreview')({ detail: { id: '7', length: 1, rate: 8 } });

    const id = new TextEncoder().encode('99');
    const header = new Uint8Array(24);
    new DataView(header.buffer).setUint32(4, 0);
    header[8] = id.length;
    header.set(id, 9);
    const event = {
      detail: new Blob([header, new Uint8Array([1, 2, 3])]),
      preventDefault() {}, stopImmediatePropagation() {}, stopPropagation() {},
    };
    await assert.doesNotReject(async () => listeners.get('b_preview')(event));
    assert.equal(decoded, 0);
  } finally {
    globalThis.setInterval = previous.setInterval;
    globalThis.window = previous.window;
  }
});

test('asset uploads use the route-relative /assets endpoint owned by the host', async () => {
  const previous = globalThis.alert;
  const writes = [];
  globalThis.alert = () => {};
  try {
    const { uploadFile } = createUploadTransport({
      app: { ui: { settings: { getSettingValue: () => false } } },
      api: { fetchApi: async (route, request) => {
        if (route === '/features') return reply({ assets: true });
        writes.push(route);
        return reply({ loader_path: 'clip.mp4', tags: ['input'] });
      } },
      chainCallback() {},
    });
    const result = await uploadFile(new File(['synthetic'], 'clip.mp4', { type: 'video/mp4' }));
    assert.equal(result.path, 'clip.mp4');
    assert.deepEqual(writes, ['/assets']);
  } finally {
    globalThis.alert = previous;
  }
});

let coreNonce = 0;
async function loadCore() {
  const extensions = [];
  const listeners = new Map();
  class LGraphNode {}
  const LiteGraph = {
    LGraphNode,
    INPUT: 1,
    OUTPUT: 2,
    alwaysSnapToGrid: false,
    isValidConnection: (from, to) => from === to || from === '*' || to === '*',
  };
  const app = {
    registerExtension: (extension) => extensions.push(extension),
    ui: { settings: { getSettingValue: (id) => settings[id] } },
    canvas: { selectedItems: new Set(), graph: null },
    graph: { links: {}, getNodeById: () => null, subgraphs: new Map(), _nodes: [] },
  };
  const api = {
    addEventListener: (name, handler) => listeners.set(name, handler),
    apiURL: (route) => route,
    fetchApi: async () => reply({}),
  };
  const previous = { app: globalThis.__vhsCoreTestApp, api: globalThis.__vhsCoreTestApi, LiteGraph: globalThis.LiteGraph };
  globalThis.__vhsCoreTestApp = app;
  globalThis.__vhsCoreTestApi = api;
  globalThis.LiteGraph = LiteGraph;
  const restoreDocument = installDocument();
  let source = await readFile(coreURL, 'utf8');
  source = source.replace("import { app } from '../../../scripts/app.js'", 'const app = globalThis.__vhsCoreTestApp;');
  source = source.replace("import { api } from '../../../scripts/api.js'", 'const api = globalThis.__vhsCoreTestApi;');
  source = source.replace(/from (['"])(\.\/[^'"]+)\1/g, (_match, _quote, relative) => `from ${JSON.stringify(new URL(relative, coreURL).href)}`);
  await import(`data:text/javascript;base64,${Buffer.from(`${source}\n// core subject ${coreNonce++}`).toString('base64')}`);
  return {
    app, api, LiteGraph, listeners, extension: extensions[0],
    restore() {
      restoreDocument();
      globalThis.__vhsCoreTestApp = previous.app;
      globalThis.__vhsCoreTestApi = previous.api;
      globalThis.LiteGraph = previous.LiteGraph;
    },
  };
}

async function createCoreNode(core, nodeData, widgets, fields = {}) {
  class NodeType extends core.LiteGraph.LGraphNode {}
  NodeType.nodeData = nodeData;
  await core.extension.beforeRegisterNodeDef(NodeType, nodeData, core.app);
  const node = new NodeType();
  Object.assign(node, {
    size: [300, 200],
    inputs: [],
    outputs: [{ name: 'IMAGE', type: 'IMAGE', links: null }],
    graph: core.app.graph,
    widgets: widgets.map(([name, value, type = 'number', options = {}]) => ({ name, value, type, options })),
    addDOMWidget(name, type, element, options) {
      const widget = { name, type, element, options };
      this.widgets.push(widget);
      return widget;
    },
    setSize() {},
    computeSize() { return [300, 200]; },
    setDirtyCanvas() {},
  }, fields);
  node.onNodeCreated?.();
  return node;
}

const loadImagePathData = () => ({
  name: 'VHS_LoadImagePath',
  input: { required: { image: ['STRING', {}], custom_width: ['INT', {}], custom_height: ['INT', {}] } },
});
const loadImagePathWidgets = [['image', '', 'text'], ['custom_width', 0], ['custom_height', 0]];

test('load dimension changes refresh the preview whenever the aspect ratio changes', async () => {
  const core = await loadCore();
  try {
    const node = await createCoreNode(core, loadImagePathData(), loadImagePathWidgets);
    const updates = [];
    node.updateParameters = (params) => updates.push(params);
    const width = node.widgets.find((widget) => widget.name === 'custom_width');
    const height = node.widgets.find((widget) => widget.name === 'custom_height');

    width.value = 512;
    height.value = 256;
    width.callback(512);

    assert.deepEqual(updates, [{ custom_width: 512, custom_height: 256 }]);
  } finally {
    core.restore();
  }
});

test('dragging from a VHS widget reports the first selected node to the host', async () => {
  const core = await loadCore();
  try {
    const node = await createCoreNode(core, loadImagePathData(), loadImagePathWidgets);
    const moved = [];
    core.app.canvas.onNodeMoved = (target) => moved.push(target);
    core.app.canvas.selectedItems = new Set([{ id: 'group' }, node]);
    const preview = node.widgets.find((widget) => widget.name === 'videopreview');
    const pointer = {};
    preview.onPointerDown(pointer, node);

    pointer.onDragEnd({ shiftKey: false });

    assert.deepEqual(moved, [node]);
  } finally {
    core.restore();
  }
});

test('type cloning resolves links inside the node graph, including subgraphs', async () => {
  const core = await loadCore();
  try {
    const disconnected = [];
    const subgraph = {
      links: { 11: { target_id: 9, target_slot: 0, disconnect: (graph) => disconnected.push(graph) } },
      getNodeById: (id) => ({
        5: { outputs: [{ type: 'LATENT' }] },
        9: { inputs: [{ type: 'IMAGE' }], onConnectionsChange() {} },
      })[id],
    };
    const node = await createCoreNode(core, { name: 'VHS_Unbatch', input: { required: {} } }, [], {
      graph: subgraph,
      outputs: [{ name: '*', type: '*', links: null }],
    });
    await sleep(80);
    node.outputs[0].links = [11];

    node.onConnectionsChange(core.LiteGraph.INPUT, 0, true, { origin_id: 5, origin_slot: 0 });
    await sleep(80);

    assert.equal(node.outputs[0].type, 'LATENT');
    assert.deepEqual(disconnected, [subgraph]);
    assert.deepEqual(node.outputs[0].links, []);
  } finally {
    core.restore();
  }
});

test('pruning tolerates the host clearing output links during disconnect', async () => {
  const core = await loadCore();
  try {
    let node;
    const graph = {
      links: { 12: { target_id: 9, target_slot: 0, disconnect: () => { node.outputs[0].links = null; } } },
      getNodeById: (id) => ({
        5: { outputs: [{ type: 'LATENT' }] },
        9: { inputs: [{ type: 'IMAGE' }], onConnectionsChange() {} },
      })[id],
    };
    node = await createCoreNode(core, { name: 'VHS_Unbatch', input: { required: {} } }, [], {
      graph,
      outputs: [{ name: '*', type: '*', links: null }],
    });
    await sleep(80);
    node.outputs[0].links = [12];
    const failures = [];
    const onFailure = (error) => failures.push(error);
    process.on('uncaughtException', onFailure);
    try {
      node.onConnectionsChange(core.LiteGraph.INPUT, 0, true, { origin_id: 5, origin_slot: 0 });
      await sleep(80);
    } finally {
      process.off('uncaughtException', onFailure);
    }
    assert.deepEqual(failures, []);
    assert.equal(node.outputs[0].type, 'LATENT');
    assert.equal(node.outputs[0].links, null);
  } finally {
    core.restore();
  }
});

test('node creation tolerates missing input groups and option-less configs', async () => {
  const core = await loadCore();
  try {
    const optionalOnly = await createCoreNode(core, { name: 'VHS_TestOptional', input: { optional: { label: ['STRING'] } } },
      [['label', '', 'text']]);
    assert.deepEqual(optionalOnly.widgets.map((widget) => widget.name), ['label']);

    const requiredOnly = await createCoreNode(core, { name: 'VHS_TestRequired', input: { required: { label: ['STRING'] } } },
      [['label', '', 'text']]);
    assert.deepEqual(requiredOnly.widgets.map((widget) => widget.name), ['label']);
  } finally {
    core.restore();
  }
});

test('VHS integer widgets round with a default step of one', async () => {
  const core = await loadCore();
  try {
    const custom = await core.extension.getCustomWidgets();
    const node = { widgets: [] };
    const unstepped = custom.VHSINT(node, 'count', ['INT', {}]);
    unstepped.callback(7);
    assert.equal(unstepped.value, 7);

    const stepped = custom.VHSINT(node, 'stride', ['INT', { step: 4 }]);
    stepped.callback(7);
    assert.equal(stepped.value, 8);
  } finally {
    core.restore();
  }
});
