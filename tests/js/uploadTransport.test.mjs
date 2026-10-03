import test from 'node:test';
import assert from 'node:assert/strict';
import { createUploadTransport } from '../../web/js/uploadTransport.js';

function transport(fetchApi) {
  return createUploadTransport({
    app: { ui: { settings: { getSettingValue: () => false } } },
    api: {
      fetchApi,
      apiURL: () => { throw new Error('Request routes must not be expanded'); },
      getAuthStore: () => { throw new Error('Private auth access is forbidden'); },
    },
    chainCallback: () => {},
  });
}

function response(body, status = 200) {
  return new Response(JSON.stringify(body), { status, statusText: status === 200 ? 'OK' : 'Denied' });
}

test('query transport delegates relative routes, headers and abort signal to the host', async () => {
  const controller = new AbortController();
  const headers = new Headers({ 'Accept': 'application/json' });
  const result = response({ width: 64 });
  const { fetchWithOptionalAuth } = transport(async (route, options) => {
    assert.equal(route, '/vhs/queryvideo?filename=clip.mp4');
    assert.equal(options.headers, headers);
    assert.equal(options.signal, controller.signal);
    return result;
  });
  assert.equal(await fetchWithOptionalAuth('/vhs/queryvideo?filename=clip.mp4', { headers, signal: controller.signal }), result);
});

test('uploads use host FormData transport with indeterminate progress and cleanup', async () => {
  const previousAlert = globalThis.alert;
  globalThis.alert = () => assert.fail('Successful upload must not alert');
  const calls = [];
  const progress = [];
  const controller = new AbortController();
  try {
    const { uploadFile } = transport(async (route, options) => {
      calls.push(route);
      assert.equal(options?.signal, controller.signal);
      if (route === '/features') return response({ assets: false, max_upload_size: 1024 });
      assert.equal(route, '/upload/image');
      assert.equal(options.method, 'POST');
      assert.ok(options.body instanceof FormData);
      assert.equal(options.body.get('image').name, 'clip.mp4');
      assert.equal(options.headers, undefined);
      assert.equal(progress.at(-1), null);
      return response({ name: 'clip.mp4', subfolder: '' });
    });
    const result = await uploadFile(new File(['fixture'], 'clip.mp4', { type: 'video/mp4' }), value => progress.push(value), { signal: controller.signal });
    assert.equal(result.path, 'clip.mp4');
    assert.deepEqual(calls, ['/features', '/upload/image']);
    assert.deepEqual(progress, [null, 1, undefined]);
  } finally {
    globalThis.alert = previousAlert;
  }
});

test('feature data changes between operations and is never cached across host contexts', async () => {
  const previousAlert = globalThis.alert;
  const alerts = [];
  globalThis.alert = value => alerts.push(String(value));
  let reads = 0;
  try {
    const { uploadFile } = transport(async route => {
      if (route === '/features') return response({ max_upload_size: ++reads === 1 ? 100 : 1 });
      assert.equal(route, '/upload/image');
      return response({ name: 'clip.mp4' });
    });
    const file = new File(['fixture'], 'clip.mp4', { type: 'video/mp4' });
    assert.equal((await uploadFile(file)).path, 'clip.mp4');
    assert.equal(await uploadFile(file), null);
    assert.match(alerts[0], /upload limit/);
    assert.equal(reads, 2);
  } finally {
    globalThis.alert = previousAlert;
  }
});

test('feature authorization failure stops the operation without an upload or fallback', async () => {
  const previousAlert = globalThis.alert;
  const alerts = [];
  globalThis.alert = value => alerts.push(String(value));
  const calls = [];
  const progress = [];
  try {
    const { uploadFile } = transport(async route => {
      calls.push(route);
      return response({}, 403);
    });
    assert.equal(await uploadFile(new File(['fixture'], 'clip.mp4'), value => progress.push(value)), undefined);
    assert.deepEqual(calls, ['/features']);
    assert.match(alerts[0], /403/);
    assert.equal(progress.at(-1), undefined);
  } finally {
    globalThis.alert = previousAlert;
  }
});

test('malformed feature responses fail before any upload is submitted', async () => {
  const previousAlert = globalThis.alert;
  globalThis.alert = () => {};
  try {
    for (const features of ['unexpected', null, []]) {
      const calls = [];
      const { uploadFile } = transport(async route => {
        calls.push(route);
        return response(route === '/features' ? features : { name: 'clip.mp4' });
      });
      assert.equal(await uploadFile(new File(['fixture'], 'clip.mp4')), undefined);
      assert.deepEqual(calls, ['/features']);
    }
  } finally {
    globalThis.alert = previousAlert;
  }
});

test('host cancellation propagates without retry and clears upload activity', async () => {
  const previousAlert = globalThis.alert;
  globalThis.alert = () => {};
  const calls = [];
  const progress = [];
  try {
    const { uploadFile } = transport(async route => {
      calls.push(route);
      if (route === '/features') return response({ assets: false });
      throw new DOMException('Aborted', 'AbortError');
    });
    assert.equal(await uploadFile(new File(['fixture'], 'clip.mp4'), value => progress.push(value)), undefined);
    assert.deepEqual(calls, ['/features', '/upload/image']);
    assert.deepEqual(progress, [null, undefined]);
  } finally {
    globalThis.alert = previousAlert;
  }
});

test('upload widget shows activity, prevents concurrent writes and aborts on node removal', async () => {
  const previous = { document: globalThis.document, alert: globalThis.alert };
  let input;
  globalThis.document = {
    createElement: () => (input = { files: [], remove() {}, click() {} }),
    body: { append() {} },
  };
  globalThis.alert = () => assert.fail('Cancellation should not alert');
  let started;
  const ready = new Promise(resolve => { started = resolve; });
  let writes = 0;
  let signal;
  try {
    class Node {
      widgets = [{ name: 'video', value: '', options: { values: [] } }];
      addWidget(_type, name, _value, callback) {
        const widget = { name, callback, options: {} };
        this.widgets.push(widget);
        return widget;
      }
    }
    const controller = createUploadTransport({
      app: { canvas: {}, ui: { settings: { getSettingValue: () => false } } },
      api: { fetchApi: async (route, options) => {
        if (route === '/features') return response({ assets: false });
        writes++;
        signal = options.signal;
        started();
        return await new Promise((_resolve, reject) => {
          signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
        });
      } },
      chainCallback: (object, name, callback) => { object[name] = callback; },
    });
    controller.addUploadWidget(Node, {}, 'video');
    const node = new Node();
    node.onNodeCreated();
    input.files = [new File(['fixture'], 'clip.mp4', { type: 'video/mp4' })];
    const upload = input.onchange();
    await ready;
    assert.match(node.widgets[1].name, /uploading/);
    assert.equal(await input.onchange(), false);
    assert.equal(writes, 1);
    node.onRemoved();
    assert.equal(signal.aborted, true);
    assert.equal(await upload, false);
    assert.match(node.widgets[1].name, /choose video/);
    assert.equal(node.widgets[0].value, '');
    assert.equal(node.progress, undefined);
  } finally {
    Object.assign(globalThis, previous);
  }
});
