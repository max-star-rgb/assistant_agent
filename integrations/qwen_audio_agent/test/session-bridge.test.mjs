import test from 'node:test';
import assert from 'node:assert/strict';
import { SessionBridge } from '../src/session-bridge.mjs';

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
