import test from 'node:test';
import assert from 'node:assert/strict';
import { once, EventEmitter } from 'node:events';
import { createServer } from 'node:http';
import WebSocket, { WebSocketServer } from 'ws';
import { startMediaServer, AudioDownlink, AudioUplink, parseAudio, MediaWriter } from '../src/media-entry.mjs';
import { finalResult } from '../src/task-results.mjs';
import { artifactsFromOutcome } from '../node_modules/qwen-audio-agent/server/src/task/task-artifact.mjs';

test('backend detail survives native task artifacts and reaches only its media session once', async () => {
  const detail = '<detail>\n1. 商品 99元 <link>https://example.com/item</link><pic>https://example.com/a.jpg</pic>\n</detail>';
  const result = await finalResult({ values: { messages: [{ type: 'human' },
    { type: 'tool', name: 'shopping_search', artifact: { assistant_agent_delivery_v1: { text: detail, output_refs: [] } } },
    { type: 'ai', content: '找到商品了' }] } }, { identity: 'user' }, { threadId: 'thread' });
  const gcp = new WebSocketServer({ host: '127.0.0.1', port: 0 });
  await once(gcp, 'listening');
  let peer, sessionId;
  gcp.on('connection', (ws, req) => {
    peer = ws; sessionId = new URL(req.url, 'http://localhost').searchParams.get('sessionId');
    ws.on('message', raw => {
      if (JSON.parse(raw).type === 'session.hello') ws.send(JSON.stringify({ type: 'voice.ready', provider: 'dashscope', inputSampleRate: 16000 }));
    });
  });
  const entry = await startMediaServer({ port: 0, host: '127.0.0.1', gatewayPort: gcp.address().port });
  const ws = new WebSocket(`ws://127.0.0.1:${entry.server.address().port}/agent-service/v1?number=u`);
  const packets = [];
  ws.on('message', raw => { const p = JSON.parse(raw); packets.push({ ...p, body: JSON.parse(p.body) }); });
  const wait = async predicate => {
    for (let i = 0; i < 200 && !predicate(); i++) await new Promise(r => setTimeout(r, 5));
    assert.ok(predicate());
  };
  try {
    await wait(() => packets.some(p => p.message === 'audioReady'));
    const task = { id: 'shopping', sessionId, status: 'completed', artifacts: artifactsFromOutcome(result) };
    for (const event of [
      { type: 'task.completed', task: { ...task, sessionId: 'other' } },
      { type: 'task.updated', task: { ...task, status: 'working' } },
      { type: 'task.completed', task }, { type: 'task.updated', task },
      { type: 'audio.delta', responseId: 'after', sampleRate: 24000, audio: 'AAA=' },
    ]) peer.send(JSON.stringify(event));
    await wait(() => packets.some(p => p.message === 'audioChunk'));
    const replies = packets.filter(p => p.message === 'chatResponse');
    assert.equal(replies.length, 1);
    assert.equal(replies[0].body.message.content.intentResult.description, detail);
    assert.equal(replies[0].body.number, 'u');
    assert.equal(replies[0].body.displayOnly, true);
    assert.equal(replies[0].body.display_only, true);
  } finally {
    await entry.close();
    for (const c of gcp.clients) c.terminate();
    await new Promise(r => gcp.close(r));
  }
});

test('media writer prioritizes realtime between images and rejects pending sends on close', async () => {
  const ws = new EventEmitter(); ws.readyState = WebSocket.OPEN;
  const sent = [], callbacks = [];
  ws.send = (raw, done) => { sent.push(JSON.parse(raw).id); callbacks.push(done); };
  ws.terminate = () => { ws.readyState = WebSocket.CLOSED; ws.emit('close'); };
  const writer = new MediaWriter(ws);
  const first = writer.send({ id: 'image1' }, { image: true });
  await Promise.resolve();
  const second = writer.send({ id: 'image2' }, { image: true });
  const audio = writer.send({ id: 'audio' });
  const ack = writer.send({ id: 'ack' });
  callbacks.shift()(); await first;
  assert.deepEqual(sent, ['image1', 'audio']);
  callbacks.shift()(); await audio;
  assert.deepEqual(sent, ['image1', 'audio', 'ack']);
  callbacks.shift()(); await ack;
  assert.deepEqual(sent, ['image1', 'audio', 'ack', 'image2']);
  const pending = writer.send({ id: 'pending' });
  const results = Promise.allSettled([second, pending]);
  ws.terminate();
  assert.deepEqual((await results).map(r => r.status), ['rejected', 'rejected']);
  callbacks.shift()(); // A late socket callback must not revive the queue.
  assert.equal(sent.length, 4);
});

