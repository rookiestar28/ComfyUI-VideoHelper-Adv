import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const source = await readFile(new URL('../../web/js/VHS.core.js', import.meta.url), 'utf8');

test('VHS production frontend avoids deprecated ComfyUI internal modules', () => {
  assert.doesNotMatch(source, /(?:\.\.\/)+extensions\/core\//);
  assert.doesNotMatch(source, /(?:\.\.\/)+scripts\/ui(?:\.js|\/)/);
  assert.doesNotMatch(source, /(?:\.\.\/)+scripts\/utils\.js/);
  assert.doesNotMatch(source, /\bsetWidgetConfig\b/);
});

test('VHS core keeps only supported ComfyUI entry-point imports', () => {
  const coreImports = [...source.matchAll(/from\s+['"](\.\.\/\.\.\/\.\.\/[^'"]+)['"]/g)]
    .map((match) => match[1]);

  assert.deepEqual(coreImports.sort(), [
    '../../../scripts/api.js',
    '../../../scripts/app.js',
  ]);
});
