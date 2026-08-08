import test from 'node:test';
import assert from 'node:assert/strict';

import { reconcileFormatWidgets } from '../../web/js/formatWidgetState.js';

function makeNode() {
  const formatWidget = { name: 'format', value: 'video/a', options: {} };
  const crfWidget = { name: 'crf', value: 19, options: {} };
  const pixFmtWidget = { name: 'pix_fmt', value: 'yuv420p', options: {} };
  const crfInput = { name: 'crf', type: 'INT', link: 42, widget: { name: 'crf' } };
  return {
    formatWidget,
    crfWidget,
    pixFmtWidget,
    crfInput,
    node: {
      widgets: [formatWidget, crfWidget, pixFmtWidget],
      inputs: [crfInput],
      removeInputCalls: 0,
      removeInput() {
        this.removeInputCalls += 1;
      },
    },
  };
}

const formats = {
  'video/a': [
    ['crf', 'INT', { default: 19, min: 0, max: 51, step: 1 }],
    ['pix_fmt', ['yuv420p', 'yuv420p10le']],
  ],
  'video/b': [
    ['crf', 'INT', { default: 23, min: 0, max: 63, step: 1 }],
    ['pix_fmt', ['yuv420p10le', 'yuv444p']],
  ],
  'video/c': [],
};

test('same-name widgets and linked inputs retain identity across format switches', () => {
  const fixture = makeNode();
  const state = {};

  reconcileFormatWidgets(fixture.node, fixture.formatWidget, formats, {}, state);
  fixture.crfWidget.value = 17;
  fixture.formatWidget.value = 'video/b';
  reconcileFormatWidgets(fixture.node, fixture.formatWidget, formats, {}, state);

  assert.equal(fixture.node.widgets.find((widget) => widget.name === 'crf'), fixture.crfWidget);
  assert.equal(fixture.node.inputs.find((input) => input.name === 'crf'), fixture.crfInput);
  assert.equal(fixture.crfInput.link, 42);
  assert.equal(fixture.node.removeInputCalls, 0);
  assert.deepEqual(fixture.pixFmtWidget.options.values, ['yuv420p10le', 'yuv444p']);
  assert.equal(fixture.crfWidget.value, 23);

  fixture.crfWidget.value = 29;
  fixture.formatWidget.value = 'video/a';
  reconcileFormatWidgets(fixture.node, fixture.formatWidget, formats, {}, state);
  assert.equal(fixture.crfWidget.value, 17);
});

test('inactive format widgets are hidden without deleting sockets', () => {
  const fixture = makeNode();
  const state = {};

  reconcileFormatWidgets(fixture.node, fixture.formatWidget, formats, {}, state);
  fixture.formatWidget.value = 'video/c';
  reconcileFormatWidgets(fixture.node, fixture.formatWidget, formats, {}, state);

  assert.equal(fixture.crfWidget.hidden, true);
  assert.equal(fixture.pixFmtWidget.hidden, true);
  assert.equal(fixture.node.inputs[0], fixture.crfInput);
  assert.equal(fixture.node.removeInputCalls, 0);
});

test('missing schema widget uses an explicit widget-only fallback', () => {
  const fixture = makeNode();
  const warnings = [];
  const app = {
    widgets: {
      STRING(node, name, config) {
        node.widgets.push({
          name,
          value: config[1]?.default ?? '',
          options: {},
        });
      },
    },
  };
  const customFormats = {
    'video/custom': [['custom_mode', 'STRING', { default: 'safe' }]],
  };
  fixture.formatWidget.value = 'video/custom';

  const result = reconcileFormatWidgets(
    fixture.node,
    fixture.formatWidget,
    customFormats,
    app,
    {},
    (message) => warnings.push(message),
  );

  assert.deepEqual(result.dynamicNames, ['custom_mode']);
  assert.equal(fixture.node.widgets.find((widget) => widget.name === 'custom_mode').value, 'safe');
  assert.equal(fixture.node.inputs.some((input) => input.name === 'custom_mode'), false);
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /widget-only fallback/);
});
