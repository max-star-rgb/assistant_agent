import { createServer } from 'node:http';
import { once } from 'node:events';
import { randomUUID } from 'node:crypto';
import WebSocket, { WebSocketServer } from 'ws';
import { readMediaImage } from './media-artifacts.mjs';

const packet = (message, body) => ({ message, body });
const ids = id => ({ taskId: `voice:${id}`, streamId: `audio:${id}` });
const format = (sampleRate, encoding) => ({ codec: 'pcm_s16le', sampleRate, channels: 1, encoding });
const requireValue = (valid, message) => { if (!valid) throw new Error(message); };

export function parseAudio(body) {
  const c = body.audioConfig;
  requireValue(c?.codec === 'pcm_s16le' && c.sampleRate === 16000 && c.channels === 1,
    'audio requires pcm_s16le / 16000 Hz / mono');
  requireValue(Array.isArray(body.contents) && body.contents.length > 0 && body.contents.length <= 10, 'invalid audio contents');
  const parts = body.contents.map(item => {
    const hex = item?.audioContent;
    requireValue(typeof hex === 'string' && hex.length > 0 && hex.length <= 64000 && hex.length % 4 === 0 && /^[0-9a-f]+$/i.test(hex), 'invalid PCM16 hexadecimal audio');
    return Buffer.from(hex, 'hex');
  });
  const data = Buffer.concat(parts);
  requireValue(data.length <= 32000, 'audio frame exceeds one second');
  return data.toString('base64');
}

// 100 ms of 16 kHz, mono PCM16. Preserve samples across media message boundaries.
export class AudioUplink {
  pending = Buffer.alloc(0);
  push(audio) {
    const data = Buffer.concat([this.pending, Buffer.from(audio, 'base64')]);
    const chunks = [];
    let offset = 0;
    for (; offset + 3200 <= data.length; offset += 3200) chunks.push(data.subarray(offset, offset + 3200).toString('base64'));
    this.pending = Buffer.from(data.subarray(offset));
    return chunks;
  }
  flush() {
    const audio = this.pending.toString('base64');
    this.pending = Buffer.alloc(0);
    return audio;
  }
}

export class AudioDownlink {
  streams = new Map();
  closed = new Set();
  remember(id) {
    this.closed.add(id);
    if (this.closed.size > 128) this.closed.delete(this.closed.values().next().value);
  }
  interrupt(reason, id) {
    const out = [];
    for (const key of this.streams.keys()) if ((!id || id === key) && !this.closed.has(key)) {
      this.remember(key);
      out.push(packet('audioInterrupt', { ...ids(key), reason }));
    }
    return out;
  }
  project(event) {
    const id = event.responseId;
    if (event.type === 'playback.clear' || event.type === 'response.interrupted') return this.interrupt('NEW_SPEECH', event.type === 'response.interrupted' ? id : null);
    if (!['audio.delta', 'audio.done'].includes(event.type)) return [];
    if (event.type === 'audio.done' && !id) return [];
    requireValue(typeof id === 'string' && id.length <= 128, 'gateway audio missing responseId');
    if (this.closed.has(id)) return [];
    let state = this.streams.get(id);
    if (event.type === 'audio.done') {
      if (!state || state.ended) return [];
      state.ended = true;
      return [packet('audioEnd', { ...ids(id), lastSequence: state.sequence - 1 })];
    }
    if (state?.ended) return [];
    requireValue(event.sampleRate === 24000, 'gateway output must be 24000 Hz');
    requireValue(typeof event.audio === 'string' && event.audio.length <= 262144, 'invalid gateway audio size');
    const data = Buffer.from(event.audio, 'base64');
    requireValue(data.length > 0 && data.length % 2 === 0 && data.toString('base64') === event.audio, 'invalid gateway PCM16');
    const out = [];
    if (!state) {
      requireValue(this.streams.size < 16, 'too many unacknowledged audio streams');
      state = { sequence: 0, ended: false }; this.streams.set(id, state);
      out.push(packet('audioStart', { ...ids(id), ...format(24000, 'base64') }));
    }
    for (let offset = 0; offset < data.length; offset += 4800) out.push(packet('audioChunk', {
      ...ids(id), sequence: state.sequence++, audioContent: data.subarray(offset, offset + 4800).toString('base64'),
    }));
    return out;
  }
  receipt(body) {
    requireValue(typeof body.streamId === 'string' && body.streamId.startsWith('audio:'), 'invalid streamId');
    const id = body.streamId.slice(6), status = body.status;
    requireValue(body.taskId === ids(id).taskId, 'audioStatus taskId mismatch');
    requireValue(['accepted', 'playing', 'completed', 'interrupted', 'failed'].includes(status), 'invalid audioStatus');
    const state = this.streams.get(id);
    if (!state && this.closed.has(id)) return null;
    requireValue(state, 'unknown streamId');
    if (status === 'accepted' || (status === 'playing' && this.closed.has(id))) return null;
    requireValue(status !== 'completed' || (state.ended && !this.closed.has(id)), 'completed requires uninterrupted audioEnd');
    if (status !== 'playing') { this.remember(id); this.streams.delete(id); }
    return { type: { playing: 'playback.started', completed: 'playback.ended', interrupted: 'playback.cancelled', failed: 'playback.cancelled' }[status],
      responseId: id, ...(['failed', 'interrupted'].includes(status) ? { reason: status === 'failed' ? 'media_playback_failed' : 'user_interruption' } : {}) };
  }
}

