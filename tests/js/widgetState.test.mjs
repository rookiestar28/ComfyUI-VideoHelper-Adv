import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const core = await readFile(new URL('../../web/js/VHS.core.js', import.meta.url), 'utf8');
const section = core.slice(core.indexOf('function chainCallback('), core.indexOf('var helpDOM'));
let helpers = {};
try { helpers = await import('../../web/js/widgetState.js'); } catch (error) {
  if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error;
}

function makeNode(type, fields, defaults = {}) {
  const dialogs = [];
  const app = { ui: { dialog: { show: value => dialogs.push(value) } } };
  const LiteGraph = { getNodeType: () => ({ nodeData: { input: { required: defaults } } }) };
  class Node {
    constructor() {
      this.type = type; this.title = type;
      this.widgets = fields.map(([name, value, options = {}]) => ({
        name, value, type: options.type ?? 'number', options,
        serialize: options.serialize, callback(value) { this.calls = (this.calls ?? []).concat([value]); },
      }));
      this.onNodeCreated();
    }
    configure(info) {
      // The public host migrator compacts a legacy dictionary before onConfigure.
      const migrated = { ...info, widgets_values: Array.isArray(info.widgets_values)
        ? info.widgets_values : Object.values(info.widgets_values ?? {}) };
      this.onConfigure(migrated);
    }
  }
  new Function('app', 'LiteGraph', 'prepareWidgetConfiguration', 'resolveWidgetState',
    section + '\nreturn useKVState;')(app, LiteGraph,
      helpers.prepareWidgetConfiguration, helpers.resolveWidgetState)(Node);
  return { node: new Node(), dialogs };
}
const folderFields = [
  ['directory', 'old'], ['image_load_cap', 17], ['skip_first_images', 9], ['select_every_nth', 8],
  ['upload', 'image', { type: 'button', serialize: false }],
  ['videopreview', { params: {} }, { type: 'preview', serialize: false }],
];
const values = node => Object.fromEntries(node.widgets.map(w => [w.name, w.value]));

test('production configure retains keyed names across host compaction and does not mutate workflow', () => {
  const { node, dialogs } = makeNode('VHS_LoadImages', folderFields);
  const info = { widgets_values: { select_every_nth: 1, directory: 'sequence', skip_first_images: 0, image_load_cap: 0 } };
  const before = structuredClone(info);
  node.configure(info);
  assert.equal(values(node).directory, 'sequence');
  assert.equal(values(node).image_load_cap, 0);
  assert.equal(values(node).skip_first_images, 0);
  assert.equal(values(node).select_every_nth, 1);
  assert.deepEqual(dialogs, []);
  assert.deepEqual(info, before);
});

test('modern compact positional values exclude upload and preview controls', () => {
  const { node, dialogs } = makeNode('VHS_LoadImages', folderFields);
  node.configure({ widgets_values: ['sequence', 0, 0, 1] });
  assert.equal(values(node).directory, 'sequence');
  assert.equal(values(node).select_every_nth, 1);
  assert.equal(values(node).upload, 'image');
  assert.deepEqual(dialogs, []);
});

test('named values win over stale legacy positional data and preserve falsey values', () => {
  const { node, dialogs } = makeNode('VHS_LoadImages', folderFields);
  node.configure({ widgets_values: ['stale', 'image', 19, 3, 5], widgets_values_named: {
    directory: '', image_load_cap: 0, skip_first_images: 0, select_every_nth: 1,
  } });
  assert.equal(values(node).directory, '');
  assert.equal(values(node).image_load_cap, 0);
  assert.equal(values(node).select_every_nth, 1);
  assert.deepEqual(dialogs, []);
});

test('historical folder upload placeholder still converts by its legacy schema', () => {
  const { node, dialogs } = makeNode('VHS_LoadImages', folderFields);
  node.configure({ widgets_values: ['legacy', 'image', 12, 2, 3] });
  assert.equal(values(node).directory, 'legacy');
  assert.equal(values(node).image_load_cap, 12);
  assert.equal(values(node).skip_first_images, 2);
  assert.equal(values(node).select_every_nth, 3);
  assert.deepEqual(dialogs, []);
});

test('force_size and save_image migrate without mutating nested workflow state', () => {
  const { node } = makeNode('VHS_LoadVideo', [
    ['video', ''], ['custom_width', 0], ['custom_height', 0],
    ['videopreview', {}, { type: 'preview', serialize: false }],
  ]);
  const info = { widgets_values: { video: 'clip.mp4', force_size: '?x48', videopreview: { params: { force_size: '?x48' } } } };
  const before = structuredClone(info);
  node.onConfigure(info);
  assert.equal(values(node).custom_height, 48);
  assert.equal(values(node).custom_width, 0);
  assert.equal(values(node).videopreview.params.force_size, undefined);
  assert.deepEqual(info, before);
  const combine = makeNode('VHS_VideoCombine', [['save_output', true]]).node;
  combine.onConfigure({ widgets_values: { save_image: false } });
  assert.equal(values(combine).save_output, false);
});

test('missing named fields restore explicit zero false and empty defaults', () => {
  const { node } = makeNode('VHS_Test', [['zero', 9], ['flag', true], ['text', 'stale']], {
    zero: ['INT', { default: 0 }], flag: ['BOOLEAN', { default: false }], text: ['STRING', { default: '' }],
  });
  node.onConfigure({ widgets_values: {} });
  assert.deepEqual(values(node), { zero: 0, flag: false, text: '' });
});

test('serialize produces named state without control values and preserves keyed legacy schema', () => {
  const { node } = makeNode('VHS_LoadImages', folderFields);
  const serialized = {};
  node.onSerialize(serialized);
  assert.equal(serialized.widgets_values.directory, 'old');
  assert.deepEqual(serialized.widgets_values_named, {
    directory: 'old', image_load_cap: 17, skip_first_images: 9, select_every_nth: 8,
  });
});

test('unmatched positional data keeps a visible restoration failure', () => {
  const { node, dialogs } = makeNode('VHS_Unknown', [['a', 0], ['b', 1]]);
  node.onConfigure({ widgets_values: [7] });
  assert.equal(dialogs.length, 1);
  assert.match(dialogs[0], /Failed to restore/);
});
