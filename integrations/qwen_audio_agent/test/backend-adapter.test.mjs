import test from 'node:test';
import assert from 'node:assert/strict';
import { verifyBackendAdapterConformance } from 'qwen-audio-agent/backend-adapter-sdk';
import { AgentServerClient } from '../src/agent-server-client.mjs';
import { LangGraphBackendAdapter } from '../src/backend-adapter.mjs';
import { serverFixture, work } from './fixture.mjs';

async function fixture(t, options) {
  const server = await serverFixture(options);
  const client = new AgentServerClient({ url: server.url, identity: 'local-developer' });
  const backend = new LangGraphBackendAdapter({ client, ownerId: work.ownerId, pollIntervalMs: 5, timeoutMs: 2000 });
  t.after(async () => { await backend.close(); await server.close(); });
  return { ...server, client, backend };
}

test('official BackendPort conformance over native HTTP wire', async t => {
  await verifyBackendAdapterConformance({ createFixture: async ({ hold }) => {
    const f = await fixture(t, { hold });
    return { backend: f.backend, work, nextWork: { ...work, id: 'task-two' }, started: f.started };
  } });
});

test('isolates tasks, guards context and sends only instruction', async t => {
  const f = await fixture(t);
  const result = await f.backend.submit({ ...work, rawTranscript: 'do not forward', context: { require_tool_approval: false } });
  assert.deepEqual(result, { content: '业务完成', artifacts: [] });
  await f.backend.submit({ ...work, id: 'task-two' });
  assert.equal(f.threads.size, 2);
  const creates = f.requests.filter(r => r.method === 'POST' && r.path.endsWith('/runs'));
  assert.equal(creates.length, 2);
  assert.deepEqual(creates[0].body.input, { messages: [{ role: 'user', name: 'voice_agent', content: '整理旅行计划' }] });
  assert.deepEqual(creates[0].body.context, { enable_memory: false, require_tool_approval: true });
  assert.equal(creates[0].body.multitask_strategy, 'reject');
  assert.equal(creates[0].user, 'local-developer');
});

test('rejects untrusted owners and unsupported attachments before HTTP', async t => {
  const f = await fixture(t);
  await assert.rejects(f.backend.submit({ ...work, ownerId: 'victim' }), /owner/);
  await assert.rejects(f.backend.submit({ ...work, inputParts: [{ type: 'file', url: 'file:///etc/passwd' }] }), /attachment/);
  assert.equal(f.requests.length, 0);
});

test('lost submission response is reconciled without a second execution', async t => {
  const f = await fixture(t, { dropSubmit: true });
  assert.equal((await f.backend.submit(work)).content, '业务完成');
  await f.backend.submit(work);
  assert.equal(f.requests.filter(r => r.method === 'POST' && r.path.endsWith('/runs')).length, 1);
});

test('closing adapter stops observation but never cancels business', async t => {
  const f = await fixture(t, { hold: true });
  const result = assert.rejects(f.backend.submit(work), /closed/);
  await f.started;
  await f.backend.close();
  await result;
  assert.equal(f.requests.filter(r => r.path.includes('/cancel')).length, 0);
});

test('HITL waits for Studio and refuses Gateway auto-granted permissions', async t => {
  const interrupt = { id: 'private-interrupt', value: { action_requests: [{ name: 'write_file', args: { path: 'x', content: 'secret' } }], review_configs: [{ action_name: 'write_file', allowed_decisions: ['approve', 'reject'] }] } };
  const f = await fixture(t, { interrupt });
  let permission;
  const requested = new Promise(resolve => f.backend.subscribe(e => { if (e.type === 'backend.permission.requested') { permission = e.permission; resolve(); } }));
  const result = f.backend.submit(work);
  await requested;
  assert.equal(permission.approvalScope, 'once');
  assert.equal(JSON.stringify(permission).includes('secret'), false);
  await assert.rejects(f.backend.respondAuthorization(work.id, permission.id, 'once', { ownerId: work.ownerId }), /Studio/);
  assert.equal(f.requests.filter(r => r.body?.command).length, 0);
  const thread = [...f.threads.values()][0];
  thread.interrupt = null;
  thread.runs.push({ run_id: 'studio-resume', status: 'success', metadata: {} });
  assert.equal((await result).content, '业务完成');
});

test('input resume is bound to owner and current interrupt and happens once', async t => {
  const f = await fixture(t, { interrupt: { id: 'ask-1', value: { type: 'input', prompt: '出发日期？', mode: 'text' } } });
  let input;
  const requested = new Promise(resolve => f.backend.subscribe(e => { if (e.type === 'backend.input.requested') { input = e.input; resolve(); } }));
  const result = f.backend.submit(work);
  await requested;
  await assert.rejects(f.backend.respondInput(work.id, input.id, { text: '明天' }, { ownerId: 'other' }));
  await f.backend.respondInput(work.id, input.id, { text: '明天' }, { ownerId: work.ownerId });
  await assert.rejects(f.backend.respondInput(work.id, input.id, { text: '明天' }, { ownerId: work.ownerId }));
  assert.equal((await result).content, '业务完成');
  const resume = f.requests.filter(r => r.body?.command);
  assert.equal(resume.length, 1);
  assert.deepEqual(resume[0].body.command, { resume: { 'ask-1': '明天' } });
});
