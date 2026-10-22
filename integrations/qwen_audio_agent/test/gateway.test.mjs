import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, readFile } from 'node:fs/promises';
import WebSocket from 'ws';
import { once } from 'node:events';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loadConfig, upstreamEnvironment, instanceNamespace } from '../src/config.mjs';
import { startGateway } from '../src/gateway.mjs';
import { serverFixture, work } from './fixture.mjs';
import { AgentServerClient } from '../src/agent-server-client.mjs';
import { LangGraphBackendAdapter } from '../src/backend-adapter.mjs';
import { setTimeout as delay } from 'node:timers/promises';
import { FrontendTrace } from '../src/frontend-trace.mjs';

test('default mock blocks Gateway before loading upstream or opening a connection', async () => {
  const config = loadConfig({ DASHSCOPE_API_KEY: 'present-but-not-authorized' });
  assert.equal(config.mode, 'mock');
  assert.equal(config.url, 'http://127.0.0.1:8088');
  await assert.rejects(startGateway(config), /mock/);
});

test('real requires explicit operator opt-in, key, fixed model and loopback server', () => {
  assert.throws(() => loadConfig({ MULTIMODAL_AGENT_PROVIDER_MODE: 'real' }), /operator/);
  assert.throws(() => loadConfig({ MULTIMODAL_AGENT_PROVIDER_MODE: 'real', QWEN_AUDIO_ENABLE_REAL: '1' }), /API_KEY/);
  assert.throws(() => loadConfig({ ASSISTANT_AGENT_URL: 'https://example.com' }), /loopback/);
  assert.equal(loadConfig({ QWEN_AUDIO_PORT: '8089' }).port, 8089);
  assert.throws(() => loadConfig({ QWEN_AUDIO_PORT: '8088' }), /port/);
  assert.throws(() => loadConfig({ QWEN_AUDIO_REALTIME_MODEL: 'another-model' }), /model/);
});

test('upstream receives only selected credentials and disabled optional services', () => {
  const endpoint = 'wss://example.com/api-ws/v1/realtime';
  assert.equal(upstreamEnvironment(loadConfig({ QWEN_AUDIO_REALTIME_BASE_URL: endpoint })).QWEN_AUDIO_REALTIME_BASE_URL, endpoint);
  assert.throws(() => loadConfig({ QWEN_AUDIO_REALTIME_BASE_URL: 'http://example.com' }), /endpoint/);
  const config = loadConfig({ MULTIMODAL_AGENT_PROVIDER_MODE: 'real', QWEN_AUDIO_ENABLE_REAL: '1', DASHSCOPE_API_KEY: 'chosen' });
  const env = upstreamEnvironment(config);
  assert.equal(env.DASHSCOPE_API_KEY, 'chosen');
  assert.equal(env.AGENT_PROTOCOL, 'none');
  assert.equal(env.QWEN_AUDIO_MEMORY_AUTO, 'on');
  assert.equal(env.QWEN_AUDIO_MEMORY_PROVIDER, 'markdown');
  assert.equal(env.QWEN_AUDIO_GATEWAY_LAN, '1');
  assert.equal(env.HOST, '0.0.0.0');
  assert.equal(env.QWEN_AUDIO_GATEWAY_TAILNET, '0');
  assert.equal(env.OPENAI_API_KEY, undefined);
});

test('installation namespace persists and separates independent task counters', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'qwen-namespace-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  await assert.rejects(instanceNamespace(directory), /ENOENT/);
  const first = await instanceNamespace(directory, { create: true });
  assert.equal(await instanceNamespace(directory), first);
  assert.notEqual(await instanceNamespace(join(directory, 'second'), { create: true }), first);
});

