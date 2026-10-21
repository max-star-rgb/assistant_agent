import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { finalResult } from '../src/task-results.mjs';
import { AgentServerClient } from '../src/agent-server-client.mjs';

const threadId = 'native-thread';
const prefix = `/artifacts/${createHash('sha256').update(`user\0${threadId}`).digest('hex').slice(0, 32)}/generated/`;
const ref = `${prefix}image.png`;
const client = { identity: 'user', url: 'http://127.0.0.1:8088',
  checkArtifact: async () => 'image/png' };
const state = refs => ({ values: { messages: [
  { type: 'human', content: 'make an image' },
  { type: 'tool', status: 'success', artifact: { assistant_agent_delivery_v1: { output_refs: refs } } },
  { type: 'ai', content: 'done' },
] } });

const detail = '<detail>\n1. 商品 99元 <link>https://example.com/item</link>\n</detail>';
const textTool = (text = detail, extra = {}) => ({ type: 'tool', name: 'shopping_search',
  artifact: { assistant_agent_delivery_v1: { text, output_refs: [] } }, ...extra });

test('declared text is a separate artifact even without an assistant summary', async () => {
  const result = await finalResult({ values: { messages: [{ type: 'human' }, textTool()] } }, client, { threadId });
  assert.equal(result.artifacts[0].parts[0].text, detail);
  assert.equal(result.artifacts[0].parts[0].mediaType, 'text/plain');
  assert.equal(result.delivery_status, 'ready');
});

test('text delivery uses latest tool result, current turn and ignores model prose', async () => {
  const messages = [textTool('old'), { type: 'human' }, textTool('superseded'), textTool(),
    textTool(detail, { name: 'another_tool' }), { type: 'ai', content: '<detail>invented</detail>' }];
  const result = await finalResult({ values: { messages } }, client, { threadId });
  assert.deepEqual(result.artifacts.flatMap(a => a.parts.map(p => p.text)), [detail]);
  messages.splice(-1, 0, textTool('failed', { status: 'error' }),
    textTool('failed', { name: 'another_tool', status: 'error' }));
  assert.deepEqual((await finalResult({ values: { messages } }, client, { threadId })).artifacts, []);
});

test('malformed delivery text is rejected without truncating detail markup', async () => {
  for (const text of [123, 'x'.repeat(16001)]) {
    const result = await finalResult({ values: { messages: [textTool(text), { type: 'ai', content: 'done' }] } }, client, { threadId });
    assert.deepEqual(result.artifacts, []);
    assert.equal(result.delivery_status, 'failed');
  }
});

test('legacy artifact URI is normalized to the same authenticated download path', async () => {
  const legacy = ref.replace('/artifacts/', 'artifact://v1/');
  const result = await finalResult(state([legacy, ref]), client, { threadId });
  assert.equal(result.delivery_status, 'ready');
  assert.equal(result.artifacts.length, 1);
  assert.equal(result.artifacts[0].parts[0].url, client.url + ref);
});

test('result projects only declared current-thread artifacts and deduplicates them', async () => {
  const result = await finalResult(state([ref, ref, '/artifacts/other/generated/no.png', 'https://evil/image.png']), client, { threadId });
  assert.equal(result.artifacts.length, 1);
  assert.equal(result.artifacts[0].parts[0].url, client.url + ref);
  assert.equal(result.delivery_status, 'partial');
});

test('unavailable artifact reports delivery failure and does not claim delivery', async () => {
  const result = await finalResult(state([ref]), { ...client, checkArtifact: async () => { throw Error('missing'); } }, { threadId });
  assert.equal(result.artifacts.length, 0);
  assert.equal(result.delivery_status, 'failed');
  assert.match(result.content, /不要自动重新生成/);
});

test('old final text and old tool artifacts cannot masquerade as current result', async () => {
  const input = state([ref]);
  input.values.messages.push({ type: 'human', content: 'next' });
  await assert.rejects(finalResult(input, client, { threadId }), /business_result_missing/);
});

test('artifact probe restricts paths, authenticates and rejects non-image responses', async () => {
  const requests = [];
  let type = 'image/png';
  const probe = new AgentServerClient({ url: client.url, identity: client.identity, fetchImpl: async (url, options) => {
    requests.push({ url, options });
    return new Response('fixture', { headers: { 'content-type': type } });
  } });
  assert.equal(await probe.checkArtifact(ref), 'image/png');
  assert.equal(requests[0].options.headers['X-Assistant-User'], 'user');
  assert.equal(requests[0].options.redirect, 'error');
  type = 'text/html';
  await assert.rejects(probe.checkArtifact(ref), /artifact_unavailable/);
  await assert.rejects(probe.checkArtifact('https://evil/image.png'), /invalid_artifact/);
  assert.equal(requests.length, 2);
});
