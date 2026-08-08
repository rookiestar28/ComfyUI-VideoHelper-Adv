import test from 'node:test';
import assert from 'node:assert/strict';

import {
  applyTextReplacements,
  formatDate,
} from '../../web/js/textReplacements.js';

test('formatDate implements ComfyUI filename date tokens', () => {
  const date = new Date(2026, 7, 8, 5, 6, 7);
  assert.equal(formatDate('yyyy-MM-dd_hh-mm-ss', date), '2026-08-08_05-06-07');
  assert.equal(formatDate('yy-M-d', date), '26-8-8');
});

test('replacement resolves property names, titles, and nested subgraphs', () => {
  const nested = {
    title: 'Nested Node',
    properties: { 'Node name for S&R': 'nested' },
    widgets: [{ name: 'value', value: 'nested/result' }],
  };
  const graph = {
    _nodes: [
      {
        title: 'Title Match',
        properties: {},
        widgets: [{ name: 'value', value: 'title:value' }],
        subgraph: { _nodes: [nested] },
      },
      {
        title: 'Ignored Title',
        properties: { 'Node name for S&R': 'named' },
        widgets: [{ name: 'value', value: 'named*value' }],
      },
    ],
  };

  assert.equal(
    applyTextReplacements(graph, '%named.value%_%Title Match.value%_%nested.value%'),
    'named_value_title_value_nested_result',
  );
});

test('date tokens resolve while width, height, and invalid tokens remain for backend handling', () => {
  const date = new Date(2026, 7, 8, 5, 6, 7);
  assert.equal(
    applyTextReplacements({ _nodes: [] }, '%date:yyyy-MM-dd%_%width%x%height%_%unknown%', date),
    '2026-08-08_%width%x%height%_%unknown%',
  );
});