const pendingWrites = new WeakMap();
/** Prioritize realtime packets before images that have not started writing. */
export class MediaWriter {
  constructor(ws) {
    this.ws = ws;
    this.realtime = []; this.images = [];
    this.active = false; this.closed = false;
    ws.once('close', () => this.close());
  }
  send(value, { image = false } = {}) {
    if (this.closed) return Promise.reject(new Error('socket unavailable'));
    const result = new Promise((resolve, reject) => {
      (image ? this.images : this.realtime).push({ value, resolve, reject, image });
    });
    queueMicrotask(() => this.drain());
    return result;
  }
  close() {
    this.closed = true;
    this.cancelActive?.();
    for (const item of [...this.realtime.splice(0), ...this.images.splice(0)]) item.reject(new Error('socket closed'));
  }
  async drain() {
    if (this.active || this.closed) return;
    this.active = true;
    try {
      while (!this.closed) {
        const item = this.realtime.shift() || this.images.shift();
        if (!item) break;
        try {
          await new Promise((resolve, reject) => {
            if (this.ws.readyState !== WebSocket.OPEN) return reject(new Error('socket unavailable'));
            let timer, settled = false;
            const finish = error => {
              if (settled) return;
              settled = true;
              clearTimeout(timer); this.cancelActive = null;
              error ? reject(error) : resolve();
            };
            this.cancelActive = () => finish(new Error('socket closed'));
            // A partially sent message cannot be skipped; a stalled transport must close.
            timer = setTimeout(() => { finish(new Error('socket write timeout')); this.ws.terminate(); }, item.image ? 30000 : 5000);
            try { this.ws.send(JSON.stringify(item.value), finish); }
            catch (error) { finish(error); }
          });
          item.resolve();
        } catch (error) {
          item.reject(error); this.close(); this.ws.terminate();
        }
      }
    } finally { this.active = false; }
  }
}

function write(ws, value) {
  if (!ws) return Promise.reject(new Error('socket unavailable'));
  // Internal GCP/visual writes remain ordered independently of media downlink.
  const next = (pendingWrites.get(ws) || Promise.resolve()).then(() => new Promise((resolve, reject) => {
    if (ws?.readyState !== WebSocket.OPEN || ws.bufferedAmount > 2 * 1024 * 1024) return reject(new Error('socket unavailable or congested'));
    const timer = setTimeout(() => { ws.terminate(); reject(new Error('socket write timeout')); }, 5000);
    ws.send(JSON.stringify(value), error => { clearTimeout(timer); error ? reject(new Error('socket write failed')) : resolve(); });
  }));
  pendingWrites.set(ws, next.catch(() => {}));
  return next;
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  // The peer can close before startup reaches the corresponding await.
  promise.catch(() => {});
  return { promise, resolve, reject };
}

function within(promise, ms = 30000) {
  let timer;
  return Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('media initialization timeout')), ms); })])
    .finally(() => clearTimeout(timer));
}

