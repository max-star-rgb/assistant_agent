import test from 'node:test';
import assert from 'node:assert/strict';
import { FrontendTrace, createFrontendTrace } from '../src/frontend-trace.mjs';
import { modelExecutionEvent, recordSentConfiguration } from '../src/frontend-execution-events.mjs';
import { patchFrontendObservation } from '../src/frontend-observation-patch.mjs';
import { loadConfig } from '../src/config.mjs';
import { createServer } from 'node:http';
import { once } from 'node:events';

test('initial provider handshake adopts the pending user turn without losing its history', async () => {
  const { trace, records, emit } = fixture();
  emit({ kind: 'turn.start', turnId: 'hello' });
  emit({ kind: 'input', turnId: 'hello', text: 'hi' });
  emit({ kind: 'session.start', providerSessionId: 'first-provider' });
  emit({ kind: 'session.start', providerSessionId: 'first-provider' });
  emit({ kind: 'model.start', turnId: 'hello', responseId: 'r' });
  emit({ kind: 'model.end', turnId: 'hello', responseId: 'r', status: 'completed', text: 'hello back' });
  await trace.close();
  const roots = [...records.values()].filter(r => r.name === 'FrontendAgent');
  assert.equal(roots.length, 1);
  assert.equal(roots[0].extra.metadata.provider_session_id, 'first-provider');
  assert.deepEqual(roots[0].inputs.messages.map(m => m.content), ['hi']);
  assert.deepEqual(roots[0].outputs.messages.map(m => m.content), ['hi', 'hello back']);
});

