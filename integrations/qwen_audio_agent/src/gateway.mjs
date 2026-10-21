import { once } from 'node:events';
import { mkdir, readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import { loadConfig, upstreamEnvironment, instanceNamespace, initializeMemoryFiles } from './config.mjs';
import { AgentServerClient } from './agent-server-client.mjs';
import { LangGraphBackendAdapter } from './backend-adapter.mjs';
import { SessionBridge } from './session-bridge.mjs';
import { VisualMemoryBridge } from './visual-memory-bridge.mjs';
import { patchFrontendMemoryContext } from './frontend-memory-context.mjs';
import { patchFrontendConcurrency } from './frontend-concurrency-patch.mjs';
import { patchWebuiVisualState } from './webui-visual-state-patch.mjs';
import { patchFrontendObservation } from './frontend-observation-patch.mjs';
import { createFrontendTrace } from './frontend-trace.mjs';
import { startMediaServer } from './media-entry.mjs';

// Deliberately omit arbitrary upstream logger fields (errors may contain payloads).
const logger = Object.fromEntries(['debug', 'info', 'warn', 'error'].map(level => [level,
  (event, fields = {}) => {
    if (level === 'debug') return;
    const safe = {};
    for (const key of ['taskId', 'ownerId', 'status', 'durationMs']) {
      if (typeof fields[key] === 'string' || typeof fields[key] === 'number') safe[key] = fields[key];
    }
    console.error(JSON.stringify({ time: new Date().toISOString(), level, event, ...safe }));
  }]));
// Upstream opens a child logger per WebSocket; retain the same redaction boundary.
logger.child = () => logger;

/** Process entrypoint: use one Gateway per process; upstream reads env at import. */
export async function startGateway(config, { backend, traceObserver } = {}) {
  if (config.mode !== 'real') throw new Error('mock mode: use npm test; live Gateway is disabled');
  await patchWebuiVisualState();
  await patchFrontendObservation();
  await patchFrontendMemoryContext();
  await patchFrontendConcurrency();
  const preserved = new Set(['PATH', 'HOME', 'USER', 'TMPDIR', 'LANG', 'TZ', 'SystemRoot']);
  for (const key of Object.keys(process.env)) if (!preserved.has(key)) delete process.env[key];
  Object.assign(process.env, upstreamEnvironment(config));
  const spawnThinkingDescription = (await readFile(new URL('../prompts/BACKEND_TASK.md', import.meta.url), 'utf8')).trim();
  if (!spawnThinkingDescription) throw new Error('BACKEND_TASK.md must not be empty');
  await mkdir(process.env.QWEN_AUDIO_AGENT_RUNTIME_ROOT, { recursive: true, mode: 0o700 });
  await initializeMemoryFiles(config);
  const { createBackendAgentHost } = await import('qwen-audio-agent/backend-adapter-sdk');
  const { createGatewayApplication } = await import('qwen-audio-agent/gateway-application');
  const { config: upstreamConfig } = await import(new URL('../core/config.mjs', import.meta.resolve('qwen-audio-agent/gateway-application')));
  backend ||= new LangGraphBackendAdapter({
    client: new AgentServerClient({ url: config.url, identity: config.identity,
      namespace: await instanceNamespace(config.stateDirectory, { create: true }) }), ownerId: config.ownerId,
  });
  const bridge = new SessionBridge({ client: backend.client, ownerId: config.ownerId });
  await bridge.initialize();
  const visualMemory = new VisualMemoryBridge({ client: backend.client, ownerId: config.ownerId });
  const trace = traceObserver ?? createFrontendTrace({ ...config.tracing, namespace: backend.client.namespace }, (event, fields) => logger.warn(event, fields));
  const application = createGatewayApplication({
    config: { ...upstreamConfig, frontendDisabledTools: [...upstreamConfig.frontendDisabledTools, 'get_current_time'] },
    agent: createBackendAgentHost(backend), autoStart: false, logger,
    knowledgeProvider: null, frontendMcp: null, frontendOpenApi: null,
    // Search uses the shared Bailian MCP tool; do not construct legacy retrieval providers.
    webSearchProvider: null, urlFetcher: null, webrtc: null,
    frontendToolSources: [bridge, visualMemory],
    spawnThinkingDescription,
    sessionObservers: trace ? [trace] : [],
  });
  application.services.conversationSync.setRecordObserver((message, context) => {
    application.services.conversationHistory.append(message, context);
    if (message.role === 'user' && message.turnId) trace?.onExecution({ sessionId: context.sessionId,
      event: { kind: 'input', turnId: message.turnId, text: message.content, time: Date.now() } });
  });
  let closing, media;
  const close = () => closing ||= (async () => {
    await media?.close();
    await application.close();
    await bridge.close();
    await visualMemory.close();
    await backend.close();
    await trace?.close();
  })();
  try {
    const listening = once(application.server, 'listening');
    application.start({ host: '0.0.0.0', port: config.port });
    await listening;
    media = await startMediaServer({ port: config.mediaPort, gatewayPort: application.server.address().port,
      backendUrl: config.url, identity: config.identity, logger });
    // Route media before upstream's GCP handler, which rejects every other path.
    const upgrades = application.server.listeners('upgrade');
    application.server.removeAllListeners('upgrade');
    application.server.on('upgrade', (request, socket, head) => {
      if (request.url?.split('?')[0] === '/agent-service/v1') media.upgrade(request, socket, head);
      else for (const handler of upgrades) handler.call(application.server, request, socket, head);
    });
    return { ...application, mediaServer: media.server, close };
  } catch (error) {
    await close();
    throw error;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const application = await startGateway(loadConfig());
    console.log(`Qwen Audio Gateway (LAN): http://0.0.0.0:${application.server.address().port}`);
    console.log(`Media entry: ws://0.0.0.0:${application.mediaServer.address().port}/agent-service/v1`);
    for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => {
      application.close().catch(() => { process.exitCode = 1; });
    });
  } catch (error) {
    console.error(error instanceof Error ? error.message : 'gateway_start_failed');
    process.exitCode = 1;
  }
}
