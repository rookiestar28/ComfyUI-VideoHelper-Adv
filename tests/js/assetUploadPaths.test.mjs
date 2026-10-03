import test from 'node:test';
import assert from 'node:assert/strict';
import { createUploadTransport } from '../../web/js/uploadTransport.js';

const reply = (body, status = 200) => new Response(JSON.stringify(body), { status });
const file = (name = 'clip.mp4') => new File(['synthetic'], name, { type: 'video/mp4' });

async function uploadCase(asset, { status = 200, features = { assets: true }, options = {}, media = file(), failure, legacy } = {}) {
  const previous = globalThis.alert;
  const alerts = [];
  const writes = [];
  globalThis.alert = message => alerts.push(String(message));
  try {
    const controller = createUploadTransport({
      app: { ui: { settings: { getSettingValue: () => false } } },
      api: { fetchApi: async (route, request) => {
        if (route === '/features') return reply(features);
        writes.push({ route, body: request.body });
        if (route === '/api/assets') {
          if (failure) throw failure;
          return reply(asset, status);
        }
        assert.equal(route, '/upload/image');
        return reply(legacy ?? { name: media.name, subfolder: request.body.get('subfolder') ?? '', type: 'input' });
      } },
      chainCallback() {},
    });
    const result = await controller.uploadFile(media, undefined, options);
    return { result, alerts, writes };
  } finally {
    globalThis.alert = previous;
  }
}

test('canonical loader_path is accepted once and takes precedence over user metadata', async () => {
  const { result, writes, alerts } = await uploadCase({ loader_path: 'hash/影片 clip.mp4', user_metadata: { filename: 'wrong.mp4' }, tags: ['input'] });
  assert.equal(result.path, 'hash/影片 clip.mp4');
  assert.deepEqual(writes.map(value => value.route), ['/api/assets']);
  assert.equal(alerts.length, 0);
});

test('safe older filename response remains supported when canonical field is absent', async () => {
  const { result, writes } = await uploadCase({ user_metadata: { filename: 'old\\clip.mp4' } });
  assert.equal(result.path, 'old/clip.mp4');
  assert.equal(writes.length, 1);
});

for (const [label, asset] of [
  ['pathless', { user_metadata: {} }],
  ['null canonical', { loader_path: null, user_metadata: { filename: 'safe.mp4' } }],
  ['absolute', { loader_path: '/outside.mp4' }],
  ['drive', { loader_path: 'C:\\outside.mp4' }],
  ['UNC', { loader_path: '\\\\server\\share\\outside.mp4' }],
  ['traversal', { loader_path: 'sub/../outside.mp4' }],
  ['NUL', { loader_path: 'clip\0.mp4' }],
  ['wrong response root', { loader_path: 'clip.mp4', tags: ['output'] }],
]) {
  test(`successful ${label} asset response fails without another write`, async () => {
    const { result, writes, alerts } = await uploadCase(asset);
    assert.equal(result, undefined);
    assert.equal(writes.length, 1);
    assert.equal(alerts.length, 1);
  });
}

for (const status of [400, 401, 403, 413, 500]) {
  test(`HTTP ${status} asset failure is not replayed`, async () => {
    const { result, writes } = await uploadCase({}, { status });
    assert.equal(result, undefined);
    assert.equal(writes.length, 1);
  });
}

for (const status of [404, 405, 501]) {
  test(`HTTP ${status} unavailable asset route permits legacy fallback`, async () => {
    const { result, writes } = await uploadCase({}, { status });
    assert.equal(result.path, 'clip.mp4');
    assert.deepEqual(writes.map(value => value.route), ['/api/assets', '/upload/image']);
  });
}

test('relative directories preselect legacy before any asset write and preserve filename', async () => {
  const media = file('001.mp4');
  Object.defineProperty(media, 'webkitRelativePath', { value: 'sequence/001.mp4' });
  const { result, writes } = await uploadCase({ user_metadata: { filename: 'flattened.mp4' } }, { media });
  assert.deepEqual(writes.map(value => value.route), ['/upload/image']);
  assert.equal(writes[0].body.get('subfolder'), 'sequence');
  assert.equal(writes[0].body.get('image').name, '001.mp4');
  assert.equal(result.path, 'sequence/001.mp4');
});

test('explicit subfolder preselects legacy; incompatible root is rejected before writing', async () => {
  const folder = await uploadCase({}, { options: { subfolder: 'sequence/' } });
  assert.deepEqual(folder.writes.map(value => value.route), ['/upload/image']);
  assert.equal(folder.result.path, 'sequence/clip.mp4');
  const denied = await uploadCase({}, { options: { assetRootTag: 'output' } });
  assert.equal(denied.result, undefined);
  assert.equal(denied.writes.length, 0);
});

test('uncertain network and timeout outcomes stop after the asset write attempt', async () => {
  for (const failure of [new TypeError('Network failure'), new DOMException('Timeout', 'TimeoutError')]) {
    const { result, writes } = await uploadCase({}, { failure });
    assert.equal(result, undefined);
    assert.equal(writes.length, 1);
  }
});

test('unsafe explicit subfolder and legacy response cannot become widget paths', async () => {
  const invalid = await uploadCase({}, { options: { subfolder: '../outside' } });
  assert.equal(invalid.result, undefined);
  assert.equal(invalid.writes.length, 0);
  for (const legacy of [{ name: '../outside.mp4' }, { name: 'clip.mp4', subfolder: '/outside' }, { name: 'clip.mp4', type: 'output' }]) {
    const rejected = await uploadCase({}, { features: { assets: false }, legacy });
    assert.equal(rejected.result, undefined);
    assert.equal(rejected.writes.length, 1);
  }
});

test('folder uploads use deterministic names and leave prior selection on partial failure', async () => {
  const previous = { document: globalThis.document, alert: globalThis.alert };
  let input;
  const alerts = [];
  const names = [];
  globalThis.document = { createElement: () => (input = { remove() {}, click() {} }), body: { append() {} } };
  globalThis.alert = message => alerts.push(String(message));
  try {
    class Node {
      widgets = [{ name: 'directory', value: 'previous', options: { values: ['previous'] } }];
      addWidget(_type, name) { const widget = { name, options: {} }; this.widgets.push(widget); return widget; }
    }
    const controller = createUploadTransport({
      app: { canvas: {}, ui: { settings: { getSettingValue: () => false } } },
      api: { fetchApi: async (route, request) => {
        if (route === '/features') return reply({ assets: false });
        assert.equal(route, '/upload/image');
        const name = request.body.get('image').name;
        names.push(name);
        return reply({ name, subfolder: 'sequence', type: 'input' }, names.length === 2 ? 500 : 200);
      } },
      chainCallback: (object, name, callback) => { object[name] = callback; },
    });
    controller.addUploadWidget(Node, {}, 'directory', 'folder');
    const node = new Node();
    node.onNodeCreated();
    input.files = ['003.png', '001.png', '002.png'].map(name => {
      const media = new File(['image'], name, { type: 'image/png' });
      Object.defineProperty(media, 'webkitRelativePath', { value: `sequence/${name}` });
      return media;
    });
    await input.onchange();
    assert.deepEqual(names, ['001.png', '002.png']);
    assert.equal(node.widgets[0].value, 'previous');
    assert.deepEqual(node.widgets[0].options.values, ['previous']);
    assert.ok(alerts.some(value => /1 of 3/.test(value)));
    assert.equal(node.progress, undefined);
    assert.match(node.widgets[1].name, /choose folder/);
  } finally {
    Object.assign(globalThis, previous);
  }
});
