import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import WebSocket from 'ws';
import { patchFrontendConcurrency } from '../src/frontend-concurrency-patch.mjs';
import { startMediaServer } from '../src/media-entry.mjs';

test('task notifications stay with their source session and do not wake another session', async () => {
  await patchFrontendConcurrency();
  const { SessionTaskCoordinator } = await import('../node_modules/qwen-audio-agent/server/src/orchestration/session-task-coordinator.mjs');
  const { TaskDomainEvent } = await import('../node_modules/qwen-audio-agent/server/src/task/task-events.mjs');
  const claims = [];
  let wakes = 0, sleeping = false;
  const coordinator = new SessionTaskCoordinator({ ownerId: 'user', sessionId: 'web',
    taskManager: { claimNotifications(args) { claims.push(args); return []; } },
    presentation: { createAnnouncements: () => ({}), state: () => ({ ready: true, outputEnabled: true, sleeping }) },
    onWake: () => wakes++,
  });
  coordinator.claimPendingNotifications();
  assert.equal(claims[0].includeOtherSessions, false);
  assert.equal(claims[0].sessionId, 'web');
  sleeping = true;
  coordinator.handleEvent({ type: TaskDomainEvent.NOTIFICATION_PENDING, ownerId: 'user', task: { id: 'other', sessionId: 'media' } });
  assert.equal(wakes, 0);
  assert.equal(claims.length, 1);
});

test('web and media have independent voice leases, output and closure under one owner', { timeout: 10000 }, async t => {
  await patchFrontendConcurrency();
  await patchFrontendConcurrency();
  const { attachGatewayClientTransport } = await import('../node_modules/qwen-audio-agent/server/src/transport/gateway-client-transport.mjs');
  const server = createServer();
  const sessions = [];
  const logger = { info() {}, warn() {}, debug() {}, child() { return this; } };
  const transport = attachGatewayClientTransport(server, {
    logger, identityManager: { resolveUpgrade: () => ({ ownerId: 'user', access: 'local' }) },
    frontendRuntime: {
      supportsImageInput: () => true,
      createSession(options) {
        const state = { ...options, closed: false, inputs: [] };
        sessions.push(state);
        return {
          start() {}, status: () => ({}), applyInputSuspension() {}, deactivate() {},
          handleClientEvent(event) {
            state.inputs.push(event);
            if (event.type === 'connect') {
              assert.equal(options.voiceAccess.claim().granted, true);
              options.voiceAccess.changed();
              options.send({ type: 'voice.ready', provider: 'dashscope', inputSampleRate: 16000 });
            }
          },
          close() { state.closed = true; options.voiceAccess.release(); options.voiceAccess.changed(); },
        };
      },
    },
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const port = server.address().port;
  const media = await startMediaServer({ port: 0, host: '127.0.0.1', gatewayPort: port,
    backendUrl: 'http://127.0.0.1:1', identity: 'user', logger });
  const sockets = [];
  t.after(async () => {
    for (const socket of sockets) socket.terminate();
    await media.close(); await transport.close();
    await new Promise(resolve => server.close(resolve));
  });
  async function connect(url) {
    const socket = new WebSocket(url, { origin: `http://127.0.0.1:${port}` });
    sockets.push(socket);
    socket.events = [];
    socket.on('message', raw => socket.events.push(JSON.parse(raw)));
    await once(socket, 'open');
    return socket;
  }
  async function until(predicate) {
    for (let n = 0; n < 200; n++) {
      if (predicate()) return;
      await new Promise(resolve => setTimeout(resolve, 10));
    }
    assert.fail(`expected event did not arrive: ${JSON.stringify(sockets.map(s => s.events))}`);
  }
  const web = await connect(`ws://127.0.0.1:${port}/api/realtime?sessionId=web`);
  const hello = { type: 'session.hello', event_id: 'hello', protocol: { min: '7.0.0', max: '7.0.0' },
    client: { type: 'web', instance_id: 'web-1' }, capabilities: ['input.audio'],
    connection: { provider: 'dashscope', takeover: false } };
  web.send(JSON.stringify(hello));
  await until(() => web.events.some(e => e.type === 'voice.ready'));
  const mobile = await connect(`ws://127.0.0.1:${media.server.address().port}/agent-service/v1?number=user`);
  await until(() => mobile.events.some(e => e.message === 'audioReady'));
  assert.equal(sessions.length, 2);
  assert.equal(sessions[0].ownerId, sessions[1].ownerId);
  assert.notEqual(sessions[0].sessionId, sessions[1].sessionId);
  assert.ok(sessions.every(s => s.voiceAccess.isActive()));
  assert.ok(!web.events.some(e => e.type === 'voice.ownership' && e.state === 'busy'));
  const duplicate = await connect(`ws://127.0.0.1:${port}/api/realtime?sessionId=web`);
  duplicate.send(JSON.stringify({ ...hello, client: { type: 'web', instance_id: 'other' } }));
  await until(() => duplicate.readyState === WebSocket.CLOSED);
  assert.equal(web.readyState, WebSocket.OPEN);
  sessions[1].send({ type: 'audio.delta', responseId: 'media-only', sampleRate: 24000, audio: 'AAA=' });
  await until(() => mobile.events.some(e => e.message === 'audioChunk'));
  assert.ok(!web.events.some(e => e.responseId === 'media-only'));
  mobile.close();
  await until(() => sessions[1].closed);
  assert.equal(sessions[0].closed, false);
  assert.equal(sessions[0].voiceAccess.isActive(), true);
});
