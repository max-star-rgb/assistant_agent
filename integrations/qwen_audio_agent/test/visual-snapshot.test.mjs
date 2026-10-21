import test from 'node:test';
import assert from 'node:assert/strict';
import { VisualMemoryBridge } from '../src/visual-memory-bridge.mjs';

test('initial and asynchronous snapshots notify context, isolate owner and retain data on failure', async () => {
  let fail = false, texts = ['钥匙在桌上'], calls = [];
  const bridge = new VisualMemoryBridge({ ownerId: 'owner', client: { request: async (path, options) => {
    calls.push([path, options?.method]);
    if (fail) throw Error('remote private payload');
    return { enabled: true, texts };
  } } });
  let notices = 0;
  const unsubscribe = bridge.subscribeVisualMemory(() => notices++);
  await bridge.beginVisualMemorySession({ ownerId: 'owner' });
  assert.deepEqual(bridge.visualMemorySnapshot('owner'), texts);
  assert.deepEqual(bridge.visualMemorySnapshot('other'), []);
  assert.equal(calls[0][1], 'POST');
  texts = ['钥匙在玄关'];
  await bridge.poll();
  assert.equal(notices, 2);
  fail = true;
  await bridge.poll();
  assert.deepEqual(bridge.visualMemorySnapshot('owner'), texts);
  assert.equal(bridge.visualMemoryStatus('owner'), 'unavailable');
  assert.equal(notices, 3);
  unsubscribe();
  fail = false;
  texts = [];
  await bridge.poll();
  assert.deepEqual(bridge.visualMemorySnapshot('owner'), []);
  assert.equal(bridge.visualMemoryStatus('owner'), 'ready_empty');
  await bridge.close();
  assert.deepEqual(bridge.visualMemorySnapshot('owner'), []);
});
