import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

let nonce = 0;
const workflow = { nodes: [{ id: 1, type: 'EmptyImage' }], links: [] };
function metadataFile(name = 'saved.mp4', metadata = { workflow }) {
  const text = Buffer.from(JSON.stringify(metadata));
  let bytes;
  if (/\.(?:webm|mkv)$/i.test(name)) {
    assert.ok(text.length < 128);
    bytes = Buffer.concat([Buffer.from('1a45dfa3', 'hex'), Buffer.alloc(16), Buffer.from('COMMENT'), Buffer.from([0x44, 0x87, 0x80 | text.length]), text, Buffer.alloc(16)]);
  } else {
    const ftyp = Buffer.alloc(24);
    ftyp.writeUInt32BE(24); ftyp.write('ftyp', 4); ftyp.write('isom', 8);
    const header = Buffer.alloc(24);
    header.writeUInt32BE(24 + text.length); header.writeUInt32BE(0xa9636d74, 4);
    header.writeUInt32BE(16 + text.length, 8); header.write('data', 12);
    bytes = Buffer.concat([ftyp, header, text]);
  }
  return new File([bytes], name, { type: 'video/mp4' });
}

async function withProduction(app, input, run) {
  const previous = { app: globalThis.__vhsImportTestApp, document: globalThis.document, reader: globalThis.FileReader };
  const extensions = [];
  app.registerExtension = extension => extensions.push(extension);
  globalThis.__vhsImportTestApp = app;
  globalThis.document = { getElementById: () => input.current };
  globalThis.FileReader = class {
    readAsArrayBuffer(file) {
      file.arrayBuffer().then(result => this.onload?.({ target: { result } }), () => this.onerror?.());
    }
  };
  try {
    let source = await readFile(new URL('../../web/js/videoinfo.js', import.meta.url), 'utf8');
    source = source.replace("import { app } from '../../../scripts/app.js'", 'const app = globalThis.__vhsImportTestApp;');
    source = source.replace(/from (['"])(\.\/[^'"]+)\1/g, (_match, _quote, relative) => `from ${JSON.stringify(new URL(relative, new URL('../../web/js/videoinfo.js', import.meta.url)).href)}`);
    await import(`data:text/javascript;base64,${Buffer.from(source + `\n// subject ${nonce++}`).toString('base64')}`);
    for (const extension of extensions) { await extension.init?.(); await extension.setup?.(); }
    return await run(extensions);
  } finally {
    globalThis.__vhsImportTestApp = previous.app;
    globalThis.document = previous.document;
    globalThis.FileReader = previous.reader;
  }
}

test('legacy MP4 import preserves filename, origin and warning context with exactly one load', async () => {
  const loads = [];
  let native = 0;
  const app = { handleFile() { native++; }, loadGraphData(...args) { loads.push(args); } };
  await withProduction(app, { current: { accept: '.json' } }, async () => {
    await app.handleFile(metadataFile('saved.MP4'), 'file_drop', { deferWarnings: true });
    assert.deepEqual(loads, [[workflow, true, true, 'saved', { openSource: 'file_drop', deferWarnings: true }]]);
    assert.equal(native, 0);
  });
});

test('legacy WebM and MKV remain supported through the same graph context', async () => {
  for (const name of ['saved.webm', 'saved.mkv']) {
    const loads = [];
    const app = { handleFile() { assert.fail('legacy workflow must not delegate'); }, loadGraphData(...args) { loads.push(args); } };
    await withProduction(app, { current: { accept: '' } }, async () => {
      await app.handleFile(metadataFile(name), 'file_open');
      assert.equal(loads.length, 1);
      assert.equal(loads[0][3], 'saved');
    });
  }
});

test('delegated inputs preserve this, all future arguments, result and rejection', async () => {
  const value = { accepted: true };
  const error = new Error('Native rejection');
  const receiver = {};
  const seen = [];
  const app = { handleFile(...args) { seen.push([this, args]); if (args[0].name === 'reject.json') throw error; return value; }, loadGraphData() { assert.fail('no legacy graph'); } };
  await withProduction(app, { current: { accept: '' } }, async () => {
    const file = new File(['{}'], 'plain.json');
    const options = { deferWarnings: true, onNodeCreated() {} };
    const result = await app.handleFile.call(receiver, file, 'file_open', options, 'future');
    assert.equal(result, value);
    assert.deepEqual(seen[0], [receiver, [file, 'file_open', options, 'future']]);
    await assert.rejects(async () => app.handleFile(new File([], 'reject.json')), candidate => candidate === error);
    assert.equal(seen.length, 2);
  });
});

test('missing and recreated file input are safe and repeated setup is idempotent', async () => {
  let calls = 0;
  const app = { handleFile() { calls++; return 'native'; }, loadGraphData() {} };
  const input = { current: null };
  await withProduction(app, input, async extensions => {
    assert.equal(extensions.length, 1);
    const installed = app.handleFile;
    input.current = { accept: '.json,video/mp4' };
    await extensions[0].setup();
    await extensions[0].setup();
    assert.equal(app.handleFile, installed);
    assert.equal(input.current.accept.split(',').filter(type => type === 'video/mp4').length, 1);
    assert.ok(input.current.accept.includes('video/x-matroska'));
    await app.handleFile(new File([], 'plain.json'));
    assert.equal(calls, 1);
  });
});

test('read errors and invalid or metadata-free media delegate once with native media options', async () => {
  const callback = () => {};
  let calls = 0;
  const app = { handleFile(_file, origin, options) { calls++; assert.equal(origin, 'file_drop'); assert.equal(options.onNodeCreated, callback); return 'native'; }, loadGraphData() { assert.fail('no valid legacy workflow'); } };
  await withProduction(app, { current: { accept: '' } }, async () => {
    const broken = new File([], 'broken.mp4', { type: 'video/mp4' });
    Object.defineProperty(broken, 'arrayBuffer', { value: async () => { throw new Error('read failure'); } });
    const pending = app.handleFile(broken, 'file_drop', { onNodeCreated: callback });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(calls, 1, 'Read failure must delegate instead of leaving import pending');
    assert.equal(await pending, 'native');
    for (const media of [metadataFile('invalid.mp4', { workflow: [] }), new File([], 'empty.mp4', { type: 'video/mp4' })]) {
      assert.equal(await app.handleFile(media, 'file_drop', { onNodeCreated: callback }), 'native');
    }
    assert.equal(calls, 3);
  });
});

test('host load failure is propagated without a second native import after mutation', async () => {
  const error = new Error('Graph load failed after mutation');
  let loads = 0;
  const app = { handleFile() { assert.fail('must not retry host mutation'); }, loadGraphData() { loads++; throw error; } };
  await withProduction(app, { current: { accept: '' } }, async () => {
    await assert.rejects(app.handleFile(metadataFile()), candidate => candidate === error);
    assert.equal(loads, 1);
  });
});

test('legacy graph loading preserves the native receiver as well as delegated calls', async () => {
  const app = { handleFile() { assert.fail('valid legacy workflow'); }, loadGraphData() { assert.fail('must use the caller receiver'); } };
  const calls = [];
  const receiver = { loadGraphData(...args) { calls.push([this, args]); } };
  await withProduction(app, { current: { accept: '' } }, async () => {
    await app.handleFile.call(receiver, metadataFile(), 'file_open');
    assert.equal(calls.length, 1);
    assert.equal(calls[0][0], receiver);
    assert.equal(calls[0][1][3], 'saved');
  });
});