/** One Gateway-owned media listener. Audio never traverses the business server. */
export async function startMediaServer({ port = 8081, host = '0.0.0.0', gatewayPort, backendUrl, identity, logger = { warn() {}, info() {} } }) {
  const server = createServer((_req, res) => { res.writeHead(404); res.end(); });
  const wss = new WebSocketServer({ noServer: true, maxPayload: 20 * 1024 * 1024, perMessageDeflate: false });
  const sessions = new Set();
  const upgrade = (req, socket, head) => {
    if (req.url?.split('?')[0] !== '/agent-service/v1') { socket.end('HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n'); return; }
    wss.handleUpgrade(req, socket, head, ws => wss.emit('connection', ws, req));
  };
  server.on('upgrade', upgrade);
  wss.on('connection', (media, request) => {
    let gcp, visual, sessionId, user, control, ready = false, disposed = false;
    const gatewaySessionId = `media-${randomUUID()}`;
    const imageDeliveries = new Set(), imageAbort = new AbortController();
    const queryNumber = new URL(request.url, 'http://localhost').searchParams.get('number');
    let initialization;
    let audioPackets = 0, outputChunks = 0, uplinkChunks = 0;
    const uplink = new AudioUplink();
    let audioFlushTimer, recoveryTimer;
    const downlink = new AudioDownlink(), voiceReady = deferred(), visualReady = deferred();
    const mediaWriter = new MediaWriter(media);
    const emit = (message, body, options) => mediaWriter.send({ message, ...(sessionId ? { sessionId } : {}), body: JSON.stringify(body) }, options);
    const announceReady = () => emit('audioReady', { version: 1, input: format(16000, 'hex'), output: format(24000, 'base64') });
    const gateway = event => write(gcp, { ...event, event_id: randomUUID() });
    const forwardAudio = async audio => {
      await gateway({ type: 'input_audio_buffer.append', audio });
      if (++uplinkChunks === 1) logger.info('media.audio_input', { status: 'forwarded' });
    };
    const flushAudio = async () => {
      clearTimeout(audioFlushTimer);
      const audio = uplink.flush();
      if (audio && !disposed) await forwardAudio(audio);
    };
    const dispose = () => {
      if (disposed) return;
      disposed = true;
      imageAbort.abort();
      mediaWriter.close();
      clearTimeout(audioFlushTimer);
      clearTimeout(recoveryTimer);
      uplink.flush();
      logger.info('media.closed', { status: `audioPackets=${audioPackets},uplinkChunks=${uplinkChunks},outputChunks=${outputChunks}` });
      voiceReady.reject(new Error('media closed')); visualReady.reject(new Error('media closed'));
      gcp?.terminate(); visual?.terminate(); media.terminate(); sessions.delete(dispose);
    };
    const fail = async code => {
      if (disposed) return;
      logger.warn('media.connection_failed', { status: code });
      try {
        for (const p of downlink.interrupt('GATEWAY_ERROR')) await emit(p.message, p.body);
        await emit('audioError', { code, message: '媒体实时连接不可用，请检查前台占用或后台状态后重新连接' });
      } catch {} finally { dispose(); }
    };
    // Preserve message order, including input received during initialization.
    const consume = (socket, handler) => {
      let chain = Promise.resolve();
      socket.on('message', raw => {
        if (disposed) return;
        chain = chain.then(async () => { if (!disposed) await handler(JSON.parse(raw)); })
          .catch(() => fail('MEDIA_PROTOCOL_ERROR'));
      });
      socket.on('error', () => { void fail('MEDIA_TRANSPORT_ERROR'); });
    };
    sessions.add(dispose);
    media.on('close', dispose);
    async function deliverDetail(task) {
      const texts = new Set();
      for (const artifact of task.artifacts) for (const part of artifact.parts || []) {
        if (part.mediaType === 'text/plain' && typeof part.text === 'string'
            && part.text.startsWith('<detail>') && part.text.endsWith('</detail>')) texts.add(part.text);
      }
      const description = [...texts].join('\n');
      if (!description || disposed) return;
      if (description.length > 16000) throw Error('detail_delivery_too_large');
      await emit('chatResponse', { number: user, taskId: task.id, deliveryId: `detail:${task.id}`,
        message: { type: 'BRIEF', chatIndex: `detail:${task.id}`, content: { intentResult: {
          status: 'SUCCESS', description,
        } } }, displayOnly: true, display_only: true, sequence: 1, final: true });
      logger.info('media.detail_delivery', { taskId: task.id, status: 'sent' });
    }
    async function deliverImages(task) {
      const details = []; let failed = false;
      const parts = task.artifacts.flatMap(a => (a.parts || []).filter(p => p.mediaType?.startsWith('image/')));
      for (const part of parts.slice(0, 4)) {
        try { details.push(await readMediaImage(part, { backendUrl, identity, signal: imageAbort.signal })); }
        catch { failed = true; }
      }
      if (disposed || (!details.length && !failed)) return;
      await emit('chatResponse', { number: user, taskId: task.id, deliveryId: `image:${task.id}`,
        message: { type: 'BRIEF', chatIndex: `image:${task.id}`, content: { intentResult: {
          status: failed && !details.length ? 'FAIL' : 'SUCCESS',
          description: failed ? '图片已生成，但部分图片传输失败，请勿重新生成。' : '',
          ...(details.length ? { detail: details } : {}),
        } } }, displayOnly: !details.length, display_only: !details.length, sequence: 1, final: true }, { image: true });
      logger.info('media.image_delivery', { taskId: task.id,
        status: `sent=${details.length},failed=${failed}` });
    }
    async function openGateway() {
      gcp = new WebSocket(`ws://127.0.0.1:${gatewayPort}/api/realtime?sessionId=${gatewaySessionId}`, { origin: `http://127.0.0.1:${gatewayPort}`, handshakeTimeout: 5000, maxPayload: 524288 });
      consume(gcp, async event => {
        if (event.type === 'session.ping') return gateway({ type: 'session.pong', request_event_id: event.event_id });
        if (['task.completed', 'task.updated'].includes(event.type) && event.task?.status === 'completed'
            && event.task.sessionId === gatewaySessionId && typeof event.task.id === 'string'
            && Array.isArray(event.task.artifacts) && event.task.artifacts.length && !imageDeliveries.has(event.task.id)) {
          imageDeliveries.add(event.task.id);
          void deliverDetail(event.task).catch(() => logger.warn('media.detail_delivery_failed'));
          // Image retrieval must not hold up audio or playback receipts.
          void deliverImages(event.task).catch(() => logger.warn('media.image_delivery_failed'));
        }
        if (event.type === 'voice.ready') {
          requireValue(event.provider === 'dashscope' && event.inputSampleRate === 16000, 'unsupported realtime format');
          voiceReady.resolve();
        }
        if (['error', 'voice.deactivated', 'voice.ownership', 'voice.connection'].includes(event.type)) {
          // Keep diagnostic metadata, never provider messages or payloads.
          const states = ['connected', 'connecting', 'disconnected', 'reconnecting', 'unavailable', 'busy'];
          const codes = ['client_occupied', 'protocol_version_unsupported', 'capability_unsupported',
            'capability_not_negotiated', 'bad_event', 'unknown_type', 'session_expired',
            'payload_too_large', 'rate_limited', 'internal'];
          const code = event.error?.code ?? event.code;
          logger.warn('media.gateway_event', { status: [event.type,
            states.includes(event.state) ? event.state : '',
            codes.includes(code) ? code : 'unclassified'].filter(Boolean).join(':') });
        }
        if (event.type === 'error' && ready && !['client_occupied', 'session_expired', 'protocol_version_unsupported'].includes(event.error?.code ?? event.code)) {
          // GCP command/response errors do not imply loss of the session.
          return;
        }
        if (event.type === 'error' || event.type === 'voice.deactivated' || (event.type === 'voice.ownership' && event.state === 'busy')) {
          return fail((event.error?.code ?? event.code) === 'client_occupied' || event.state === 'busy' ? 'GATEWAY_OCCUPIED' : 'GATEWAY_UNAVAILABLE');
        }
        if (event.type === 'voice.connection' && ['disconnected', 'reconnecting', 'unavailable'].includes(event.state)) {
          if (!ready) return fail('GATEWAY_UNAVAILABLE');
          if (!recoveryTimer) {
            recoveryTimer = setTimeout(() => { void fail('GATEWAY_RECOVERY_TIMEOUT'); }, 15000);
            for (const p of downlink.interrupt('GATEWAY_ERROR')) await emit(p.message, p.body);
            await emit('audioConnecting', { message: 'Realtime session reconnecting' });
          }
          return;
        }
        if (recoveryTimer && (event.type === 'voice.ready' || (event.type === 'voice.connection' && event.state === 'connected'))) {
          clearTimeout(recoveryTimer);
          recoveryTimer = undefined;
          await announceReady();
        }
        for (const p of downlink.project(event)) {
          if (p.message === 'audioStart') logger.info('media.audio_output', { status: 'started' });
          if (p.message === 'audioChunk') outputChunks++;
          await emit(p.message, p.body);
        }
        if (event.type === 'transcript.final' && event.role === 'user'
          && typeof event.turnId === 'string' && event.turnId.trim()
          && typeof event.content === 'string' && event.content.trim()) {
          await emit('asrResult', { turnId: event.turnId, text: event.content, final: true });
        }
        if (event.type === 'transcript.final' && event.role === 'assistant' && typeof event.responseId === 'string' && event.content) {
          const links = ids(event.responseId);
          await emit('chatResponse', { ...links, number: user, message: { type: 'BRIEF', chatIndex: links.taskId,
            content: { intentResult: { description: event.content, status: 'SUCCESS' } } },
          displayOnly: true, display_only: true, audioSource: 'model', sequence: 1, final: true });
        }
      });
      gcp.on('close', () => { if (!disposed) void fail('GATEWAY_DISCONNECTED'); });
      await once(gcp, 'open');
      await gateway({ type: 'session.hello', protocol: { min: '7.0.0', max: '7.0.0' },
        client: { type: 'external-media', instance_id: `media-${randomUUID()}` },
        capabilities: ['input.audio', 'playback.receipts', 'input.image_buffer', 'client.events'],
        connection: { provider: 'dashscope', takeover: false } });
      await within(voiceReady.promise);
    }
    async function openVisual(frame = { message: 'assistantControl', sessionId, body: { number: user, callType: 'VIDEO' } }) {
      const controlBody = typeof frame.body === 'string' ? JSON.parse(frame.body) : frame.body;
      const url = new URL('/internal/media-visual/v1', backendUrl); url.protocol = 'ws:';
      visual = new WebSocket(url, { headers: { 'X-Assistant-User': identity }, handshakeTimeout: 5000, maxPayload: 524288 });
      consume(visual, async frame => {
        const body = typeof frame.body === 'string' ? JSON.parse(frame.body) : frame.body;
        if (frame.message === 'gatewayVisualEvent') {
          requireValue(['input_image_buffer.append', 'input_image_buffer.clear', 'client.event.publish'].includes(body.type), 'invalid visual event');
          await gateway(body);
        } else if (frame.message === 'audioReady') visualReady.resolve();
        else if (frame.message === 'assistantControl' || frame.message === 'assistantControlStartAck') {
          requireValue(body.code === 0 || body.code === 'OK', 'visual control failed');
        } else await emit(frame.message, body);
      });
      visual.on('close', () => { if (!disposed) void fail('VISUAL_BACKEND_DISCONNECTED'); });
      await once(visual, 'open');
      await write(visual, { ...frame, body: JSON.stringify({ ...controlBody,
        clientCapabilities: { ...controlBody.clientCapabilities, audioStreamV1: true } }) });
      await within(visualReady.promise);
    }
    async function initialize(number) {
      requireValue(typeof number === 'string' && number.trim() && number.length <= 128, 'missing user number');
      user = number;
      await emit('audioConnecting', { message: 'Realtime session initializing; input is queued' });
      await openGateway();
      ready = true;
      logger.info('media.ready', { status: 'AUDIO' });
    }
    consume(media, async frame => {
      const body = typeof frame.body === 'string' ? JSON.parse(frame.body) : frame.body;
      requireValue(body && typeof body === 'object' && !Array.isArray(body), 'body must be an object');
      try {
        if (initialization) await initialization;
        if (disposed) return;
        requireValue(frame.sessionId === undefined || (typeof frame.sessionId === 'string' && frame.sessionId.length <= 128), 'invalid sessionId');
        if (sessionId === undefined) sessionId = frame.sessionId;
        if (frame.sessionId !== undefined) requireValue(frame.sessionId === sessionId, 'sessionId mismatch');
        if (['assistantControl', 'assistantControlStart'].includes(frame.message)) {
          requireValue(!control, 'duplicate control');
          requireValue(!body.context || (typeof body.context === 'object' && Object.keys(body.context).length === 0), 'Gateway media does not accept business context; configure the frontend separately');
          const number = frame.message === 'assistantControl' ? body.number : body.userInfo?.number;
          if (ready) requireValue(number === user, 'userNumber mismatch');
          const callType = body.callType || 'AUDIO'; requireValue(['AUDIO', 'VIDEO'].includes(callType), 'invalid callType');
          if (!ready) await (initialization = initialize(number));
          control = frame.message;
          if (callType === 'VIDEO' && !visual) await openVisual(frame);
          await emit(control === 'assistantControl' ? control : 'assistantControlStartAck', control === 'assistantControl' ? { code: 0, message: 'success', phoneNumber: user } : { code: 'OK' });
          await announceReady();
          return;
        }
        requireValue(ready, 'missing connection number; use ?number= or assistantControl');
        if (['audio', 'video', 'chat'].includes(frame.message) && control !== 'assistantControlStart' && body.userNumber !== undefined) requireValue(body.userNumber === user, 'userNumber mismatch');
        switch (frame.message) {
          case 'audio': {
            const chunks = uplink.push(parseAudio(body));
            clearTimeout(audioFlushTimer);
            for (const audio of chunks) await forwardAudio(audio);
            // Do not strand a short final fragment when the media sender stops.
            if (uplink.pending.length) audioFlushTimer = setTimeout(() => {
              void flushAudio().catch(() => fail('MEDIA_TRANSPORT_ERROR'));
            }, 100);
            audioPackets++;
            // Downlink congestion must not stall the independent audio uplink.
            void emit('audioResponse', { code: 0, message: 'audio forwarded' })
              .catch(() => { if (!disposed) void fail('MEDIA_TRANSPORT_ERROR'); });
            break;
          }
          case 'audioStatus': {
            const event = downlink.receipt(body); if (event) await gateway(event); break;
          }
          case 'chat':
            requireValue(typeof body.chatIndex === 'string', 'missing chatIndex');
            logger.info('media.chat_input', { status: 'ASR acknowledged' });
            await emit('chatAck', { code: 0, chatIndex: body.chatIndex, message: 'ASR acknowledged; audio drives realtime' }); break;
          case 'interrupt':
            await flushAudio();
            for (const p of downlink.interrupt('MEDIA_INTERRUPT')) await emit(p.message, p.body);
            await gateway({ type: 'response.cancel' });
            await emit('interrupt', { code: 0, message: 'interrupted' }); break;
          case 'video': case 'videoClear': case 'chatResponseAck':
            if (!visual && frame.message === 'video') await openVisual();
            requireValue(visual, 'video stream not initialized'); await write(visual, frame); break;
          default: throw new Error('unsupported media message');
        }
      } catch (error) {
        // Only adapter-created errors reach this boundary, never Provider payloads.
        logger.warn('media.message_rejected', { status: error.message });
        await emit(frame.message || 'error', { code: 'FAIL', message: error.message });
        if (initialization && !ready) dispose();
      }
    });
    if (queryNumber !== null) {
      initialization = initialize(queryNumber).then(announceReady);
      initialization.catch(() => fail('MEDIA_INITIALIZATION_FAILED'));
    }
  });
  let closed;
  const close = () => closed ||= (async () => {
    for (const dispose of [...sessions]) dispose();
    await new Promise(resolve => wss.close(resolve));
    await new Promise(resolve => server.close(resolve));
  })();
  try {
    server.listen(port, host); await once(server, 'listening');
    return { server, close, upgrade };
  } catch (error) { await close(); throw error; }
}
