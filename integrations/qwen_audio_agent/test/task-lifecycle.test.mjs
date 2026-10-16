import test from 'node:test';
import assert from 'node:assert/strict';
import { AgentServerClient } from '../src/agent-server-client.mjs';
import { LangGraphBackendAdapter } from '../src/backend-adapter.mjs';
import { serverFixture, work } from './fixture.mjs';

test('success snapshot follows a newer Studio run before returning old text', async t => {
  const f = await setup(t, { hold: false });
  const state = f.client.state.bind(f.client);
  let advanced = false;
  f.client.state = async handle => {
    const result = await state(handle);
    if (!advanced) {
      advanced = true;
      [...f.threads.values()][0].runs.push({ run_id: 'studio', status: 'error' });
    }
    return result;
  };
  await assert.rejects(f.backend.submit(work), /business_error/);
});

test('rejected concurrent operation does not reject task observation', async t => {
  const f = await setup(t);
  let entered, release;
  const polled = new Promise(resolve => { entered = resolve; });
  const gate = new Promise(resolve => { release = resolve; });
  const run = f.client.run.bind(f.client);
  f.client.run = async handle => { entered(); await gate; return run(handle); };
  const pending = f.backend.submit(work);
  await polled;
  const record = f.backend.active.get(work.id);
  record.mutation = Promise.reject(new Error('stale_interrupt'));
  record.mutation.catch(() => {});
  release();
  setTimeout(() => {
    record.mutation = null;
    [...f.threads.values()][0].runs[0].status = 'success';
  }, 10);
  assert.equal((await pending).content, '业务完成');
});

test('cancel rejects a terminal run so Gateway cannot report false cancellation', async t => {
  const f = await setup(t);
  const pending = f.backend.submit(work);
  await f.started;
  [...f.threads.values()][0].runs[0].status = 'success';
  await assert.rejects(f.backend.cancel(work.id, { ownerId: work.ownerId }), /already_finished/);
  assert.equal((await pending).content, '业务完成');
});

async function setup(t, options = { hold: true }) {
  const f = await serverFixture(options);
  const client = new AgentServerClient({ url: f.url, identity: 'local-developer' });
  const backend = new LangGraphBackendAdapter({ client, ownerId: work.ownerId, pollIntervalMs: 5, timeoutMs: 1000 });
  t.after(async () => { await backend.close(); await f.close(); });
  return { ...f, client, backend };
}

test('active task status requires owner even if task id is known', async t => {
  const f = await setup(t);
  const pending = assert.rejects(f.backend.submit(work));
  await f.started;
  assert.equal(f.backend.status(work.id).state, 'not_found');
  await f.backend.close();
  await pending;
});

test('cancel targets resumed run rather than the old interrupted run', async t => {
  const f = await setup(t, { interrupt: { id: 'approval', value: { action_requests: [{ name: 'write_file' }] } } });
  let ready;
  const waiting = new Promise(resolve => { ready = resolve; });
  f.backend.subscribe(e => { if (e.type === 'backend.permission.requested') ready(); });
  const pending = assert.rejects(f.backend.submit(work), /cancel/);
  await waiting;
  const thread = [...f.threads.values()][0];
  thread.interrupt = null;
  thread.runs.push({ run_id: 'new-resumed-run', status: 'running', metadata: {} });
  await f.backend.cancel(work.id, { ownerId: work.ownerId });
  await pending;
  assert.match(f.requests.find(r => r.path.includes('/cancel')).path, /new-resumed-run/);
});

test('existing empty thread and unresolved submission never replay run creation', async t => {
  const f = await setup(t, { dropSubmit: true });
  await f.client.submit(work);
  const thread = [...f.threads.values()][0];
  thread.runs.length = 0;
  await assert.rejects(f.client.submit(work), /submission_unknown/);
  assert.equal(f.requests.filter(r => r.method === 'POST' && r.path.endsWith('/runs')).length, 1);
});

test('reused task id cannot silently return a result for different input', async t => {
  const f = await setup(t, { hold: false });
  await f.client.submit(work);
  await assert.rejects(f.client.submit({ ...work, instruction: '不同任务' }), /task_input_mismatch/);
  assert.equal(f.requests.filter(r => r.method === 'POST' && r.path.endsWith('/runs')).length, 1);
});

test('independent installations do not share sequential Gateway task ids', async t => {
  const f = await setup(t, { hold: false });
  await f.client.submit(work);
  const other = new AgentServerClient({ url: f.url, identity: 'local-developer', namespace: 'other-installation' });
  await other.submit(work);
  assert.equal(f.threads.size, 2);
});

test('closing during in-flight poll prevents late success delivery', async t => {
  const f = await setup(t);
  let release, entered;
  const polled = new Promise(resolve => { entered = resolve; });
  const gate = new Promise(resolve => { release = resolve; });
  f.client.run = async () => { entered(); await gate; return { status: 'success' }; };
  const pending = assert.rejects(f.backend.submit(work), /closed/);
  await polled;
  await f.backend.close();
  release();
  await pending;
});

test('empty/unsupported final output is an error instead of fabricated success', async t => {
  const f = await setup(t, { hold: false });
  f.client.state = async () => ({ tasks: [], values: { messages: [{ type: 'tool', content: 'secret tool result' }] } });
  await assert.rejects(f.backend.submit(work), /business_result_missing/);
});
