import test from 'node:test';
import assert from 'node:assert/strict';
import { AgentServerClient } from '../src/agent-server-client.mjs';
import { serverFixture, work } from './fixture.mjs';

test('task session supplies stable private image scope across independent threads', async t => {
  const f = await serverFixture();
  t.after(() => f.close());
  const client = new AgentServerClient({ url: f.url, identity: 'local-developer', namespace: 'one' });
  client.resolveSessionId = id => id === 'other' ? 'session-two' : 'session-one';
  for (const id of ['picture', 'three', 'other']) await client.submit({ ...work, id });
  const runs = f.requests.filter(r => r.method === 'POST' && r.path.endsWith('/runs'));
  const scopes = runs.map(r => r.body.metadata.qwen_artifact_session);
  assert.match(scopes[0], /^[a-f0-9]{64}$/);
  assert.equal(scopes[0], scopes[1]);
  assert.notEqual(scopes[0], scopes[2]);
  assert.equal(f.threads.size, 3);
  assert.deepEqual(runs[0].body.input.messages, [{ role: 'user', name: 'voice_agent', content: work.instruction }]);
  const handle = await client.lookup('three');
  await client.createRun(handle.threadId, 'three', { input: { messages: [] } });
  assert.equal(f.requests.findLast(r => r.method === 'POST' && r.path.endsWith('/runs')).body.metadata.qwen_artifact_session, scopes[1]);
});

test('no session never shares a global latest image scope', async t => {
  const f = await serverFixture();
  t.after(() => f.close());
  const client = new AgentServerClient({ url: f.url, identity: 'local-developer' });
  await client.submit(work);
  const run = f.requests.find(r => r.method === 'POST' && r.path.endsWith('/runs'));
  assert.equal(run.body.metadata.qwen_artifact_session, undefined);
});
