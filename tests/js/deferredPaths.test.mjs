import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

import { createPasteHandler } from '../../web/js/pasteHandler.js';
import { configureSelectLatestNode } from '../../web/js/selectLatest.js';
import { fitPathForDisplay, splitPath } from '../../web/js/pathWidgets.js';

function proportionalContext() {
  return { measureText: (text) => ({ width: text.length }) };
}

test('path display recognizes Windows drive and UNC separators without mutating values', () => {
  const ctx = proportionalContext();
  const values = [
    'C:\\media\\project\\very-long-video-name.mp4',
    '\\\\server\\share\\project\\very-long-video-name.mp4',
    'C:/media\\project/very-long-video-name.mp4',
    '/media/project/very-long-video-name.mp4',
  ];

  for (const value of values) {
    const original = value;
    const [directory, filename] = splitPath(value);
    const [display, width] = fitPathForDisplay(ctx, value, 24);
    assert.equal(value, original);
    assert.equal(filename, 'very-long-video-name.mp4');
    assert.ok(directory.length > 0);
    assert.ok(display.length <= 24);
    assert.equal(width, display.length);
  }

  assert.match(fitPathForDisplay(ctx, values[0], 24)[0], /^C:\\…\\/);
  assert.match(fitPathForDisplay(ctx, values[1], 24)[0], /^\\\\…\\/);
});

test('copied-path paste creates and synchronizes only the supported path node', async () => {
  const added = [];
  const callbackValues = [];
  const node = {
    pos: [0, 0],
    widgets: [{ value: '', callback: (value) => callbackValues.push(value) }],
  };
  const app = {
    canvas: {
      graph_mouse: [12, 34],
      graph: { add: (value) => added.push(value) },
    },
  };
  const handler = createPasteHandler({
    app,
    LiteGraph: { createNode: (type) => {
      assert.equal(type, 'VHS_LoadVideoPath');
      return node;
    } },
    getCopiedPath: () => 'C:\\contained\\clip.mp4',
  });
  const events = [];
  const event = {
    target: { classList: { contains: (name) => name === 'litegraph' } },
    clipboardData: {
      getData: (type) => type === 'text/plain' ? 'C:\\contained\\clip.mp4' : '',
      items: [{ type: 'video/mp4', getAsFile: () => { throw new Error('must not inspect video blobs'); } }],
    },
    preventDefault: () => events.push('prevent'),
    stopImmediatePropagation: () => events.push('stop'),
  };

  assert.equal(await handler(event), false);
  assert.deepEqual(added, [node]);
  assert.deepEqual(node.pos, [12, 34]);
  assert.equal(node.widgets[0].value, 'C:\\contained\\clip.mp4');
  assert.deepEqual(callbackValues, ['C:\\contained\\clip.mp4']);
  assert.deepEqual(events, ['prevent', 'stop']);
});

test('paste handler ignores unrelated text, targets, and video blobs', async () => {
  let created = 0;
  const handler = createPasteHandler({
    app: { canvas: { graph: { add() {} }, graph_mouse: [0, 0] } },
    LiteGraph: { createNode: () => { created += 1; } },
    getCopiedPath: () => '/contained/expected.mp4',
  });
  const event = {
    target: { classList: { contains: () => true } },
    clipboardData: {
      getData: () => '/different.mp4',
      items: [{ type: 'video/mp4' }],
    },
    preventDefault() { throw new Error('ignored paste must not be consumed'); },
    stopImmediatePropagation() { throw new Error('ignored paste must not be consumed'); },
  };

  assert.equal(await handler(event), undefined);
  assert.equal(created, 0);
});

test('SelectLatest remains virtual and propagates deterministic latest path', async () => {
  const callbackValues = [];
  const target = {
    inputs: [{ widget: { name: 'video' } }],
    widgets: [{ name: 'video', value: '', callback: (value) => callbackValues.push(value) }],
  };
  const node = {
    outputs: [{ links: [7] }],
    widgets: [{ value: '/contained/prefix' }, { value: '.mp4' }],
    graph: {
      links: { 7: { target_id: 2, target_slot: 0 } },
      getNodeById: (id) => id === 2 ? target : undefined,
    },
  };
  configureSelectLatestNode(node, {
    app: { canvas: { graph_mouse: [0, 0] } },
    api: { apiURL: (value) => value },
    fetchWithOptionalAuth: async () => ({ ok: true, json: async () => ['prefix-001.mp4', 'prefix-002.mp4'] }),
    pathStem: (value) => ['/contained/', value.slice('/contained/'.length)],
  });

  assert.equal(node.isVirtualNode, true);
  await node.widgets[0].callback();
  assert.equal(node.latest_file, '/contained/prefix-002.mp4');
  assert.equal(target.widgets[0].value, '/contained/prefix-002.mp4');
  assert.deepEqual(callbackValues, ['/contained/prefix-002.mp4']);
  node.applyToGraph();
  assert.equal(callbackValues.length, 2);
});

test('SelectLatest clears stale selection on empty or malformed path response', async () => {
  const callbackValues = [];
  const target = {
    inputs: [{ widget: { name: 'video' } }],
    widgets: [{ name: 'video', value: '/stale.mp4', callback: (value) => callbackValues.push(value) }],
  };
  const node = {
    latest_file: '/stale.mp4',
    outputs: [{ links: [9] }],
    widgets: [{ value: '/missing/prefix' }, { value: '.mp4' }],
    graph: {
      links: { 9: { target_id: 2, target_slot: 0 } },
      getNodeById: (id) => id === 2 ? target : undefined,
    },
  };
  configureSelectLatestNode(node, {
    app: { canvas: { graph_mouse: [0, 0] } },
    api: { apiURL: (value) => value },
    fetchWithOptionalAuth: async () => ({ ok: true, json: async () => ({ error: 'denied' }) }),
    pathStem: () => ['/missing/', 'prefix'],
  });

  await node.onPromptExecuted();
  assert.equal(node.latest_file, undefined);
  assert.equal(target.widgets[0].value, '');
  assert.deepEqual(callbackValues, ['']);
});

test('dormant deferred branches are absent from production core', async () => {
  const core = await readFile(new URL('../../web/js/VHS.core.js', import.meta.url), 'utf8');
  assert.doesNotMatch(core, /video\s*&&\s*false/);
  assert.doesNotMatch(core, /function\s+addTimestampWidget\s*\(/);
  assert.doesNotMatch(core, /VHS_SaveImageSequence/);
  assert.match(core, /createPasteHandler/);
  assert.match(core, /configureSelectLatestNode/);
});