test('completed mock image reaches only its media session as IMAGE base64 once', async () => {
  const png = Buffer.concat([Buffer.from('89504e470d0a1a0a', 'hex'), Buffer.alloc(8 * 1024 * 1024)]);
  let reads = 0, uplinkPackets = 0, gatewaySocket, gatewaySession;
  const backend = createServer((req, res) => {
    assert.equal(req.headers['x-assistant-user'], 'local-developer');
    reads++;
    res.writeHead(200, { 'content-type': 'image/png' }); res.end(png);
  });
  backend.listen(0, '127.0.0.1'); await once(backend, 'listening');
  const gcp = new WebSocketServer({ host: '127.0.0.1', port: 0 });
  await once(gcp, 'listening');
  gcp.on('connection', (ws, req) => {
    gatewaySocket = ws;
    gatewaySession = new URL(req.url, 'http://localhost').searchParams.get('sessionId');
    ws.on('message', raw => {
      if (JSON.parse(raw).type === 'input_audio_buffer.append') uplinkPackets++;
      if (JSON.parse(raw).type === 'session.hello') ws.send(JSON.stringify({ type: 'voice.ready', provider: 'dashscope', inputSampleRate: 16000 }));
    });
  });
  const backendUrl = `http://127.0.0.1:${backend.address().port}`;
  const entry = await startMediaServer({ port: 0, host: '127.0.0.1', gatewayPort: gcp.address().port, backendUrl, identity: 'local-developer' });
  const media = new WebSocket(`ws://127.0.0.1:${entry.server.address().port}/agent-service/v1?number=00860332`);
  const packets = [];
  media.on('message', raw => { const p = JSON.parse(raw); packets.push({ ...p, body: JSON.parse(p.body) }); });
  const wait = async predicate => {
    for (let n = 0; n < 600 && !predicate(); n++) await new Promise(r => setTimeout(r, 5));
    assert.ok(predicate());
  };
  try {
    await wait(() => packets.some(p => p.message === 'audioReady'));
    const ref = `/artifacts/${'a'.repeat(32)}/generated/mock.png`;
    const task = { id: 'task-image', status: 'completed', sessionId: gatewaySession,
      artifacts: [{ artifactId: ref, parts: [{ url: backendUrl + ref, mediaType: 'image/png' }] }] };
    gatewaySocket.send(JSON.stringify({ type: 'task.completed', task: { ...task, sessionId: 'other-session' } }));
    gatewaySocket.send(JSON.stringify({ type: 'task.completed', task }));
    gatewaySocket.send(JSON.stringify({ type: 'task.updated', task }));
    // A slow receiver must not turn the image's buffered bytes into an audio ACK failure.
    media._socket.pause();
    await new Promise(resolve => setTimeout(resolve, 100));
    let sentAudio = 0;
    const audio = setInterval(() => { sentAudio++; media.send(JSON.stringify({ message: 'audio', body: JSON.stringify({
      audioConfig: { codec: 'pcm_s16le', sampleRate: 16000, channels: 1 }, contents: [{ audioContent: '0000'.repeat(1600) }],
    }) })); }, 5);
    await new Promise(resolve => setTimeout(resolve, 200));
    clearInterval(audio);
    await new Promise(resolve => setTimeout(resolve, 100));
    // Downstream image backpressure must not stop the opposite-direction audio.
    assert.equal(uplinkPackets, sentAudio, 'all uploaded audio must reach GCP before image download resumes');
    media._socket.resume();
    await wait(() => packets.some(p => p.body.message?.content?.intentResult?.detail?.[0]?.type === 'IMAGE'));
    const imagePackets = packets.filter(p => p.body.message?.content?.intentResult?.detail);
    assert.equal(imagePackets.length, 1);
    assert.equal(reads, 1);
    assert.equal(imagePackets[0].body.number, '00860332');
    assert.equal(imagePackets[0].body.displayOnly, false);
    await wait(() => packets.some(p => p.message === 'audioResponse'));
    assert.equal(media.readyState, WebSocket.OPEN);
    assert.equal(imagePackets[0].body.message.content.intentResult.detail[0].image, png.toString('base64'));
    gatewaySocket.send(JSON.stringify({ type: 'task.completed', task: { ...task, id: 'bad-image', artifacts: [
      { artifactId: 'external', parts: [{ url: 'https://invalid.example/image.png', mediaType: 'image/png' }] },
    ] } }));
    await wait(() => packets.some(p => p.body.message?.content?.intentResult?.status === 'FAIL'));
    assert.equal(reads, 1);
    assert.equal(media.readyState, WebSocket.OPEN);
  } finally {
    await entry.close();
    for (const ws of gcp.clients) ws.terminate();
    await new Promise(resolve => gcp.close(resolve));
    backend.closeAllConnections(); await new Promise(resolve => backend.close(resolve));
  }
});

