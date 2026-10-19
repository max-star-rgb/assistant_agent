import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';

// 2.0.1's public tool sources lack a prompt/refresh hook; keep the patch version-bound.
export async function patchFrontendMemoryContext() {
  const root = fileURLToPath(new URL('../../../', import.meta.resolve('qwen-audio-agent/gateway-application')));
  const { version } = JSON.parse(await readFile(join(root, 'package.json'), 'utf8'));
  if (version !== '2.0.1') throw Error('review_frontend_memory_patch_for_new_upstream');
  const path = join(root, 'server/src/voice/realtime-session-runtime.mjs');
  let text = await readFile(path, 'utf8');
  const original = text;
  const changes = [
    ['    memories: memoryService?.list(ownerId, { limit: 64 }) || [],\n    recentMessages:',
      '    memories: [...(memoryService?.list(ownerId, { limit: 64 }) || []), ...frontendToolSources.flatMap(source => source.memorySnapshot?.(ownerId) || [])],\n    recentMessages:'],
    ['      frontendToolSourcesReady.then(() => {',
      '      frontendToolSourcesReady.then(async () => {\n        await Promise.all(frontendToolSources.map(source => source.refreshSnapshot?.()))'],
    ['        await Promise.all(frontendToolSources.map(source => source.refreshSnapshot?.()))',
      '        await Promise.all(frontendToolSources.map(source => source.refreshSnapshot?.()))\n        await Promise.all(frontendToolSources.map(source => source.beginMemorySession?.({ ownerId, sessionId })))'],
    ['  const unsubscribeMemory = typeof memoryService?.subscribe',
      '  const unsubscribeExternalMemory = frontendToolSources.map(source => source.subscribeMemory?.(() => realtimeSession.updateAgentContext(getAgentContext())))\n  const unsubscribeMemory = typeof memoryService?.subscribe'],
    ['      unsubscribeMemory()', '      unsubscribeMemory()\n      unsubscribeExternalMemory.forEach(unsubscribe => unsubscribe?.())'],
  ];
  for (const [before, after] of changes) {
    if (text.includes(after)) continue;
    if (text.split(before).length !== 2) throw Error('frontend_memory_patch_target_changed');
    text = text.replace(before, after);
  }
  if (text !== original) await writeFile(path, text);
  const appPath = join(root, 'server/src/app/gateway-application.mjs');
  let app = await readFile(appPath, 'utf8');
  const before = '    sessionSummariser,\n    taskManager,';
  const after = '    sessionSummariser,\n    taskOperations,\n    taskManager,';
  if (!app.includes(after)) {
    if (app.split(before).length !== 2) throw Error('frontend_memory_task_hook_changed');
    app = app.replace(before, after);
    await writeFile(appPath, app);
  }
}
