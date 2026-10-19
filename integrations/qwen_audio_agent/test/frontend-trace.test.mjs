import test from 'node:test';
import assert from 'node:assert/strict';
import { FrontendTrace, createFrontendTrace } from '../src/frontend-trace.mjs';
import { modelExecutionEvent, recordSentConfiguration } from '../src/frontend-execution-events.mjs';
import { patchFrontendObservation } from '../src/frontend-observation-patch.mjs';
import { loadConfig } from '../src/config.mjs';
import { createServer } from 'node:http';
import { once } from 'node:events';

test('model snapshots include actual configuration and overrides, never conversation history', async () => {
  const { trace, records, emit } = fixture();
  const configuration = { session: { instructions: 'session system', tools: [{ type: 'function', function: {
    name: 'lookup', parameters: { type: 'object', properties: { token: { type: 'string' } } } } }] },
    response: { instructions: 'response override', tool_choice: 'none', max_output_tokens: 128 } };
  emit({ kind: 'input', turnId: 't', text: 'current input' });
  emit({ kind: 'model.start', turnId: 't', responseId: 'r', configuration });
  configuration.session.instructions = 'later mutation';
  emit({ kind: 'model.end', turnId: 't', responseId: 'r', status: 'completed' });
  await trace.close();
  const run = [...records.values()].find(r => r.run_type === 'llm');
  assert.equal(run.inputs.messages[0].role, 'system');
  assert.equal(run.inputs.messages[0].content, 'response override');
  assert.equal(run.inputs.session_configuration.instructions, 'session system');
  assert.equal(run.extra.invocation_params.tools[0].function.parameters.properties.token.type, 'string');
  assert.equal(run.extra.invocation_params.tool_choice, 'none');
  assert.equal(run.inputs.messages.length, 2);
  const hidden = fixture({ hideInputs: true });
  hidden.emit({ kind: 'model.start', turnId: 't', responseId: 'r', configuration });
  await hidden.trace.close();
  assert.ok(!JSON.stringify([...hidden.records.values()]).includes('response override'));
});

test('actual upstream transport captures only accepted configuration, with immutable response snapshot', async () => {
  await patchFrontendObservation();
  const { createRealtimeFrontend } = await import(new URL('../voice/realtime-provider.mjs', import.meta.resolve('qwen-audio-agent/gateway-application')));
  const events = [], wire = [];
  const frontend = createRealtimeFrontend({ providerName: 'dashscope', agentContext: { recentMessages: [] }, onEvent: e => events.push(e) });
  frontend.ws = { readyState: 1, bufferedAmount: 0, send: text => wire.push(JSON.parse(text)), terminate() {} };
  assert.equal(frontend.restoreRecentConversation(), undefined);
  assert.equal(wire.length, 0);
  frontend.send({ type: 'session.update', session: { instructions: 'sent system', tools: [], api_key: 'SECRET' } });
  frontend.handleProviderEvent({ type: 'response.created', response: { id: 'r' } });
  const configuration = events.at(-1).__executionConfiguration;
  assert.equal(configuration.session.instructions, 'sent system');
  assert.equal(configuration.session.api_key, undefined);
  frontend.send({ type: 'session.update', session: { instructions: 'later system' } });
  assert.equal(configuration.session.instructions, 'sent system');
  const pending = {};
  frontend.pendingResponses.push(pending);
  recordSentConfiguration(frontend, { type: 'response.create', response: { instructions: 'override', tool_choice: 'none' } });
  assert.equal(pending.observedConfiguration.response.instructions, 'override');
  assert.equal(pending.observedConfiguration.session.instructions, 'later system');
  frontend.pendingResponses = [];
  frontend.ws.readyState = 3;
  frontend.send({ type: 'session.update', session: { instructions: 'not sent' } });
  assert.equal(frontend.observedSessionConfiguration.instructions, 'later system');
});

