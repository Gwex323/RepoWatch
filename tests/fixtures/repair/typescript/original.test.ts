import { test } from 'node:test';
import assert from 'node:assert/strict';
import { bound } from './bounds.ts';

test('value below limit', () => {
  assert.equal(bound(2, 5), 2);
});
