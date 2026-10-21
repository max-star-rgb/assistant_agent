import { homedir } from 'node:os';
import { dirname, isAbsolute, join } from 'node:path';
import { loopbackUrl } from './agent-server-client.mjs';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { createHash, randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';

export async function instanceNamespace(directory, { create = false } = {}) {
  const path = join(directory, 'integration-id');
  if (create) {
    await mkdir(directory, { recursive: true, mode: 0o700 });
    try { await writeFile(path, randomUUID(), { flag: 'wx', mode: 0o600 }); }
    catch (error) { if (error.code !== 'EEXIST') throw error; }
  }
  const value = (await readFile(path, 'utf8')).trim();
  if (!/^[a-f0-9-]{36}$/.test(value)) throw new Error('invalid_integration_namespace');
  return value;
}

export function loadConfig(env = process.env) {
  const mode = env.MULTIMODAL_AGENT_PROVIDER_MODE || 'mock';
  if (!['mock', 'real'].includes(mode)) throw new Error('Invalid provider mode');
  if (mode === 'real' && env.QWEN_AUDIO_ENABLE_REAL !== '1') throw new Error('Real Gateway requires operator QWEN_AUDIO_ENABLE_REAL=1');
  if (mode === 'real' && !env.DASHSCOPE_API_KEY?.trim()) throw new Error('DASHSCOPE_API_KEY is required');
  const model = env.QWEN_AUDIO_REALTIME_MODEL || 'qwen3.5-omni-flash-realtime';
  const realtimeBaseUrl = env.QWEN_AUDIO_REALTIME_BASE_URL?.trim();
  if (realtimeBaseUrl) {
    const endpoint = new URL(realtimeBaseUrl);
    if (endpoint.protocol !== 'wss:' || endpoint.username || endpoint.password || endpoint.hash) throw new Error('Invalid realtime WebSocket endpoint');
  }
  if (model !== 'qwen3.5-omni-flash-realtime') throw new Error('Unsupported realtime model');
  const port = Number(env.QWEN_AUDIO_PORT || 3101);
  if (!Number.isInteger(port) || port < 1024 || port > 65535 || port === 8088) throw new Error('Invalid Gateway port');
  const mediaPort = Number(env.QWEN_AUDIO_MEDIA_PORT || 8081);
  if (!Number.isInteger(mediaPort) || mediaPort < 1024 || mediaPort > 65535 || [port, 8088].includes(mediaPort)) throw new Error('Invalid media port');
  const stateDirectory = env.QWEN_AUDIO_STATE_DIR || join(homedir(), '.local/state/assistant_agent/qwen_audio');
  if (!isAbsolute(stateDirectory)) throw new Error('QWEN_AUDIO_STATE_DIR must be absolute');
  const identity = env.ASSISTANT_AGENT_USER || 'local-developer';
  if (!/^[a-zA-Z0-9_.@-]{1,128}$/.test(identity)) throw new Error('Invalid Agent Server identity');
  const tracing = Object.freeze({
    enabled: mode === 'real' && env.LANGSMITH_TRACING === 'true',
    apiKey: env.LANGSMITH_API_KEY?.trim(), endpoint: env.LANGSMITH_ENDPOINT?.trim(),
    project: env.LANGSMITH_PROJECT || 'default', model,
    hideInputs: env.LANGSMITH_HIDE_INPUTS === 'true', hideOutputs: env.LANGSMITH_HIDE_OUTPUTS === 'true',
  });
  const memoryBaseUrl = env.QWEN_AUDIO_MEMORY_BASE_URL?.trim() || 'https://dashscope.aliyuncs.com/compatible-mode/v1';
  const memoryEndpoint = new URL(memoryBaseUrl);
  if (!['https:', 'http:'].includes(memoryEndpoint.protocol) || memoryEndpoint.username || memoryEndpoint.password || memoryEndpoint.hash) throw Error('Invalid memory model endpoint');
  const memory = Object.freeze({ model: env.QWEN_AUDIO_MEMORY_MODEL?.trim() || 'qwen-flash', baseUrl: memoryBaseUrl,
    apiKey: mode === 'real' ? (env.QWEN_AUDIO_MEMORY_API_KEY?.trim() || env.DASHSCOPE_API_KEY.trim()) : '' });
  return Object.freeze({ mode, model, realtimeBaseUrl, port, mediaPort, stateDirectory, identity, tracing, memory,
    ownerId: 'user_personal', url: loopbackUrl(env.ASSISTANT_AGENT_URL || 'http://127.0.0.1:8088'),
    apiKey: mode === 'real' ? env.DASHSCOPE_API_KEY.trim() : '' });
}

export function upstreamEnvironment(config) {
  const memoryDirectory = join(config.stateDirectory, 'memory', createHash('sha256').update(config.identity).digest('hex'));
  return {
    QWEN_AUDIO_MEMORY_PROVIDER: 'markdown',
    QWEN_AUDIO_AGENT_USER_MODEL_PATH: join(memoryDirectory, 'USER.md'),
    QWEN_AUDIO_AGENT_MEMORY_PATH: join(memoryDirectory, 'MEMORY.md'),
    QWEN_AUDIO_MEMORY_MODEL: config.memory.model,
    QWEN_AUDIO_MEMORY_BASE_URL: config.memory.baseUrl,
    QWEN_AUDIO_MEMORY_API_KEY: config.memory.apiKey,
    QWEN_AUDIO_AGENT_RUNTIME_ROOT: join(config.stateDirectory, 'runtime'),
    QWAUDIO_CONFIG_DIR: join(config.stateDirectory, 'config'),
    QWAUDIO_DATA_DIR: join(config.stateDirectory, 'data'),
    QWAUDIO_STATE_DIR: join(config.stateDirectory, 'state'),
    QWAUDIO_CACHE_DIR: join(config.stateDirectory, 'cache'),
    AGENT_PROTOCOL: 'none', HOST: '0.0.0.0', PORT: String(config.port),
    QWEN_AUDIO_REALTIME_PROVIDER: 'dashscope', QWEN_AUDIO_REALTIME_MODEL: config.model,
    QWEN_AUDIO_AGENT_FRONTEND_PROMPT_DIR: fileURLToPath(new URL('../prompts/', import.meta.url)),
    QWEN_AUDIO_AGENT_ASSISTANT_PROFILE_PATH: fileURLToPath(new URL('../../../config/frontend-agent/ASSISTANT.md', import.meta.resolve('qwen-audio-agent/gateway-application'))),
    DASHSCOPE_API_KEY: config.apiKey,
    ...(config.realtimeBaseUrl ? { QWEN_AUDIO_REALTIME_BASE_URL: config.realtimeBaseUrl } : {}),
    QWEN_AUDIO_AGENT_PERSONAL_OWNER_ID: config.ownerId,
    QWEN_AUDIO_AGENT_IDENTITY_MODE: 'personal',
    QWEN_AUDIO_GATEWAY_LAN: '1', QWEN_AUDIO_GATEWAY_TAILNET: '0',
    QWEN_AUDIO_MEMORY_AUTO: 'on', QWEN_AUDIO_PREFERENCE_LEARNING: 'off',
    QWEN_AUDIO_SESSION_DIGEST: 'off', QWEN_AUDIO_DOMAIN_LIBRARY: 'off',
    QWEN_AUDIO_SCHEDULE_TOOL_ENABLED: 'off', QWEN_AUDIO_WEB_TOOLS_ENABLED: 'off',
    QWEN_AUDIO_KNOWLEDGE_TOOL_ENABLED: 'off', QWEN_AUDIO_NOTES_TOOL_ENABLED: 'off',
    QWEN_AUDIO_RECALL_TOOL_ENABLED: 'off',
  };
}

export async function initializeMemoryFiles(config) {
  const env = upstreamEnvironment(config);
  for (const path of [env.QWEN_AUDIO_AGENT_USER_MODEL_PATH, env.QWEN_AUDIO_AGENT_MEMORY_PATH]) {
    await mkdir(dirname(path), { recursive: true, mode: 0o700 });
    try {
      await writeFile(path, '', { flag: 'wx', mode: 0o600 });
    } catch (error) {
      if (error.code !== 'EEXIST') throw error;
    }
  }
}