function fixture(options = {}) {
  const records = new Map();
  const client = {
    async createRun(run) { records.set(run.id, structuredClone(run)); },
    async updateRun(id, patch) { Object.assign(records.get(id), structuredClone(patch)); },
  };
  const trace = new FrontendTrace({ client, project: 'offline', model: 'fixture', ...options });
  let time = 1700000000000;
  const emit = event => trace.onExecution({ sessionId: 's1', event: { time: time++, ...event } });
  return { trace, records, emit };
}

test('one turn contains tool continuation; late ASR patches its own completed root', async () => {
  const { trace, records, emit } = fixture();
  emit({ kind: 'turn.start', turnId: 't1' });
  emit({ kind: 'model.start', turnId: 't1', responseId: 'r1' });
  emit({ kind: 'tool.start', turnId: 't1', responseId: 'r1', callId: 'c1', name: 'lookup', input: { request: 'hello' } });
  emit({ kind: 'model.end', turnId: 't1', responseId: 'r1', status: 'completed', hasTools: true });
  emit({ kind: 'tool.end', turnId: 't1', callId: 'c1', output: { task_id: 'task_1' } });
  emit({ kind: 'model.start', turnId: 't1', responseId: 'r2' });
  emit({ kind: 'model.delta', turnId: 't1', responseId: 'r2', text: 'answer' });
  emit({ kind: 'model.end', turnId: 't1', responseId: 'r2', status: 'completed', text: 'answer' });
  emit({ kind: 'turn.start', turnId: 't2' });
  emit({ kind: 'input', turnId: 't1', text: 'late ASR' });
  await trace.close();
  const roots = [...records.values()].filter(r => !r.parent_run_id);
  const root = roots.find(r => r.extra.metadata.turn_id === 't1');
  assert.equal(roots.length, 2);
  assert.equal(root.inputs.messages[0].content, 'late ASR');
  assert.equal(root.outputs.status, 'completed');
  assert.equal([...records.values()].filter(r => r.parent_run_id === root.id).length, 3);
  assert.ok([...records.values()].every(r => r.end_time));
});

test('interrupt closes active children; duplicate end and close do not reopen traces', async () => {
  const { trace, records, emit } = fixture();
  emit({ kind: 'model.start', turnId: 't1', responseId: 'r1' });
  emit({ kind: 'interrupt' });
  emit({ kind: 'model.end', turnId: 't1', responseId: 'r1', status: 'completed' });
  await trace.close();
  assert.equal(records.size, 2);
  assert.ok([...records.values()].every(r => r.outputs.status === 'interrupted'));
});

test('privacy flags hide text and tool payloads, exporter failures stay observational', async () => {
  const { trace, records, emit } = fixture({ hideInputs: true, hideOutputs: true });
  emit({ kind: 'input', turnId: 't1', text: 'PRIVATE' });
  emit({ kind: 'tool.start', turnId: 't1', callId: 'c1', name: 'lookup', input: { secret: 'PRIVATE' } });
  emit({ kind: 'tool.end', turnId: 't1', callId: 'c1', output: 'PRIVATE' });
  await trace.close();
  assert.ok(!JSON.stringify([...records.values()]).includes('PRIVATE'));
  const failed = new FrontendTrace({ client: { createRun: async () => { throw Error('secret'); }, updateRun: async () => { throw Error('secret'); } } });
  failed.onExecution({ sessionId: 's', event: { kind: 'input', turnId: 't', text: 'x', time: Date.now() } });
  await failed.close();
});

