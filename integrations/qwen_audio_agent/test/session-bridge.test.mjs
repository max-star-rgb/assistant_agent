import test from 'node:test';
import assert from 'node:assert/strict';
import { SessionBridge } from '../src/session-bridge.mjs';

test('explicit edits use business task admission and snapshots notify only on changes', async () => {
  const calls = [];
  const bridge = new SessionBridge({ ownerId: 'owner', client: { request: async (path, options) => {
    calls.push({ path, options });
    if (path.endsWith('capabilities')) return { memory: true, memory_editable: true,
      memory_tool: { type: 'function', function: { name: 'recall_memory' } } };
    return { status: 'ok', documents: [{ memory_id: '1', content: 'background', editable: true }] };
  } } });
  await bridge.initialize();
  let notifications = 0;
  bridge.subscribeMemory(() => notifications++);
  await bridge.refreshSnapshot();
  await bridge.refreshSnapshot();
  assert.equal(notifications, 1);
  assert.deepEqual(bridge.memorySnapshot('other'), []);
  assert.match(bridge.memorySnapshot('owner')[0].content, /background/);
  const submitted = [];
  bridge.submitEdit = (args, context) => { submitted.push({ args, context }); return { status: 'accepted' }; };
  const context = { ownerId: 'owner', sessionId: 's', turnId: 't' };
  assert.equal((await bridge.execute('recall_memory', { action: 'append', content: 'new' }, context)).status, 'accepted');
  assert.equal(submitted.length, 1);
  await assert.rejects(bridge.execute('recall_memory', { action: 'replace', memory_id: '1', old_text: 'old' }, context));
  assert.ok(calls.every(c => c.path !== '/memory-edits'));
});

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

test('completed voice-user records submit deduplicated snapshots, not assistant/tool results', async () => {
  const calls = [];
  const client = { request: async (path, options) => {
    calls.push({ path, body: options?.body });
    return path.endsWith('capabilities') ? { memory: true } : { status: 'submitted' };
  } };
  const bridge = new SessionBridge({ client, ownerId: 'owner' });
  await bridge.initialize();
  const context = { ownerId: 'owner', sessionId: 'voice-1' };
  const message = { id: 'user:1', role: 'user', source: 'voice-user', content: '我住在上海' };
  await bridge.observe(message, context);
  await bridge.observe(message, context);
  await bridge.observe({ ...message, id: 'assistant:1', role: 'assistant' }, context);
  await bridge.observe({ ...message, id: 'user:2', source: 'agent-result' }, context);
  assert.equal(calls.filter(c => c.path.endsWith('transcripts')).length, 1);
  assert.deepEqual(calls[1].body, { session_id: 'voice-1', items: [{ id: 'user:1', text: '我住在上海' }] });
  await assert.rejects(bridge.observe(message, { ...context, ownerId: 'other' }), /owner/);
  await bridge.close();
});

test('disabled memory exposes no recall tool and sends no transcripts', async () => {
  let count = 0;
  const bridge = new SessionBridge({ ownerId: 'owner', client: { request: async () => { count++; return { memory: false }; } } });
  await bridge.initialize();
  await bridge.observe({ id: '1', role: 'user', source: 'voice-user', content: 'hello' }, { ownerId: 'owner', sessionId: '1' });
  assert.deepEqual(bridge.tools(), []);
  assert.equal(count, 1);
});

test('recall source rejects identity injection, and memory failure is observable', async () => {
  const bridge = new SessionBridge({ ownerId: 'owner', client: { request: async path => {
    if (path.endsWith('capabilities')) return { memory: true };
    throw Error('upstream-private-content');
  } } });
  await bridge.initialize();
  await assert.rejects(bridge.execute('recall_memory', { request: 'x', ownerId: 'victim' }, { ownerId: 'owner', sessionId: 's' }), /arguments/);
  await assert.rejects(bridge.observe({ id: '1', role: 'user', source: 'voice-user', content: 'hi' }, { ownerId: 'owner', sessionId: 's' }), /memory_submission_failed/);
  assert.deepEqual(bridge.health(), { ok: false, enabled: true, error: 'memory_submission_failed' });
});
