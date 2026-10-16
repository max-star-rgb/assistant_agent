const $ = id => document.getElementById(id);
let ws, heartbeat, micStream, inputContext, outputContext, captureNode, sourceNode;
let generation = 0, micGeneration = 0, sequence = 0, sent = 0, playbackAt = 0;
let busy = false, stopping = false, playbackEnabled = false;
let receivedSamples = 0, receivedPeak = 0;
const playing = new Set();
let turnId = null;
function playbackStatus() {
  const state = outputContext?.state || '未启动';
  $('playback-state').textContent = `收到回复音频 ${(receivedSamples / 24000).toFixed(2)} 秒 · 播放器 ${state} · 待播放 ${playing.size} 段${receivedSamples && !receivedPeak ? ' · 音频为静音' : ''}`;
}
async function enableSpeaker() {
  if (!outputContext || outputContext.state === 'closed') {
    outputContext = new AudioContext();
    outputContext.onstatechange = playbackStatus;
  }
  await outputContext.resume();
  playbackStatus();
}
function log(text) {
  const lines = $('log').textContent.split('\n').filter(Boolean);
  lines.push(`${new Date().toLocaleTimeString()}  ${text}`);
  $('log').textContent = lines.slice(-40).join('\n'); $('log').scrollTop = $('log').scrollHeight;
}
function error(err) {
  const descriptions = {NotFoundError: '没有找到麦克风。请连接麦克风，或检查浏览器的音频输入设备；不需要摄像头。',
    NotAllowedError: '麦克风权限被拒绝。请在浏览器地址栏的权限设置中允许麦克风。',
    NotReadableError: '麦克风暂时无法使用，可能被其他应用占用。'};
  $('error').textContent = descriptions[err?.name] || err?.message || String(err); log($('error').textContent);
}
function control(message) { if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(message)); }
function buttons() {
  const connected = ws?.readyState === WebSocket.OPEN;
  $('mic').disabled = !connected || stopping;
  $('mic').textContent = micStream ? '结束持续对话' : '开始持续对话';
  if (micStream && !busy && !playing.size) $('mic-state').textContent = '持续收音中，可以随时说话';
  $('cancel').disabled = !connected || (!busy && !playing.size);
}
function silence() {
  playbackEnabled = false;
  for (const source of playing) { try { source.stop(); } catch {} }
  playing.clear(); playbackAt = 0;
}
function releaseMic() {
  micGeneration++;
  if (captureNode) captureNode.port.onmessage = null;
  captureNode?.disconnect(); sourceNode?.disconnect(); captureNode = sourceNode = null;
  micStream?.getTracks().forEach(t => t.stop()); micStream = null;
  inputContext?.close().catch(() => {}); inputContext = null;
  $('level').value = 0;
}
function discardMic() {
  releaseMic(); control({type: 'audio.discard'}); stopping = false;
  $('mic-state').textContent = '点击开始持续对话，之后无需逐句发送'; buttons();
}
function sendPcm(pcm) {
  if (ws?.readyState !== WebSocket.OPEN) return;
  if (ws.bufferedAmount > 256 * 1024) { discardMic(); error(new Error('网络积压，本段录音已丢弃，请重试。')); return; }
  const packet = new ArrayBuffer(13 + pcm.byteLength), view = new DataView(packet);
  view.setUint8(0, 1); view.setUint32(1, sequence++, true); view.setFloat64(5, Date.now(), true);
  new Uint8Array(packet, 13).set(new Uint8Array(pcm)); ws.send(packet); sent += pcm.byteLength;
  $('bytes').textContent = `${(sent / 1024).toFixed(1)} KB`;
}
async function startMic() {
  const current = ++micGeneration; $('mic').disabled = true; $('error').textContent = ''; silence();
  try {
    await enableSpeaker();
    if (current !== micGeneration) return;
    const stream = await navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true}, video: false});
    if (current !== micGeneration || ws?.readyState !== WebSocket.OPEN) { stream.getTracks().forEach(t => t.stop()); return; }
    micStream = stream;
    const context = new AudioContext(); inputContext = context;
    await context.audioWorklet.addModule('/media-lab/assets/capture-worklet.js');
    if (current !== micGeneration) return;
    await context.resume();
    if (current !== micGeneration) return;
    playbackEnabled = true;
    control({type: 'audio.start', sample_rate: 16000, channels: 1, format: 'pcm_s16le'});
    sourceNode = context.createMediaStreamSource(stream); captureNode = new AudioWorkletNode(context, 'pcm-capture');
    captureNode.port.onmessage = ({data}) => {
      if (!data.pcm) return;
      $('level').value = data.peak;
      captureFrame(data.pcm);
    };
    sourceNode.connect(captureNode); captureNode.connect(context.destination);
    for (const track of stream.getTracks()) track.onended = () => { discardMic(); error(new Error('麦克风已断开，请检查设备。')); };
    $('mic-state').textContent = '持续收音中，由模型判断讲话起止';
  } catch (err) { discardMic(); error(err); }
  finally { buttons(); }
}
function captureFrame(pcm) {
  // Keep sending through silence, model generation, playback and native tool execution.
  if (micStream) sendPcm(pcm);
}
function play(packet) {
  if (packet.byteLength <= 13) return;
  const view = new DataView(packet), count = (packet.byteLength - 13) / 2;
  if (view.getUint8(0) !== 3 || !Number.isInteger(count)) return;
  receivedSamples += count;
  for (let i = 0; i < count; i++) receivedPeak = Math.max(receivedPeak, Math.abs(view.getInt16(13 + i * 2, true)));
  playbackStatus();
  if (!playbackEnabled || !outputContext) return;
  if (receivedSamples === count) log(`收到首段回复音频，播放器 ${outputContext.state}`);
  if (outputContext.state !== 'running') $('error').textContent = '音频已收到，但播放器暂停了。请点击「测试扬声器 / 恢复播放」。';
  if (playbackAt - outputContext.currentTime > 90) {
    control({type: 'turn.cancel'}); silence(); error(new Error('回复音频过长，已停止本轮。')); return;
  }
  const buffer = outputContext.createBuffer(1, count, 24000), samples = buffer.getChannelData(0);
  for (let i = 0; i < count; i++) samples[i] = view.getInt16(13 + i * 2, true) / 32768;
  const source = outputContext.createBufferSource(); source.buffer = buffer; source.connect(outputContext.destination);
  playing.add(source); source.onended = () => { playing.delete(source); buttons(); playbackStatus(); };
  playbackAt = Math.max(playbackAt, outputContext.currentTime + 0.03);
  source.start(playbackAt); playbackAt += buffer.duration; buttons(); playbackStatus();
}
function disconnect() {
  generation++; clearInterval(heartbeat); discardMic(); silence();
  const socket = ws; ws = null; socket?.close(); busy = false;
  outputContext?.close().catch(() => {}); outputContext = null;
  $('status').textContent = '未连接'; $('connect').disabled = false; $('disconnect').disabled = true; buttons();
}
$('connect').onclick = () => {
  if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) { error(new Error('请使用 Chrome 或 Edge，通过 localhost 打开页面。')); return; }
  $('error').textContent = ''; $('connect').disabled = true; $('disconnect').disabled = false; $('status').textContent = '正在连接';
  sent = sequence = 0; turnId = null; $('bytes').textContent = '0 KB'; $('audio-count').textContent = '0.0 秒';
  const current = ++generation;
  ws = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/media-lab/ws`); ws.binaryType = 'arraybuffer';
  ws.onmessage = ({data}) => {
    if (current !== generation) return;
    if (data instanceof ArrayBuffer) { play(data); return; }
    const event = JSON.parse(data);
    if (event.type === 'session.ready') {
      $('status').textContent = '已连接 · 语音对话'; log(`会话 ${event.thread_id}`); buttons();
      heartbeat = setInterval(() => control({type: 'ping', time: performance.now()}), 5000);
    } else if (event.type === 'media.received') $('audio-count').textContent = `${(event.audio_samples / 16000).toFixed(1)} 秒`;
    else if (event.type === 'pong') $('rtt').textContent = `${Math.round(performance.now() - event.time)} ms`;
    else if (event.type === 'voice.speech_started') {
      turnId = event.turn_id; silence(); playbackEnabled = true; busy = false;
      receivedSamples = receivedPeak = 0; playbackStatus();
      $('user-text').textContent = '正在识别…'; $('reply-text').textContent = '';
      $('mic-state').textContent = '正在说话 · 已停止上一轮播放'; buttons();
    } else if (event.turn_id && turnId && event.turn_id !== turnId) return;
    else if (event.type === 'voice.interrupted') { silence(); busy = false; buttons(); }
    else if (event.type === 'voice.speech_stopped') { busy = true; $('mic-state').textContent = 'Agent 正在回答，可直接开口打断'; buttons(); }
    else if (event.type === 'voice_input_transcript') $('user-text').textContent = event.text;
    else if (event.type === 'voice_reply_text') $('reply-text').textContent += event.text;
    else if (event.type === 'voice_tool_preamble') log(event.text);
    else if (event.type === 'business.task') log(`业务任务 ${event.task_id || ''}：${event.status}（结束通话不取消任务）`);
    else if (event.type === 'turn.started') { busy = true; buttons(); }
    else if (event.type === 'turn.finished') { busy = false; $('mic-state').textContent = playing.size ? '正在播放回复' : '可以继续说话'; buttons(); }
    else if (event.type === 'recording.error') { discardMic(); busy = false; buttons(); error(new Error(event.message)); }
    else if (event.type === 'turn.error' || event.type === 'error') error(new Error(event.message));
  };
  ws.onerror = () => { if (current === generation) error(new Error('连接失败，请确认 Agent Server 正在运行。')); };
  ws.onclose = () => { if (current === generation) { disconnect(); log('连接已关闭，麦克风已释放'); } };
};
$('disconnect').onclick = disconnect;
$('speaker-test').onclick = async () => {
  try {
    await enableSpeaker();
    const tone = outputContext.createOscillator(), gain = outputContext.createGain();
    tone.frequency.value = 660; gain.gain.setValueAtTime(0.08, outputContext.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, outputContext.currentTime + 0.3);
    tone.connect(gain); gain.connect(outputContext.destination);
    tone.onended = () => { tone.disconnect(); gain.disconnect(); };
    tone.start(); tone.stop(outputContext.currentTime + 0.3);
    $('error').textContent = ''; log(`已发送扬声器测试音，播放器 ${outputContext.state}`);
  } catch (err) { error(err); }
};
$('mic').onclick = () => {
  if (micStream) disconnect();
  else startMic();
};
$('cancel').onclick = () => { silence(); control({type: 'turn.cancel'}); $('mic-state').textContent = '已停止语音回复，业务任务继续执行'; buttons(); };
window.addEventListener('pagehide', disconnect);
document.addEventListener('visibilitychange', () => { if (document.hidden && micStream) { disconnect(); log('页面进入后台，持续语音已结束'); } });
