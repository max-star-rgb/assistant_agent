import { once } from 'node:events';
import { mkdir } from 'node:fs/promises';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { loadConfig, upstreamEnvironment, instanceNamespace } from './config.mjs';
import { AgentServerClient } from './agent-server-client.mjs';
import { LangGraphBackendAdapter } from './backend-adapter.mjs';
import { SessionBridge } from './session-bridge.mjs';
import { patchWebuiVisualState } from './webui-visual-state-patch.mjs';

// Deliberately omit arbitrary upstream logger fields (errors may contain payloads).
const logger = Object.fromEntries(['debug', 'info', 'warn', 'error'].map(level => [level,
  (event, fields = {}) => {
    if (level === 'debug') return;
    const safe = {};
    for (const key of ['taskId', 'ownerId', 'status', 'durationMs']) {
      if (typeof fields[key] === 'string' || typeof fields[key] === 'number') safe[key] = fields[key];
    }
    console.error(JSON.stringify({ level, event, ...safe }));
  }]));
// Upstream opens a child logger per WebSocket; retain the same redaction boundary.
logger.child = () => logger;

/** Process entrypoint: use one Gateway per process; upstream reads env at import. */
export async function startGateway(config, { backend } = {}) {
  if (config.mode !== 'real') throw new Error('mock mode: use npm test; live Gateway is disabled');
  await patchWebuiVisualState();
  const preserved = new Set(['PATH', 'HOME', 'USER', 'TMPDIR', 'LANG', 'TZ', 'SystemRoot']);
  for (const key of Object.keys(process.env)) if (!preserved.has(key)) delete process.env[key];
  Object.assign(process.env, upstreamEnvironment(config));
  // Runtime data is isolated, but the upstream frontend still needs its shipped prompts.
  process.env.QWEN_AUDIO_AGENT_FRONTEND_PROMPT_DIR = fileURLToPath(new URL(
    '../../../config/frontend-agent', import.meta.resolve('qwen-audio-agent/gateway-application')));
  process.env.QWEN_AUDIO_AGENT_ASSISTANT_PROFILE_PATH = `${process.env.QWEN_AUDIO_AGENT_FRONTEND_PROMPT_DIR}/ASSISTANT.md`;
  await mkdir(process.env.QWEN_AUDIO_AGENT_RUNTIME_ROOT, { recursive: true, mode: 0o700 });
  const { createBackendAgentHost } = await import('qwen-audio-agent/backend-adapter-sdk');
  const { createGatewayApplication } = await import('qwen-audio-agent/gateway-application');
  backend ||= new LangGraphBackendAdapter({
    client: new AgentServerClient({ url: config.url, identity: config.identity,
      namespace: await instanceNamespace(config.stateDirectory, { create: true }) }), ownerId: config.ownerId,
  });
  const bridge = new SessionBridge({ client: backend.client, ownerId: config.ownerId });
  const application = createGatewayApplication({
    agent: createBackendAgentHost(backend), autoStart: false, logger,
    memoryProvider: null, knowledgeProvider: null, frontendMcp: null, frontendOpenApi: null,
    webSearchProvider: null, publicEndpoint: null, webrtc: null,
    frontendToolSources: [bridge],
  });
  // Retain upstream history persistence while observing only completed user ASR.
  application.services.conversationSync.setRecordObserver((message, context) => {
    application.services.conversationHistory.append(message, context);
    void bridge.observe(message, context).catch(() => logger.warn('memory.error'));
  });
  let closing;
  const close = () => closing ||= (async () => {
    await backend.close();
    await application.close();
  })();
  try {
    const listening = once(application.server, 'listening');
    application.start({ host: '127.0.0.1', port: config.port });
    await listening;
    return { ...application, close };
  } catch (error) {
    await close();
    throw error;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const application = await startGateway(loadConfig());
    console.log(`Qwen Audio Gateway: http://127.0.0.1:${application.server.address().port}`);
    for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => {
      application.close().catch(() => { process.exitCode = 1; });
    });
  } catch (error) {
    console.error(error instanceof Error ? error.message : 'gateway_start_failed');
    process.exitCode = 1;
  }
}