test('audio validates actual format and downlink rejects stale chunks after interruption', () => {
  const body = { audioConfig: { codec: 'pcm_s16le', sampleRate: 16000, channels: 1 }, contents: [{ audioContent: '0000' }] };
  assert.equal(parseAudio(body), 'AAA=');
  assert.throws(() => parseAudio({ ...body, audioConfig: { ...body.audioConfig, codec: 'opus' } }));
  const out = new AudioDownlink();
  const event = { type: 'audio.delta', responseId: 'r1', sampleRate: 24000, audio: 'AAA=' };
  const packets = out.project(event);
  assert.deepEqual(packets.map(p => p.message), ['audioStart', 'audioChunk']);
  assert.equal(packets[1].body.sequence, 0);
  assert.throws(() => out.receipt({ ...packets[0].body, status: 'completed' }));
  assert.equal(out.project({ type: 'audio.done', responseId: 'r1' })[0].body.lastSequence, 0);
  assert.equal(out.project({ type: 'playback.clear' })[0].message, 'audioInterrupt');
  assert.deepEqual(out.project(event), []);
  assert.equal(out.receipt({ ...packets[0].body, status: 'interrupted' }).type, 'playback.cancelled');
  assert.deepEqual(out.project({ type: 'audio.done' }), []);
});

