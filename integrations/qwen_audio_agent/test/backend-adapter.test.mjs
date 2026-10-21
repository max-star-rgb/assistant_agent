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
  const result = await f.backend.submit({ ...work, rawTranscript: 'do not forward', context: { require_tool_approval: true } });
  assert.deepEqual(result, { content: '业务完成', artifacts: [], execution_status: 'completed', delivery_status: 'ready' });
  await f.backend.submit({ ...work, id: 'task-two' });
  assert.equal(f.threads.size, 2);
  const creates = f.requests.filter(r => r.method === 'POST' && r.path.endsWith('/runs'));
  assert.equal(creates.length, 2);
  assert.deepEqual(creates[0].body.input, { messages: [{ role: 'user', name: 'voice_agent', content: '整理旅行计划' }] });
  assert.deepEqual(creates[0].body.context, { enable_memory: false, require_tool_approval: false });
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

test('unexpected old HITL is not silently approved or resumed', async t => {
  const f = await fixture(t, { interrupt: { id: 'old', value: { action_requests: [{ name: 'write_file' }] } } });
  await assert.rejects(f.backend.submit(work), /unsupported_interrupt_use_Studio/);
  assert.equal(f.requests.filter(r => r.body?.command).length, 0);
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
