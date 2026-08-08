import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const moduleURLs = {
  core: new URL('../../web/js/VHS.core.js', import.meta.url),
  upload: new URL('../../web/js/uploadTransport.js', import.meta.url),
  preview: new URL('../../web/js/mediaPreview.js', import.meta.url),
  paths: new URL('../../web/js/pathWidgets.js', import.meta.url),
  latent: new URL('../../web/js/latentPreview.js', import.meta.url),
  paste: new URL('../../web/js/pasteHandler.js', import.meta.url),
  selectLatest: new URL('../../web/js/selectLatest.js', import.meta.url),
};

test('frontend domains exist and core is the sole extension registration owner', async () => {
  const sources = Object.fromEntries(
    await Promise.all(Object.entries(moduleURLs).map(async ([name, url]) => [name, await readFile(url, 'utf8')]))
  );

  assert.equal((sources.core.match(/\bapp\.registerExtension\s*\(/g) ?? []).length, 1);
  for (const [name, source] of Object.entries(sources)) {
    if (name !== 'core') assert.doesNotMatch(source, /\bapp\.registerExtension\s*\(/);
  }

  assert.match(sources.core, /from\s+["']\.\/uploadTransport\.js["']/);
  assert.match(sources.core, /from\s+["']\.\/mediaPreview\.js["']/);
  assert.match(sources.core, /from\s+["']\.\/pathWidgets\.js["']/);
  assert.match(sources.core, /from\s+["']\.\/latentPreview\.js["']/);
  assert.match(sources.core, /from\s+["']\.\/pasteHandler\.js["']/);
  assert.match(sources.core, /from\s+["']\.\/selectLatest\.js["']/);
});

test('extracted frontend modules own domain entry points without duplicate core definitions', async () => {
  const core = await readFile(moduleURLs.core, 'utf8');
  const expected = {
    upload: ['createUploadTransport'],
    preview: ['createMediaPreview'],
    paths: ['createPathWidgets'],
    latent: ['createLatentPreview'],
    paste: ['createPasteHandler'],
    selectLatest: ['configureSelectLatestNode'],
  };
  for (const [name, exports] of Object.entries(expected)) {
    const source = await readFile(moduleURLs[name], 'utf8');
    for (const symbol of exports) assert.match(source, new RegExp(`export\\s+function\\s+${symbol}\\s*\\(`));
  }

  for (const duplicate of [
    'getServerFeatures', 'uploadFile', 'addAudioPreview', 'addVideoPreview',
    'searchBox', 'drawAnnotated', 'mouseAnnotated', 'getLatentPreviewCtx',
    'beginLatentPreview',
  ]) {
    assert.doesNotMatch(core, new RegExp(`function\\s+${duplicate}\\s*\\(`), duplicate);
  }
});

test('all VHS production modules avoid private host and reference imports', async () => {
  for (const url of Object.values(moduleURLs)) {
    const source = await readFile(url, 'utf8');
    assert.doesNotMatch(source, /(?:\.\.\/)+extensions\/core\//);
    assert.doesNotMatch(source, /(?:\.\.\/)+scripts\/ui(?:\.js|\/)/);
    assert.doesNotMatch(source, /(?:\.\.\/)+scripts\/utils\.js/);
    assert.doesNotMatch(source, /(?:^|["'\/])reference[\/]/i);
    assert.doesNotMatch(source, /electronAdapter|envUtil|electronAPI/);
  }
});

test('domain modules import safely with Desktop bridge absent or present', async () => {
  const previousWindow = globalThis.window;
  try {
    delete globalThis.window;
    for (const [name, url] of Object.entries(moduleURLs)) {
      if (name !== 'core') await import(`${url.href}?bridge=absent`);
    }

    globalThis.window = { __comfyDesktop2: { isRemote: () => false } };
    for (const [name, url] of Object.entries(moduleURLs)) {
      if (name !== 'core') await import(`${url.href}?bridge=present`);
    }
  } finally {
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
  }
});