test('actual Gateway startup, occupied port and shutdown with a local HTTP backend', async t => {
  const stateDirectory = await mkdtemp(join(tmpdir(), 'qwen-integration-test-'));
  const server = await serverFixture({ memory: true, search: true });
  const config = { ...loadConfig({}), mode: 'real', apiKey: '', port: 0, mediaPort: 0, stateDirectory };
  const backend = new LangGraphBackendAdapter({ client: new AgentServerClient({ url: server.url, identity: 'local-developer' }), ownerId: work.ownerId });
  const traceRecords = new Map();
  const traceObserver = new FrontendTrace({ client: {
    async createRun(run) { traceRecords.set(run.id, structuredClone(run)); },
    async updateRun(id, patch) { Object.assign(traceRecords.get(id), structuredClone(patch)); },
  } });
  const app = await startGateway(config, { backend, traceObserver });
  t.after(async () => { await app.close(); await server.close(); await rm(stateDirectory, { recursive: true, force: true }); });
  const port = app.server.address().port;
  assert.equal(app.server.address().address, '0.0.0.0');
  for (const mediaPort of [port, app.mediaServer.address().port]) {
    for (const query of ['', '?probe=route']) {
      const mediaSocket = new WebSocket(`ws://127.0.0.1:${mediaPort}/agent-service/v1${query}`);
      await once(mediaSocket, 'open');
      const mediaReply = once(mediaSocket, 'message');
      mediaSocket.send(JSON.stringify({ message: 'assistantControl', body: JSON.stringify({ number: 'fixture', context: { unsupported: true } }) }));
      const mediaError = JSON.parse((await mediaReply)[0]);
      assert.match(JSON.parse(mediaError.body).message, /business context/);
      mediaSocket.close();
      await once(mediaSocket, 'close');
    }
  }
  assert.ok(app.mediaServer.address().port > 0);
  assert.ok((await readFile(`${process.env.QWEN_AUDIO_AGENT_FRONTEND_PROMPT_DIR}/PROMPT.md`, 'utf8')).trim());
  assert.ok((await readFile(process.env.QWEN_AUDIO_AGENT_ASSISTANT_PROFILE_PATH, 'utf8')).trim());
  const ws = new WebSocket(`ws://127.0.0.1:${port}/api/realtime`, { headers: { Origin: `http://127.0.0.1:${port}` } });
  await once(ws, 'open');
  ws.close();
  await once(ws, 'close');
  const response = await fetch(`http://127.0.0.1:${port}/`);
  assert.equal(response.status, 200);
  // Real upstream Runtime, fake model transport: no Provider/network inference.
  let provider;
  const toolOutputs = new Map();
  const deliveries = [];
  const contexts = [];
  const session = app.services.frontendRuntime.createSession({
    ownerId: work.ownerId, sessionId: 'trace-fixture', send() {}, actionCapabilities: {},
    voiceAccess: { claim: () => ({ granted: true }), release: () => false, changed() {}, isActive: () => true },
    realtimeFrontendFactory(options) {
      assert.deepEqual(options.agentContext.recentMessages, [], 'provider history must not be rebuilt locally');
      assert.deepEqual(options.agentContext.visualMemory, ['离线视觉快照'], 'initial snapshot precedes connection');
      provider = options;
      return { ready: true, provider: { key: 'dashscope', inputSampleRate: 16000 }, capabilities: { perResponseInstructions: true },
        connect: async () => {}, updateAgentContext(context) { contexts.push(context); }, setInputMuted() {}, cancel() {}, close() {},
        appendUserInputContext: async () => {}, cancelResponses() {},
        injectDelivery: async (text, origin, context, options) => { deliveries.push({ text, origin, context, options }); return { completed: true }; },
        ensureResponse: async () => {},
        sendUserInput: async () => ({ completed: true }),
        clearPendingImage() {}, sendFunctionOutput: async (callId, output) => { toolOutputs.set(callId, output); },
      };
    },
  });
  session.start();
  session.handleClientEvent({ type: 'connect', inputEnabled: false, outputEnabled: true, textOnly: true });
  for (let n = 0; n < 100 && !provider; n++) await delay(1);
  assert.ok(provider, 'upstream actually connected to the fixture model');
  server.setVisualMemory(['更新后的视觉快照']);
  for (let n = 0; n < 600 && !contexts.some(c => c.visualMemory?.includes('更新后的视觉快照')); n++) await delay(5);
  assert.ok(contexts.some(c => c.visualMemory?.includes('更新后的视觉快照')), 'visual snapshot refresh reaches native realtime context');
  const { frontendTools } = await import('../node_modules/qwen-audio-agent/server/src/frontend/frontend-tools.mjs');
  const toolNames = frontendTools(provider.agentContext).map(tool => tool.function.name);
  assert.ok(toolNames.includes('memory'));
  assert.ok(!toolNames.includes('recall_memory'));
  assert.ok(toolNames.includes('mcp_bailian_bailian_web_search'), 'frontend must expose the shared Bailian MCP tool');
  assert.ok(!toolNames.includes('respond_to_approval'));
  assert.ok(!toolNames.includes('get_pending_approvals'));
  assert.ok(frontendTools({ ...provider.agentContext, frontend: { ...provider.agentContext.frontend,
    capabilities: ['permission.respond'] } }).some(t => t.function.name === 'respond_permission'));
  assert.ok(!toolNames.includes('web_search'));
  assert.ok(!toolNames.includes('fetch_url'));
  assert.ok(!toolNames.includes('get_current_time'));
  assert.ok(!toolNames.includes('retry_task_delivery'));
  assert.deepEqual(app.services.frontendRetrieval.capabilities(), []);
  provider.onEvent({ type: 'input_audio_buffer.speech_started', item_id: 'i1' });
  provider.onEvent({ type: 'input_audio_buffer.speech_stopped', item_id: 'i1' });
  provider.onEvent({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'i1', transcript: '你好' });
  provider.onEvent({ type: 'response.created', response: { id: 'r1' } });
  provider.onEvent({ type: 'response.function_call_arguments.done', response_id: 'r1', call_id: 'c1', name: 'mcp_bailian_bailian_web_search', arguments: '{"query":"测试"}' });
  provider.onEvent({ type: 'response.done', response: { id: 'r1', status: 'completed', output: [{ type: 'function_call' }] } });
  for (let n = 0; n < 100 && ![...traceRecords.values()].some(r => r.run_type === 'tool' && r.end_time); n++) await delay(1);
  provider.onEvent({ type: 'response.created', response: { id: 'r2' } });
  provider.onEvent({ type: 'response.done', response: { id: 'r2', status: 'completed', output: [{ type: 'message', content: [{ type: 'text', text: '你好！' }] }] } });
  await traceObserver.queue;
  const root = [...traceRecords.values()].find(r => r.name === 'FrontendAgent');
  assert.equal(root.inputs.messages[0].content, '你好');
  assert.equal(root.outputs.status, 'completed');
  assert.equal([...traceRecords.values()].filter(r => r.run_type === 'llm').length, 2);
  assert.equal([...traceRecords.values()].filter(r => r.run_type === 'tool').length, 1);
  async function callTool(id, name, args) {
    provider.onEvent({ type: 'response.created', response: { id } });
    provider.onEvent({ type: 'response.function_call_arguments.done', response_id: id, call_id: id, name, arguments: JSON.stringify(args) });
    provider.onEvent({ type: 'response.done', response: { id, status: 'completed', output: [{ type: 'function_call' }] } });
    for (let n = 0; n < 200 && !toolOutputs.has(id); n++) await delay(5);
    assert.ok(toolOutputs.has(id));
    const output = toolOutputs.get(id);
    return typeof output === 'string' ? JSON.parse(output) : output;
  }
  const searchResult = await callTool('search', 'mcp_bailian_bailian_web_search', { query: '离线查询' });
  assert.deepEqual(searchResult, { result: '离线搜索结果' }, 'external tools without optional policy must deliver their result');
  assert.equal(server.requests.filter(r => r.path.endsWith('/transcripts')).length, 0);
  session.handleClientEvent({ type: 'text.message', text: '网页文字也要进入记忆' });
  await delay(0);
  session.close();
  session.close();
  assert.equal(server.requests.filter(r => r.path.endsWith('/transcripts')).length, 0);
  const task = app.services.runtimeCommands.createTask({ event_id: 'integration-submit', message: { parts: [{ type: 'text', text: '测试委派' }] } }, { ownerId: work.ownerId });
  for (let n = 0; n < 100 && app.services.runtimeCommands.getTask(task.id, { ownerId: work.ownerId }).status !== 'completed'; n++) await delay(10);
  const completed = app.services.runtimeCommands.getTask(task.id, { ownerId: work.ownerId });
  assert.equal(completed.status, 'completed');
  assert.equal(backend.client.resolveSessionId(task.id), completed.sessionId);
  assert.match(server.requests.find(r => r.path.endsWith('/runs') && r.body?.context)
    .body.metadata.qwen_artifact_session, /^[a-f0-9]{64}$/);
  assert.equal(server.requests.find(r => r.path.endsWith('/runs') && r.body?.context).body.context.require_tool_approval, false);
  assert.equal(deliveries.some(d => d.origin === 'permission'), false);
  assert.equal(server.requests.filter(r => r.method === 'POST' && r.path.endsWith('/runs')).length, 1);
  app.services.conversationSync.record({ ownerId: work.ownerId, sessionId: 'integration-session', id: 'asr1', source: 'voice-user', role: 'user', content: '我是小明' });
  assert.equal(server.requests.some(r => r.path.endsWith('/transcripts') && r.body.session_id === 'integration-session'), false);
  // Upstream maps cancellation rejection to FAILED. It must not claim cancellation.
  let entered, release;
  const polled = new Promise(resolve => { entered = resolve; });
  const gate = new Promise(resolve => { release = resolve; });
  const originalRun = backend.client.run.bind(backend.client);
  backend.client.run = async handle => { entered(); await gate; return originalRun(handle); };
  const racing = app.services.runtimeCommands.createTask({ event_id: 'cancel-race', message: { parts: [{ type: 'text', text: '取消竞争' }] } }, { ownerId: work.ownerId });
  await polled;
  await app.services.runtimeCommands.cancelTask(racing.id, { ownerId: work.ownerId });
  for (let n = 0; n < 100 && app.services.runtimeCommands.getTask(racing.id, { ownerId: work.ownerId }).status === 'cancelling'; n++) await delay(10);
  const cancelled = app.services.runtimeCommands.getTask(racing.id, { ownerId: work.ownerId });
  assert.equal(cancelled.status, 'failed');
  assert.match(cancelled.error, /already_finished/);
  assert.equal(server.requests.filter(r => r.path.endsWith('/cancel')).length, 0);
  release();
  backend.client.run = originalRun;
  const conflict = { ...config, port };
  const otherBackend = new LangGraphBackendAdapter({ client: new AgentServerClient({ url: server.url, identity: 'local-developer' }), ownerId: work.ownerId });
  await assert.rejects(startGateway(conflict, { backend: otherBackend }), error => error.code === 'EADDRINUSE');
  const mediaConflictBackend = new LangGraphBackendAdapter({ client: new AgentServerClient({ url: server.url, identity: 'local-developer' }), ownerId: work.ownerId });
  await assert.rejects(startGateway({ ...config, port: 0, mediaPort: app.mediaServer.address().port },
    { backend: mediaConflictBackend }), error => error.code === 'EADDRINUSE');
  await app.close();
  await app.close();
  assert.equal(app.server.listening, false);
  assert.equal(app.mediaServer.listening, false);
});
