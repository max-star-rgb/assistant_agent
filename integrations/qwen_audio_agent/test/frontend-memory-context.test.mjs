import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { patchFrontendMemoryContext } from '../src/frontend-memory-context.mjs';

test('upstream prompt contains bounded memory and patch is repeatable', async () => {
  await patchFrontendMemoryContext();
  await patchFrontendMemoryContext();
  const base = import.meta.resolve('qwen-audio-agent/gateway-application');
  const { buildFrontendInstructions } = await import(new URL('../frontend/frontend-tools.mjs', base));
  const prompt = buildFrontendInstructions({ memories: [{ scope: 'memory', format: 'text', content: '用户背景样本' }] });
  assert.match(prompt, /用户背景样本/);
  assert.match(prompt, /Assistant Profile/);
  const runtime = await readFile(new URL('../voice/realtime-session-runtime.mjs', base), 'utf8');
  assert.match(runtime, /source\.memorySnapshot\?\.\(ownerId\)/);
  assert.match(runtime, /source\.beginMemorySession/);
  assert.match(runtime, /unsubscribeExternalMemory\.forEach/);
});
