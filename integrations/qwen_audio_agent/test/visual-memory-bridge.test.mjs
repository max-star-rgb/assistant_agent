import test from 'node:test';
import assert from 'node:assert/strict';
import { SessionBridge } from '../src/session-bridge.mjs';

test('retired visual recall is neither exposed nor executed, even with stale capabilities', async () => {
  const calls = [];
  const bridge = new SessionBridge({ ownerId: 'owner', client: { request: async path => {
    calls.push(path);
    return { visual_memory_tool: { type: 'function', function: { name: 'recall_visual_memory' } } };
  } } });
  await bridge.initialize();
  assert.deepEqual(bridge.tools(), []);
  await assert.rejects(bridge.execute('recall_visual_memory', { query: '钥匙' }, { ownerId: 'owner', sessionId: 's' }), /tool_unavailable/);
  assert.deepEqual(calls, ['/qwen-audio/capabilities']);
  await bridge.close();
});
