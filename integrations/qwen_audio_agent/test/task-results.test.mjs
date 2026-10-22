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

test('detail is delivered separately from the unchanged answer and existing images', async () => {
  const detail = '<detail>\n商品 <link>https://example.com/item</link>\n</detail>';
  const input = state([ref]);
  input.values.messages.splice(2, 0,
    { type: 'tool', name: 'shopping_search', artifact: { assistant_agent_delivery_v1: { text: detail, output_refs: [] } } },
    { type: 'tool', name: 'image_generation', artifact: { assistant_agent_delivery_v1: { output_refs: [`${prefix}second.png`] } } });
  input.values.messages[1].name = 'image_generation';
  const result = await finalResult(input, client, { threadId });
  assert.equal(result.content, 'done');
  assert.deepEqual(result.artifacts.slice(0, 2).map(a => a.parts[0].url), [client.url + ref, `${client.url}${prefix}second.png`]);
  assert.deepEqual(result.artifacts[2].parts, [{ text: detail, mediaType: 'text/plain' }]);
});

test('detail ignores old turns, superseded results, failed tools and other text', async () => {
  const tool = (text, status = 'success', name = 'shopping_search') => ({ type: 'tool', name, status,
    artifact: { assistant_agent_delivery_v1: { text, output_refs: [] } } });
  const messages = [tool('<detail>old</detail>'), { type: 'human' }, tool('<detail>superseded</detail>'),
    tool('<detail>failed</detail>', 'error'), tool('ordinary tool text', 'success', 'other'),
    { type: 'ai', content: '<detail>model prose</detail>' }];
  assert.deepEqual((await finalResult({ values: { messages } }, client, { threadId })).artifacts, []);
  messages.splice(-1, 0, tool('<detail>current</detail>'));
  assert.deepEqual((await finalResult({ values: { messages } }, client, { threadId })).artifacts[0].parts,
    [{ text: '<detail>current</detail>', mediaType: 'text/plain' }]);
});

test('detail alone works through MCP wrapping and oversized cards are not truncated', async () => {
  const delivery = { text: '<detail>card</detail>', output_refs: [] };
  const input = { values: { messages: [{ type: 'human' }, { type: 'tool', name: 'shopping_search',
    artifact: { structured_content: { assistant_agent_delivery_v1: delivery } } }] } };
  const result = await finalResult(input, client, { threadId });
  assert.equal(result.artifacts[0].parts[0].text, delivery.text);
  delivery.text = `<detail>${'x'.repeat(16000)}</detail>`;
  const oversized = await finalResult(input, client, { threadId });
  assert.deepEqual(oversized.artifacts, []);
  assert.equal(oversized.delivery_status, 'failed');
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