for (const explicitControl of [false, true]) {
test(`VIDEO with explicitControl=${explicitControl} preserves the visual lane and same GCP session`, async () => {
  const gcp = new WebSocketServer({ host: '127.0.0.1', port: 0 });
  const visual = new WebSocketServer({ host: '127.0.0.1', port: 0 });
  await Promise.all([once(gcp, 'listening'), once(visual, 'listening')]);
  const events = [], videoFrames = [];
  gcp.on('connection', ws => ws.on('message', raw => {
    const e = JSON.parse(raw); events.push(e);
    if (e.type === 'session.hello') ws.send(JSON.stringify({ type: 'voice.ready', provider: 'dashscope', inputSampleRate: 16000 }));
  }));
  visual.on('connection', (ws, req) => {
    assert.equal(req.url, '/internal/media-visual/v1');
    assert.equal(req.headers['x-assistant-user'], 'local-developer');
    ws.on('message', raw => {
      const e = JSON.parse(raw); videoFrames.push(e);
      const send = (message, body) => ws.send(JSON.stringify({ message, body: JSON.stringify(body) }));
      if (e.message === 'assistantControl') send('audioReady', {});
      if (e.message === 'video') {
        send('gatewayVisualEvent', { type: 'input_image_buffer.append', image: 'fixture', media_type: 'image/jpeg' });
        send('videoResponse', { code: 0, message: 'video received' });
      }
    });
  });
  const entry = await startMediaServer({ port: 0, host: '127.0.0.1', gatewayPort: gcp.address().port,
    backendUrl: `http://127.0.0.1:${visual.address().port}`, identity: 'local-developer' });
  const ws = new WebSocket(`ws://127.0.0.1:${entry.server.address().port}/agent-service/v1${explicitControl ? '' : '?number=u'}`);
  const send = (message, body) => ws.send(JSON.stringify({ message, body: JSON.stringify(body) }));
  try {
    await once(ws, 'open');
    const ready = new Promise(resolve => ws.on('message', raw => { if (JSON.parse(raw).message === 'audioReady') resolve(); }));
    if (explicitControl) send('assistantControl', { number: 'u', callType: 'VIDEO' });
    await ready;
    const received = new Promise(resolve => ws.on('message', raw => { if (JSON.parse(raw).message === 'videoResponse') resolve(); }));
    send('video', { userNumber: 'u', videoIndex: 'v1', contents: [] });
    await received;
    assert.ok(videoFrames.some(f => f.message === 'video'));
    const controlBody = JSON.parse(videoFrames.find(f => f.message === 'assistantControl').body);
    assert.equal(controlBody.number, 'u');
    assert.equal(controlBody.callType, 'VIDEO');
    assert.equal(controlBody.clientCapabilities.audioStreamV1, true);
    assert.ok(events[0].capabilities.includes('input.image_buffer'));
    for (let n = 0; n < 100 && !events.some(e => e.type === 'input_image_buffer.append'); n++) await new Promise(r => setTimeout(r, 5));
    assert.ok(events.some(e => e.type === 'input_image_buffer.append'));
  } finally {
    await entry.close();
    for (const server of [gcp, visual]) { for (const c of server.clients) c.terminate(); await new Promise(r => server.close(r)); }
  }
});

}

test('media listener negotiates, forwards audio directly to GCP, and owns shutdown', async () => {
  const gcp = new WebSocketServer({ host: '127.0.0.1', port: 0 });
  await once(gcp, 'listening');
  const received = [];
  gcp.on('connection', ws => ws.on('message', raw => {
    const event = JSON.parse(raw); received.push(event);
    if (event.type === 'session.hello') ws.send(JSON.stringify({ type: 'voice.ready', provider: 'dashscope', inputSampleRate: 16000 }));
    if (event.type === 'input_audio_buffer.append') {
      for (const e of [{ type: 'audio.delta', responseId: 'r2', sampleRate: 24000, audio: 'AAA=' }, { type: 'audio.done', responseId: 'r2' }]) ws.send(JSON.stringify(e));
    }
  }));
  const entry = await startMediaServer({ port: 0, host: '127.0.0.1', gatewayPort: gcp.address().port,
    backendUrl: 'http://127.0.0.1:1', identity: 'local-developer' });
  const ws = new WebSocket(`ws://127.0.0.1:${entry.server.address().port}/agent-service/v1`);
  const packets = [];
  ws.on('message', raw => { const p = JSON.parse(raw); packets.push({ ...p, body: JSON.parse(p.body) }); });
  const wait = async predicate => {
    for (let n = 0; n < 200 && !predicate(); n++) await new Promise(r => setTimeout(r, 5));
    assert.ok(predicate());
  };
  const send = (message, body) => ws.send(JSON.stringify({ message, sessionId: 's', body: JSON.stringify(body) }));
  try {
    await once(ws, 'open');
    send('assistantControl', { number: 'u', callType: 'AUDIO', clientCapabilities: { audioStreamV1: true } });
    await wait(() => packets.some(p => p.message === 'audioReady'));
    assert.deepEqual(packets.slice(0, 3).map(p => p.message), ['audioConnecting', 'assistantControl', 'audioReady']);
    send('audio', { userNumber: 'u', audioConfig: { codec: 'pcm_s16le', sampleRate: 16000, channels: 1 }, contents: [{ audioContent: '0000' }] });
    await wait(() => packets.some(p => p.message === 'audioEnd'));
    assert.ok(received.some(e => e.type === 'input_audio_buffer.append' && e.audio === 'AAA='));
    assert.ok(packets.some(p => p.message === 'audioResponse' && p.body.message === 'audio forwarded'));
    send('audioStatus', { taskId: 'voice:r2', streamId: 'audio:r2', status: 'completed' });
    await wait(() => received.some(e => e.type === 'playback.ended'));
    assert.equal((await fetch(`http://127.0.0.1:${entry.server.address().port}/threads`)).status, 404);
  } finally {
    await entry.close();
    for (const client of gcp.clients) client.terminate();
    await new Promise(resolve => gcp.close(resolve));
  }
});

