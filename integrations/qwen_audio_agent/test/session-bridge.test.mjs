import test from 'node:test';
import assert from 'node:assert/strict';
import { SessionBridge } from '../src/session-bridge.mjs';

test('Bailian schema and arguments pass through the authenticated bridge without memory', async () => {
  const definition = { type: 'function', function: { name: 'mcp_bailian_bailian_web_search',
    description: '远端原始描述', parameters: { type: 'object', properties: { query: { type: 'string' } } } } };
  const calls = [];
  const bridge = new SessionBridge({ ownerId: 'owner', client: { request: async (path, options) => {
    calls.push({ path, options });
    return path.endsWith('capabilities') ? { memory: false, search_tool: definition } : { result: '资料' };
  } } });
  await bridge.initialize();
  assert.deepEqual(bridge.tools().map(tool => tool.definition), [definition]);
  const context = { ownerId: 'owner', sessionId: 's' };
  assert.deepEqual(await bridge.execute(definition.function.name, { query: '天气' }, context), { result: '资料' });
  assert.deepEqual(calls[1], { path: '/qwen-audio/search', options: { method: 'POST',
    body: { session_id: 's', arguments: { query: '天气' } } } });
  await assert.rejects(bridge.execute(definition.function.name, {}, { ...context, ownerId: 'other' }), /owner/);
  await assert.rejects(bridge.execute('web_search', {}, context));
  await assert.rejects(bridge.execute('fetch_url', {}, context));
});
