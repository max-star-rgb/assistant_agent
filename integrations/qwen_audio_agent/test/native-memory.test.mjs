import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, readFile, writeFile, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loadConfig, upstreamEnvironment, initializeMemoryFiles } from '../src/config.mjs';
import { createConfiguredMemoryProvider } from '../node_modules/qwen-audio-agent/server/src/memory/provider-factory.mjs';
import { patchFrontendMemoryContext } from '../src/frontend-memory-context.mjs';
await patchFrontendMemoryContext();
const { createMemoryModule } = await import('../node_modules/qwen-audio-agent/server/src/memory/module.mjs');
import { ConversationSync } from '../node_modules/qwen-audio-agent/server/src/conversation/conversation-sync.mjs';

test('memory initialization creates private empty files without replacing saved content', async t => {
  const dir = await mkdtemp(join(tmpdir(), 'memory-init-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const config = loadConfig({ QWEN_AUDIO_STATE_DIR: dir });
  const env = upstreamEnvironment(config);
  await Promise.all([initializeMemoryFiles(config), initializeMemoryFiles(config)]);
  for (const path of [env.QWEN_AUDIO_AGENT_USER_MODEL_PATH, env.QWEN_AUDIO_AGENT_MEMORY_PATH]) {
    assert.equal(await readFile(path, 'utf8'), '');
    assert.equal((await stat(path)).mode & 0o777, 0o600);
    await writeFile(path, 'existing memory');
  }
  await initializeMemoryFiles(config);
  assert.equal(await readFile(env.QWEN_AUDIO_AGENT_USER_MODEL_PATH, 'utf8'), 'existing memory');
  assert.equal(await readFile(env.QWEN_AUDIO_AGENT_MEMORY_PATH, 'utf8'), 'existing memory');
});

test('native session-close extraction persists once and explicit edits invalidate old evidence', async t => {
  const dir = await mkdtemp(join(tmpdir(), 'native-extraction-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const config = { memoryProvider: 'markdown', userModelPath: join(dir, 'USER.md'), frontendMemoryPath: join(dir, 'MEMORY.md'), personalOwnerId: 'user_personal', preferenceLearningEnabled: false };
  const sync = new ConversationSync();
  let calls = 0, fail = false;
  const module = createMemoryModule({ config, logger: { warn() {} }, conversationSync: sync, textModelCall: async () => {
    calls++;
    if (fail) throw Error('model unavailable');
    return JSON.stringify({ changes: [{ document: 'memory', edits: [], append: '- 用户养了一只猫。' }] });
  } });
  t.after(() => module.close());
  const context = { ownerId: 'user_personal', sessionId: 's', logger: { warn() {} } };
  const record = sessionId => { for (let i = 0; i < 4; i++) sync.record({ ownerId: context.ownerId, sessionId, id: `u${i}`, role: 'user', source: 'voice-user', content: `我养了一只猫，第${i}条补充。` }); };
  record('s');
  assert.equal(calls, 0);
  await module.sessionObservers[0].onSessionClosed(context);
  await module.sessionObservers[0].onSessionClosed(context);
  assert.equal(calls, 1);
  assert.match(module.services.frontendMemory.list(context.ownerId)[0].content, /养了一只猫/);
  let notices = 0;
  module.services.frontendMemory.subscribe(() => notices++);
  record('stale');
  await module.services.frontendMemory.apply(context.ownerId, [{ document: 'user', append: '- 请简短回答。' }], { source: 'realtime-tool', sessionId: 'stale' });
  await module.sessionObservers[0].onSessionClosed({ ...context, sessionId: 'stale' });
  assert.equal(calls, 1);
  assert.equal(notices, 1);
  record('next');
  await module.sessionObservers[0].onSessionClosed({ ...context, sessionId: 'next' });
  assert.equal(calls, 2, 'fresh session must extract immediately without a cooldown');
  for (let i = 0; i < 4; i++) sync.record({ ownerId: 'other', sessionId: 'failure', id: `f${i}`, role: 'user', source: 'voice-user', content: '我喜欢茶。' });
  fail = true;
  await module.sessionObservers[0].onSessionClosed({ ...context, ownerId: 'other', sessionId: 'failure' });
  assert.equal(calls, 3);
  assert.deepEqual(module.services.frontendMemory.list('other'), []);
});

test('configured Markdown documents persist across restart and isolate identities', async t => {
  const dir = await mkdtemp(join(tmpdir(), 'native-memory-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const env = identity => upstreamEnvironment(loadConfig({ QWEN_AUDIO_STATE_DIR: dir, ASSISTANT_AGENT_USER: identity }));
  const first = env('alice'), second = env('bob');
  assert.equal(first.QWEN_AUDIO_MEMORY_PROVIDER, 'markdown');
  assert.notEqual(first.QWEN_AUDIO_AGENT_MEMORY_PATH, second.QWEN_AUDIO_AGENT_MEMORY_PATH);
  const provider = values => createConfiguredMemoryProvider({ config: { memoryProvider: values.QWEN_AUDIO_MEMORY_PROVIDER,
    userModelPath: values.QWEN_AUDIO_AGENT_USER_MODEL_PATH, frontendMemoryPath: values.QWEN_AUDIO_AGENT_MEMORY_PATH,
    personalOwnerId: 'user_personal' }, logger: { warn() {} } });
  const memory = provider(first);
  memory.apply('user_personal', [{ document: 'memory', append: '- 喜欢茉莉花茶' }]);
  const revision = memory.list('user_personal')[0].revision;
  memory.apply('user_personal', [{ document: 'memory', append: '- 养了一只猫' }]);
  assert.throws(() => memory.apply('user_personal', [{ document: 'memory', append: '旧修改', expectedRevision: revision }]), /changed/);
  assert.match(provider(first).list('user_personal')[0].content, /茉莉花茶/);
  assert.deepEqual(provider(second).list('user_personal'), []);
});

test('mock never forwards extraction credentials and real passes explicit model configuration', () => {
  const settings = { QWEN_AUDIO_MEMORY_API_KEY: 'private', QWEN_AUDIO_MEMORY_MODEL: 'chosen-model', QWEN_AUDIO_MEMORY_BASE_URL: 'https://example.com/v1' };
  assert.equal(upstreamEnvironment(loadConfig(settings)).QWEN_AUDIO_MEMORY_API_KEY, '');
  const real = upstreamEnvironment(loadConfig({ ...settings, MULTIMODAL_AGENT_PROVIDER_MODE: 'real', QWEN_AUDIO_ENABLE_REAL: '1', DASHSCOPE_API_KEY: 'voice' }));
  assert.equal(real.QWEN_AUDIO_MEMORY_API_KEY, 'private');
  assert.equal(real.QWEN_AUDIO_MEMORY_MODEL, 'chosen-model');
  assert.equal(real.QWEN_AUDIO_MEMORY_BASE_URL, 'https://example.com/v1');
});