test('model snapshots include actual configuration and overrides', async () => {
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

test('cross-turn snapshots preserve dialogue and late ASR without future messages or cross-session history', async () => {
  const { trace, records, emit } = fixture();
  emit({ kind: 'session.start', providerSessionId: 'p1' });
  emit({ kind: 'turn.start', turnId: 't1' });
  emit({ kind: 'model.start', turnId: 't1', responseId: 'r1' });
  emit({ kind: 'model.end', turnId: 't1', responseId: 'r1', status: 'completed', text: 'first answer', itemIds: ['a1'] });
  emit({ kind: 'input', turnId: 't2', text: 'follow-up', itemId: 'u2' });
  emit({ kind: 'model.start', turnId: 't2', responseId: 'r2' });
  emit({ kind: 'input', turnId: 't1', text: 'late first question', itemId: 'u1' });
  emit({ kind: 'model.end', turnId: 't2', responseId: 'r2', status: 'completed', text: 'second answer' });
  emit({ kind: 'input', turnId: 't3', text: 'future question' });
  await trace.queue;
  const model = id => [...records.values()].find(r => r.run_type === 'llm' && r.extra.metadata.response_id === id);
  assert.deepEqual(model('r1').inputs.messages, [{ role: 'user', content: 'late first question' }]);
  assert.deepEqual(model('r2').inputs.messages.map(m => m.content), ['late first question', 'first answer', 'follow-up']);
  assert.equal(model('r2').inputs.observation.representation, 'observed_text_history');
  const before = structuredClone(model('r2').inputs);
  emit({ kind: 'session.start', providerSessionId: 'p2' });
  emit({ kind: 'input', turnId: 't4', text: 'new session' });
  emit({ kind: 'model.start', turnId: 't4', responseId: 'r4' });
  await trace.close();
  assert.deepEqual(model('r4').inputs.messages, [{ role: 'user', content: 'new session' }]);
  assert.deepEqual(model('r2').inputs, before);
  assert.equal(model('r4').extra.metadata.provider_session_id, 'p2');
  const root = [...records.values()].find(r => r.name === 'FrontendAgent' && r.extra.metadata.turn_id === 't2');
  assert.deepEqual(root.inputs.messages.map(m => m.content), ['late first question', 'first answer', 'follow-up']);
  assert.deepEqual(root.outputs.messages.map(m => m.content), ['late first question', 'first answer', 'follow-up', 'second answer']);
});

test('confirmed item removal changes future history only and history limits are visible', async () => {
  const { trace, records, emit } = fixture();
  emit({ kind: 'input', turnId: 't1', text: 'question', itemId: 'u1' });
  emit({ kind: 'model.start', turnId: 't1', responseId: 'r1' });
  emit({ kind: 'model.end', turnId: 't1', responseId: 'r1', status: 'completed', text: 'deleted answer', itemIds: ['a1'] });
  emit({ kind: 'history.remove', itemId: 'a1' });
  emit({ kind: 'history.remove', itemId: 'u1' });
  emit({ kind: 'input', turnId: 't1', text: 'late deleted question', itemId: 'u1' });
  for (let i = 0; i < 20; i++) emit({ kind: 'input', turnId: `long${i}`, text: 'x'.repeat(16000) });
  emit({ kind: 'model.start', turnId: 'long19', responseId: 'last' });
  await trace.close();
  const run = [...records.values()].find(r => r.extra.metadata.response_id === 'last');
  assert.ok(!JSON.stringify(run.inputs).includes('deleted'));
  assert.equal(run.inputs.observation.history_trimmed, true);
  assert.ok(JSON.stringify(run.inputs.messages).length < 140000);
});

test('provider lifecycle and deletion events are projected without response IDs', () => {
  assert.deepEqual(modelExecutionEvent({ type: 'session.created', session: { id: 'p1' } }), { kind: 'session.start', providerSessionId: 'p1' });
  for (const type of ['conversation.item.deleted', 'conversation.item.truncated']) {
    assert.deepEqual(modelExecutionEvent({ type, item_id: 'i1' }), { kind: 'history.remove', itemId: 'i1' });
  }
});

test('late ASR backfill also respects the snapshot size limit', async () => {
  const { trace, records, emit } = fixture();
  for (let i = 0; i < 20; i++) emit({ kind: 'turn.start', turnId: `t${i}` });
  emit({ kind: 'model.start', turnId: 't19', responseId: 'r' });
  for (let i = 0; i < 20; i++) emit({ kind: 'input', turnId: `t${i}`, text: 'x'.repeat(16000) });
  await trace.close();
  const run = [...records.values()].find(r => r.run_type === 'llm');
  assert.ok(JSON.stringify(run.inputs.messages).length < 140000);
  assert.equal(run.inputs.observation.history_trimmed, true);
});

test('actual upstream transport captures only accepted configuration, with immutable response snapshot', async () => {
  await patchFrontendObservation();
  await patchFrontendObservation();
  const { createRealtimeFrontend } = await import(new URL('../voice/realtime-provider.mjs', import.meta.resolve('qwen-audio-agent/gateway-application')));
  const events = [], wire = [];
  const frontend = createRealtimeFrontend({ providerName: 'dashscope', agentContext: { recentMessages: [] }, onEvent: e => events.push(e) });
  frontend.ws = { readyState: 1, bufferedAmount: 0, send: text => wire.push(JSON.parse(text)), terminate() {} };
  assert.equal(frontend.restoreRecentConversation(), undefined);
  assert.equal(wire.length, 0);
  const injected = frontend.createConversationItem({ type: 'message', role: 'user', content: [{ type: 'input_text', text: 'task result' }] });
  const item = wire.at(-1).item;
  frontend.handleProviderEvent({ type: 'conversation.item.created', item });
  await injected;
  assert.ok(events.some(e => e.type === 'observation.context.sent' && e.text === 'task result'));
  frontend.send({ type: 'session.update', session: { instructions: 'sent system', tools: [], api_key: 'SECRET' } });
  frontend.send({ type: 'conversation.item.create', item: { type: 'function_call_output', call_id: 'c', output: '{"value":1}' } });
  frontend.handleProviderEvent({ type: 'response.created', response: { id: 'r' } });
  const fact = modelExecutionEvent(events.at(-1), { turnId: 't' });
  assert.deepEqual(fact.toolResults, [{ call_id: 'c', output: '{"value":1}' }]);
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
  frontend.conversationItemWaiters.set('rejected-context', { contextOnly: true });
  frontend.send({ type: 'conversation.item.create', item: { id: 'rejected-context', type: 'message', role: 'user', content: [{ type: 'input_text', text: 'not sent context' }] } });
  assert.ok(!JSON.stringify(events).includes('not sent context'));
  frontend.send({ type: 'conversation.item.create', item: { type: 'function_call_output', call_id: 'rejected', output: 'not sent' } });
  frontend.send({ type: 'session.update', session: { instructions: 'not sent' } });
  assert.equal(frontend.observedSessionConfiguration.instructions, 'later system');
  assert.deepEqual(frontend.observedToolResults, [{ call_id: 'c', output: '{"value":1}' }]);
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

test('each root has immutable full input and output snapshots across delegation and notification', async () => {
  const { trace, records, emit } = fixture();
  emit({ kind: 'input', turnId: 't1', text: 'request' });
  emit({ kind: 'model.start', turnId: 't1', responseId: 'r1', configuration: { session: { instructions: 'system' }, response: {} } });
  emit({ kind: 'tool.start', turnId: 't1', responseId: 'r1', callId: 'c', name: 'spawn_thinking', input: {} });
  emit({ kind: 'tool.end', turnId: 't1', callId: 'c', taskId: 'task' });
  emit({ kind: 'model.end', turnId: 't1', responseId: 'r1', status: 'completed', hasTools: true });
  emit({ kind: 'model.start', turnId: 't1', responseId: 'r2', toolResults: [{ call_id: 'c', output: 'accepted' }] });
  emit({ kind: 'model.end', turnId: 't1', responseId: 'r2', status: 'completed', text: 'working' });
  await trace.queue;
  const first = [...records.values()].find(r => !r.parent_run_id);
  assert.ok(first.end_time, 'frontend run completes independently of background work');
  assert.deepEqual(first.inputs.messages.map(m => m.content), ['system', 'request']);
  assert.deepEqual(first.outputs.messages.map(m => m.role), ['system', 'user', 'assistant', 'tool', 'assistant']);
  const before = structuredClone(first);
  emit({ kind: 'history.message', itemId: 'notification', role: 'user', text: 'backend result' });
  emit({ kind: 'history.message', itemId: 'notification', role: 'user', text: 'backend result' });
  emit({ kind: 'model.start', turnId: 'announcement:a', responseId: 'a', origin: 'announcement', taskId: 'task' });
  emit({ kind: 'model.end', turnId: 'announcement:a', responseId: 'a', origin: 'announcement', taskId: 'task', status: 'completed', text: 'result summary' });
  emit({ kind: 'input', turnId: 't2', text: 'follow up' });
  emit({ kind: 'model.start', turnId: 't2', responseId: 'r3' });
  emit({ kind: 'model.end', turnId: 't2', responseId: 'r3', status: 'completed', text: 'final' });
  await trace.close();
  const roots = [...records.values()].filter(r => !r.parent_run_id);
  assert.equal(roots.length, 3);
  assert.deepEqual(first, before);
  assert.ok(roots.every(r => r.extra.metadata.thread_id === 's1'));
  assert.deepEqual(roots[1].inputs.messages.map(m => m.content), ['system', 'request', '', 'accepted', 'working', 'backend result']);
  assert.deepEqual(roots[2].outputs.messages.slice(-4).map(m => m.content), ['backend result', 'result summary', 'follow up', 'final']);
});

test('accepted context injection is observed without recording media or rejected sends', () => {
  const events = [];
  const frontend = { onEvent: e => events.push(e), conversationItemWaiters: new Map([['n', { contextOnly: true }]]) };
  recordSentConfiguration(frontend, { type: 'conversation.item.create', item: { id: 'n', type: 'message', role: 'user', content: [
    { type: 'input_text', text: 'backend notification' }, { type: 'input_audio', audio: 'PRIVATE_MEDIA' },
  ] } });
  assert.equal(events.length, 1);
  const fact = modelExecutionEvent(events[0]);
  assert.deepEqual(fact, { kind: 'history.message', itemId: 'n', role: 'user', text: 'backend notification' });
  assert.ok(!JSON.stringify(events).includes('PRIVATE_MEDIA'));
});


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


test('continuation shows the call and sent result, without changing root input', async () => {
  const { trace, records, emit } = fixture();
  emit({ kind: 'input', turnId: 't', text: 'request' });
  emit({ kind: 'model.start', turnId: 't', responseId: 'r1' });
  emit({ kind: 'tool.start', turnId: 't', responseId: 'r1', callId: 'c', name: 'lookup', input: { query: 'x' } });
  emit({ kind: 'tool.end', turnId: 't', callId: 'c', output: { value: 'raw result' } });
  emit({ kind: 'model.end', turnId: 't', responseId: 'r1', status: 'completed', hasTools: true });
  emit({ kind: 'model.start', turnId: 't', responseId: 'r2', toolResults: [{ call_id: 'c', output: '{"value":"sent result"}' }] });
  emit({ kind: 'model.end', turnId: 't', responseId: 'r2', status: 'completed', text: 'answer' });
  emit({ kind: 'input', turnId: 't', text: 'late request' });
  emit({ kind: 'input', turnId: 'next', text: 'follow-up' });
  emit({ kind: 'model.start', turnId: 'next', responseId: 'r3' });
  await trace.close();
  const root = [...records.values()].find(r => !r.parent_run_id);
  const models = [...records.values()].filter(r => r.run_type === 'llm');
  assert.deepEqual(models[0].outputs.message.tool_calls, [{ id: 'c', type: 'function', function: { name: 'lookup', arguments: '{"query":"x"}' } }]);
  assert.deepEqual(models[1].inputs.messages.map(m => m.role), ['user', 'assistant', 'tool']);
  assert.equal(models[1].inputs.messages[2].content, '{"value":"sent result"}');
  assert.equal(models[1].inputs.messages[2].tool_call_id, 'c');
  assert.equal(models[0].inputs.messages.length, 1);
  assert.deepEqual(models[2].inputs.messages.map(m => m.role), ['user', 'assistant', 'tool', 'assistant', 'user']);
  assert.equal(models[2].inputs.messages[2].content, '{"value":"sent result"}');
  assert.equal(root.inputs.messages[0].content, 'late request');
  assert.equal(root.outputs.messages.at(-1).content, 'answer');
  assert.ok(root.end_time >= models[1].end_time);
});

test('unsent results and hidden tool payloads never appear in continuation inputs', async () => {
  for (const options of [{}, { hideInputs: true }, { hideOutputs: true }]) {
    const { trace, records, emit } = fixture(options);
    emit({ kind: 'input', turnId: 't', text: 'request' });
    emit({ kind: 'model.start', turnId: 't', responseId: 'r1' });
    emit({ kind: 'tool.start', turnId: 't', responseId: 'r1', callId: 'c', name: 'lookup', input: {} });
    emit({ kind: 'tool.end', turnId: 't', callId: 'c', output: { value: 'raw' } });
    emit({ kind: 'model.end', turnId: 't', responseId: 'r1', status: 'completed', hasTools: true });
    if (Object.keys(options).length) emit({ kind: 'history.message', itemId: 'n', role: 'user', text: 'PRIVATE notification' });
    emit({ kind: 'model.start', turnId: 't', responseId: 'r2', toolResults: [
      { call_id: Object.keys(options).length ? 'c' : 'another-turn', output: '{"value":"PRIVATE","api_key":"SECRET"}' },
    ] });
    await trace.close();
    const continuation = [...records.values()].find(r => r.extra.metadata.response_id === 'r2');
    assert.ok(!JSON.stringify(continuation.inputs).includes('PRIVATE'));
    assert.ok(!JSON.stringify(continuation.inputs).includes('SECRET'));
    assert.ok(!continuation.inputs.messages?.some(m => m.role === 'tool'));
    if (Object.keys(options).length) assert.ok(!JSON.stringify([...records.values()]).includes('PRIVATE notification'));
  }
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
  const patches = requests.filter(r => r.method === 'PATCH' && r.path.endsWith(root.id));
  assert.equal(patches.length, 1, 'only the terminal snapshot is patched to ingestion');
  assert.ok(requests.filter(r => r.method === 'PATCH').every(r => r.body.end_time && r.body.outputs),
    'every update carries its actual terminal timestamp and outputs');
  assert.deepEqual(patches.at(-1).body.outputs.messages.map(m => m.content), ['hello', 'hi']);
  assert.ok(requests.some(r => r.method === 'PATCH' && r.path.endsWith(root.id) && r.body.end_time));
});