test('numbered connection queues immediate audio without control and accepts optional control later', async () => {
  const gcp = new WebSocketServer({ host: '127.0.0.1', port: 0 });
  await once(gcp, 'listening');
  const received = [], packets = [], logs = [];
  gcp.on('connection', ws => ws.on('message', raw => {
    const event = JSON.parse(raw); received.push(event);
    if (event.type === 'session.hello') setTimeout(() => {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'voice.ready', provider: 'dashscope', inputSampleRate: 16000 }));
    }, 30);
  }));
  const entry = await startMediaServer({ port: 0, host: '127.0.0.1', gatewayPort: gcp.address().port,
    backendUrl: 'http://127.0.0.1:1', identity: 'local-developer',
    logger: { info: (event, data) => logs.push({ event, ...data }), warn: (event, data) => logs.push({ event, ...data }) } });
  const ws = new WebSocket(`ws://127.0.0.1:${entry.server.address().port}/agent-service/v1?number=00860332`);
  ws.on('message', raw => { const p = JSON.parse(raw); packets.push({ ...p, body: JSON.parse(p.body) }); });
  const send = (message, body) => ws.send(JSON.stringify({ message, sessionId: 's', body: JSON.stringify(body) }));
  const wait = async predicate => {
    for (let n = 0; n < 200 && !predicate(); n++) await new Promise(r => setTimeout(r, 5));
    assert.ok(predicate());
  };
  try {
    await once(ws, 'open');
    const audio = { audioConfig: { codec: 'pcm_s16le', sampleRate: 16000, channels: 1 }, contents: [{ audioContent: '0000' }] };
    for (let i = 0; i < 160; i++) send('audio', audio);
    await wait(() => packets.filter(p => p.message === 'audioResponse').length === 160);
    await wait(() => received.some(e => e.type === 'input_audio_buffer.append'));
    assert.ok(packets.some(p => p.message === 'audioReady'));
    send('assistantControl', { number: '00860332' });
    await wait(() => packets.some(p => p.message === 'assistantControl'));
    assert.equal(packets.find(p => p.message === 'assistantControl').body.code, 0);
    assert.equal(received.filter(e => e.type === 'session.hello').length, 1);
    send('audio', { ...audio, userNumber: 'different' });
    await wait(() => packets.some(p => p.body.message === 'userNumber mismatch'));
    send('audio', { ...audio, audioConfig: { ...audio.audioConfig, codec: 'opus' } });
    await wait(() => packets.some(p => p.body.message?.includes('audio requires')));
    assert.equal(received.filter(e => e.type === 'input_audio_buffer.append').length, 1);
    assert.equal(Buffer.from(received.find(e => e.type === 'input_audio_buffer.append').audio, 'base64').length, 320);
    assert.ok(logs.some(e => e.event === 'media.message_rejected'));
  } finally {
    await entry.close();
    for (const client of gcp.clients) client.terminate();
    await new Promise(resolve => gcp.close(resolve));
  }
});


test('uplink merges 20 ms packets, splits large packets, and preserves the short tail exactly', () => {
  const uplink = new AudioUplink();
  const input = Buffer.from(Array.from({ length: 7040 }, (_, i) => i % 251));
  const output = [];
  for (let i = 0; i < 5; i++) {
    const chunks = uplink.push(input.subarray(i * 640, (i + 1) * 640).toString('base64'));
    assert.equal(chunks.length, i === 4 ? 1 : 0);
    output.push(...chunks);
  }
  output.push(...uplink.push(input.subarray(3200).toString('base64')));
  assert.deepEqual(output.map(c => Buffer.from(c, 'base64').length), [3200, 3200]);
  output.push(uplink.flush());
  assert.deepEqual(Buffer.concat(output.map(c => Buffer.from(c, 'base64'))), input);
  assert.equal(uplink.flush(), '');
});

