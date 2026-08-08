import test from 'node:test';
import assert from 'node:assert/strict';

import { createUploadTransport } from '../../web/js/uploadTransport.js';
import { createPathWidgets } from '../../web/js/pathWidgets.js';
import { createLatentPreview } from '../../web/js/latentPreview.js';

function createHostContext(features) {
  const app = { ui: { settings: { getSettingValue: () => false } } };
  const api = {
    apiURL: (route) => route,
    getAuthStore: async () => null,
  };
  const fetchStub = async () => ({
    ok: true,
    json: async () => features,
  });
  return { app, api, fetchStub };
}

test('upload limit rejection returns a useful message before any upload request', async () => {
  const previous = { fetch: globalThis.fetch, alert: globalThis.alert, xhr: globalThis.XMLHttpRequest };
  const alerts = [];
  let xhrConstructed = false;
  const { app, api, fetchStub } = createHostContext({ max_upload_size: 8, assets: true });
  try {
    globalThis.fetch = fetchStub;
    globalThis.alert = (message) => alerts.push(String(message));
    globalThis.XMLHttpRequest = class { constructor() { xhrConstructed = true; } };
    const { uploadFile } = createUploadTransport({ app, api, chainCallback: () => {} });

    const result = await uploadFile(
      { name: 'large.mp4', type: 'video/mp4', size: 16 },
      undefined,
      { acceptedTypes: ['video/mp4'], label: 'Video' },
    );

    assert.equal(result, null);
    assert.equal(xhrConstructed, false);
    assert.equal(alerts.length, 1);
    assert.match(alerts[0], /exceeds the current ComfyUI upload limit/);
    assert.match(alerts[0], /16 B > 8 B/);
  } finally {
    Object.assign(globalThis, previous);
  }
});

test('asset response without a materialized filename falls back to legacy upload', async () => {
  const previous = {
    fetch: globalThis.fetch,
    alert: globalThis.alert,
    xhr: globalThis.XMLHttpRequest,
    FormData: globalThis.FormData,
    File: globalThis.File,
  };
  const routes = [];
  const responses = [
    { status: 200, json: { id: 'deduplicated', user_metadata: {} } },
    { status: 200, json: { name: 'clip.mp4', subfolder: 'input' } },
  ];
  const { app, api, fetchStub } = createHostContext({ max_upload_size: 1024, assets: true });
  try {
    globalThis.fetch = fetchStub;
    globalThis.alert = () => assert.fail('no alert expected');
    globalThis.FormData = class { append() {} };
    globalThis.File = class {
      constructor(_parts, name, options) { this.name = name; Object.assign(this, options); }
    };
    globalThis.XMLHttpRequest = class {
      constructor() { this.upload = {}; }
      open(_method, url) { routes.push(url); }
      setRequestHeader() {}
      send() {
        const response = responses.shift();
        this.status = response.status;
        this.statusText = 'OK';
        this.responseText = JSON.stringify(response.json);
        queueMicrotask(() => this.onload());
      }
    };
    const { uploadFile } = createUploadTransport({ app, api, chainCallback: () => {} });

    const result = await uploadFile(
      { name: 'clip.mp4', type: 'video/mp4', size: 12, lastModified: 1 },
      undefined,
      { acceptedTypes: ['video/mp4'], label: 'Video' },
    );

    assert.deepEqual(routes, ['/api/assets', '/upload/image']);
    assert.equal(result.route, 'legacy');
    assert.equal(result.path, 'input/clip.mp4');
  } finally {
    Object.assign(globalThis, previous);
  }
});

test('path widget domain retains precision and tail-preserving display helpers', () => {
  const { roundToPrecision, fitPath } = createPathWidgets({
    app: {}, api: {}, fetchWithOptionalAuth: async () => {}, debugLog: () => {}, LiteGraph: {},
  });
  const ctx = { measureText: (text) => ({ width: text.length }) };

  assert.equal(roundToPrecision(12.340, 3), '12.34');
  assert.equal(fitPath(ctx, '/deep/tree/clip.mp4', 12)[0].endsWith('clip.mp4'), true);
});

test('latent preview cleanup removes domain-owned widgets after a preview starts', () => {
  const previousSetInterval = globalThis.setInterval;
  const listeners = new Map();
  let removed = false;
  const node = {
    progress: undefined,
    graph: { rootGraph: {} },
    widgets: [{ name: 'vhslatentpreview', onRemove: () => { removed = true; } }],
  };
  try {
    globalThis.setInterval = () => 1;
    const controller = createLatentPreview({
      app: { canvas: { graph: { rootGraph: node.graph.rootGraph } } },
      api: { addEventListener: (name, handler) => listeners.set(name, handler) },
      getNodeById: () => node,
      allowDragFromWidget: () => {},
      fitHeight: () => {},
    });
    listeners.get('VHS_latentpreview')({ detail: { id: '7', length: 1, rate: 8 } });
    controller.clear();

    assert.equal(removed, true);
    assert.equal(node.widgets.length, 0);
  } finally {
    globalThis.setInterval = previousSetInterval;
  }
});
