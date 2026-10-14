import { strict as assert } from 'node:assert';
import { existsSync } from 'node:fs';
import { test } from 'node:test';

test('isolated TypeScript runtime', () => {
  const expected: number = 1000;
  assert.equal(process.getuid!(), expected);
  assert.equal(process.env.GITHUB_TOKEN, undefined);
  assert.equal(existsSync('/var/run/docker.sock'), false);
});