for (const trigger of [
  { type: 'error', error: { code: 'bad_event' }, message: 'private payload' },
  { type: 'voice.connection', state: 'reconnecting' },
  { type: 'voice.connection', state: 'disconnected' },
  { type: 'voice.connection', state: 'unavailable' },
]) test(`established media survives ${trigger.type}/${trigger.state || 'error'} and resumes audio`, async () => {
  const gcp = new WebSocketServer({ host: '127.0.0.1', port: 0 });
  await once(gcp, 'listening');
  let peer;
  gcp.on('connection', ws => {
    peer = ws;
    ws.on('message', raw => {
      if (JSON.parse(raw).type === 'session.hello') ws.send(JSON.stringify({ type: 'voice.ready', provider: 'dashscope', inputSampleRate: 16000 }));
    });
  });
  const logs = [];
  const entry = await startMediaServer({ port: 0, host: '127.0.0.1', gatewayPort: gcp.address().port,
    logger: { info() {}, warn: (event, fields) => logs.push({ event, ...fields }) } });
  const ws = new WebSocket(`ws://127.0.0.1:${entry.server.address().port}/agent-service/v1?number=u`);
  const packets = [];
  ws.on('message', raw => packets.push(JSON.parse(raw)));
  const wait = async predicate => {
    for (let i = 0; i < 200 && !predicate(); i++) await new Promise(r => setTimeout(r, 5));
    assert.ok(predicate());
  };
  try {
    await wait(() => packets.some(p => p.message === 'audioReady'));
    for (const event of [trigger, { type: 'voice.connection', state: 'connected' },
      { type: 'audio.delta', responseId: 'recovered', sampleRate: 24000, audio: 'AAA=' }]) peer.send(JSON.stringify(event));
    await wait(() => packets.some(p => p.message === 'audioChunk'));
    assert.equal(ws.readyState, WebSocket.OPEN);
    assert.ok(logs.some(l => l.event === 'media.gateway_event'));
    assert.ok(!JSON.stringify(logs).includes('private payload'));
    peer.send(JSON.stringify({ type: 'voice.deactivated' }));
    await wait(() => ws.readyState === WebSocket.CLOSED);
    assert.ok(packets.some(p => p.message === 'audioError'));
  } finally {
    await entry.close();
    for (const c of gcp.clients) c.terminate();
    await new Promise(r => gcp.close(r));
  }
});

test('unavailable recovery has a deadline and reports its cause before closing', { timeout: 20000 }, async () => {
  const gcp = new WebSocketServer({ host: '127.0.0.1', port: 0 });
  await once(gcp, 'listening');
  let peer;
  gcp.on('connection', ws => {
    peer = ws;
    ws.on('message', raw => {
      if (JSON.parse(raw).type === 'session.hello') ws.send(JSON.stringify({ type: 'voice.ready', provider: 'dashscope', inputSampleRate: 16000 }));
    });
  });
  const entry = await startMediaServer({ port: 0, host: '127.0.0.1', gatewayPort: gcp.address().port });
  const ws = new WebSocket(`ws://127.0.0.1:${entry.server.address().port}/agent-service/v1?number=u`);
  const packets = [];
  const ready = new Promise(resolve => ws.on('message', raw => {
    const p = JSON.parse(raw); packets.push({ ...p, body: JSON.parse(p.body) });
    if (p.message === 'audioReady') resolve();
  }));
  try {
    await ready;
    const closed = once(ws, 'close');
    peer.send(JSON.stringify({ type: 'voice.connection', state: 'unavailable' }));
    await closed;
    assert.equal(packets.at(-1).body.code, 'GATEWAY_RECOVERY_TIMEOUT');
    assert.ok(packets.some(p => p.message === 'audioConnecting' && p.body.message.includes('reconnecting')));
  } finally {
    await entry.close();
    for (const c of gcp.clients) c.terminate();
    await new Promise(r => gcp.close(r));
  }
});