test('tool-only completion waits for actual tool end; usage and final transcript survive', async () => {
  const { trace, records, emit } = fixture();
  emit({ kind: 'tool.start', turnId: 't', callId: 'c', name: 'lookup', input: { authorization: 'SECRET', request: 'ok' } });
  emit({ kind: 'model.start', turnId: 't', responseId: 'r' });
  emit({ kind: 'model.text', turnId: 't', responseId: 'r', text: '已提交' });
  emit({ kind: 'model.end', turnId: 't', responseId: 'r', status: 'completed', hasTools: false, usage: { input_tokens: 2, output_tokens: 3, total_tokens: 5 } });
  await trace.queue;
  assert.ok(![...records.values()].find(r => !r.parent_run_id).end_time);
  emit({ kind: 'tool.end', turnId: 't', callId: 'c', output: { status: 'accepted', audio: 'SECRET' } });
  await trace.queue;
  assert.equal([...records.values()].find(r => !r.parent_run_id).outputs.status, 'completed');
  const llm = [...records.values()].find(r => r.run_type === 'llm');
  assert.equal(llm.outputs.message.content, '已提交');
  assert.equal(llm.extra.metadata.usage_metadata.total_tokens, 5);
  assert.ok(!JSON.stringify([...records.values()]).includes('SECRET'));
  await trace.close();
});

test('projection excludes audio and separates external announcements; mock cannot enable tracing', () => {
  assert.equal(modelExecutionEvent({ type: 'response.audio.delta', response_id: 'r', delta: 'PCM' }, {}), null);
  const fact = modelExecutionEvent({ type: 'response.done', response: { id: 'r', status: 'completed',
    output: [{ type: 'message', content: [{ type: 'audio', audio: 'PCM', transcript: 'done' }] }] } },
    { turnId: 't', origin: 'announcement' });
  assert.equal(fact.turnId, 'announcement:r');
  assert.ok(!JSON.stringify(fact).includes('PCM'));
  assert.equal(loadConfig({ LANGSMITH_TRACING: 'true', LANGSMITH_API_KEY: 'present' }).tracing.enabled, false);
  const configured = loadConfig({ MULTIMODAL_AGENT_PROVIDER_MODE: 'real', QWEN_AUDIO_ENABLE_REAL: '1',
    DASHSCOPE_API_KEY: 'fixture', LANGSMITH_TRACING: 'true', LANGSMITH_PROJECT: 'selected', LANGSMITH_HIDE_INPUTS: 'true' });
  assert.equal(configured.tracing.enabled, true);
  assert.equal(configured.tracing.project, 'selected');
  assert.equal(configured.tracing.hideInputs, true);
});

test('official SDK delivers run tree to local HTTP fixture without any Provider', async t => {
  const requests = [];
  const server = createServer(async (req, res) => {
    let body = '';
    for await (const chunk of req) body += chunk;
    requests.push({ method: req.method, path: req.url, body: body ? JSON.parse(body) : {} });
    res.setHeader('Content-Type', 'application/json');
    res.end('{}');
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => new Promise(resolve => server.close(resolve)));
  const trace = createFrontendTrace({ enabled: true, endpoint: `http://127.0.0.1:${server.address().port}`,
    apiKey: 'local-fixture-only', project: 'offline-wire', model: 'fixture' });
  trace.onExecution({ sessionId: 's', event: { kind: 'input', turnId: 't', text: 'hello', time: Date.now() } });
  trace.onExecution({ sessionId: 's', event: { kind: 'model.start', turnId: 't', responseId: 'r', time: Date.now() } });
  trace.onExecution({ sessionId: 's', event: { kind: 'model.end', turnId: 't', responseId: 'r', status: 'completed', text: 'hi', time: Date.now() } });
  await trace.close();
  const creates = requests.filter(r => r.method === 'POST' && r.path === '/runs');
  assert.equal(creates.length, 2);
  const root = creates.find(r => r.body.name === 'FrontendAgent').body;
  const child = creates.find(r => r.body.name === 'QwenRealtime').body;
  assert.equal(child.parent_run_id, root.id);
  assert.equal(child.trace_id, root.id);
  assert.equal(root.session_name, 'offline-wire');
  assert.ok(requests.some(r => r.method === 'PATCH' && r.path.endsWith(root.id) && r.body.end_time));
});
