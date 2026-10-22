import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';

// shortcut: 2.0.1 has internal hooks but no public execution observer injection.
// Remove this version-guarded patch once upstream exposes equivalent lifecycle facts.
export async function patchFrontendObservation() {
  const root = fileURLToPath(new URL('../../../', import.meta.resolve('qwen-audio-agent/gateway-application')));
  const { version } = JSON.parse(await readFile(join(root, 'package.json'), 'utf8'));
  if (version !== '2.0.1') throw new Error('review_frontend_observation_patch_for_new_upstream');
  const changes = [
    ['server/src/app/gateway-application.mjs', [
      ['  sessionJournal = null,', '  sessionObservers = [],\n  sessionJournal = null,'],
      ['  sessionObservers: [', '  sessionObservers: [\n    ...sessionObservers,'],
    ]],
    ['server/src/voice/realtime-session-runtime.mjs', [
      ['  const frontendRecentMessages = () => realtimeRecoveryContext.project(\n    conversationSync.frontendContext({ ownerId, sessionId }),\n  )', '  // Provider owns conversation history; local records are UI/Memory observations only.'],
      ['    recentMessages: frontendRecentMessages(),', '    recentMessages: [],'],
      ['        const recentMessages = conversationSync.frontendContext({ ownerId, sessionId })', '        const recentMessages = [] // Never replay local conversation after reconnect.'],
      ["import { randomUUID } from 'node:crypto'", "import { publicExecutionEvent, modelExecutionEvent } from './frontend-execution-events.mjs'\nimport { randomUUID } from 'node:crypto'"],
      ['  const emit = event => { if (!closed) send(event) }', `  const observeExecution = event => {
    if (event) observers.emit('onExecution', { ownerId, sessionId,
      event: { ...event, time: Date.now() }, logger: connectionLogger })
  }
  const emit = event => {
    if (!closed) {
      observeExecution(publicExecutionEvent(event))
      send(event)
    }
  }`],
      ['    if (inputs.handleProviderEvent(event)) return', `    const observedResponseContext = presentationRuntime.get(realtimeResponseId(event))
    if (event.type !== 'response.done') observeExecution(modelExecutionEvent(event, observedResponseContext))
    // Resolve ASR through the retained item mapping even when the input is stale.
    if (event.type === 'conversation.item.input_audio_transcription.completed') {
      const context = turns.correlation.resolve(event.item_id, null)
      if (context) observeExecution({ kind: 'input', turnId: context.turnId, text: event.transcript, itemId: event.item_id })
    }
    if (inputs.handleProviderEvent(event)) return`],
      ['        onResponseDone({', "        observeExecution(modelExecutionEvent(event, observedResponseContext))\n        onResponseDone({"],
      ['    onToolResultReady: ({ callId, turnId, toolName, failed, errorCode }) => {', `    onToolResultReady: ({ callId, turnId, toolName, failed, errorCode, output, taskId }) => {
      observeExecution({ kind: 'tool.end', callId, turnId, name: toolName, failed, output, taskId })`],
      ["    } else if (event.type === GatewayClientEvent.INTERRUPT) {", "    } else if (event.type === GatewayClientEvent.INTERRUPT) {\n      observeExecution({ kind: 'interrupt' })"],
      ["      observers.emit('onSessionClosed',", "      observeExecution({ kind: 'session.end' })\n      observers.emit('onSessionClosed',"],
      ['    onDisconnected: () => {', "    onDisconnected: () => {\n      observeExecution({ kind: 'session.error' })"],
      ["          connectionLogger.warn('realtime.tool_call.failed', {", "          observeExecution({ kind: 'tool.end', ...callFields, name: callFields.toolName, failed: true, output: { status: 'failed' } })\n          connectionLogger.warn('realtime.tool_call.failed', {"],
    ]],
    ['server/src/frontend/tools/tool-call-handler.mjs', [
      ['      this.onToolResultReady({\n        callId,', '      this.onToolResultReady({\n        output,\n        callId,'],
      // External tool sources may omit policy; preserve the default result limit.
      ['      tool?.policy.maxResultBytes,', '      tool?.policy?.maxResultBytes,'],
    ]],
    ['server/src/voice/realtime-provider.mjs', [
      ['        item,\n        resolve,', '        item,\n        contextOnly,\n        resolve,'],
      ["import WebSocket from 'ws'", "import { configurationFields, recordSentConfiguration } from './frontend-execution-events.mjs'\nimport WebSocket from 'ws'"],
      ['    this.agentContext = agentContext\n', '    this.agentContext = agentContext\n    this.observedSessionConfiguration = {}\n'],
      ['    this.agentContext = agentContext\n    this.observedSessionConfiguration = {}\n', '    this.agentContext = agentContext\n    this.observedSessionConfiguration = {}\n    this.observedToolResults = []\n'],
      ['        this.recentContextInjected = false', '        this.recentContextInjected = false\n        this.observedSessionConfiguration = {}'],
      ['        this.recentContextInjected = false\n        this.observedSessionConfiguration = {}', '        this.recentContextInjected = false\n        this.observedSessionConfiguration = {}\n        this.observedToolResults = []'],
      ["      event.__voiceOrigin = pending?.origin || 'model'", `      event.__executionConfiguration = pending?.observedConfiguration || {
        session: configurationFields(this.observedSessionConfiguration), response: {},
      }
      event.__voiceOrigin = pending?.origin || 'model'`],
      ['      event.__voiceOrigin = pending?.origin || \'model\'', '      event.__voiceOrigin = pending?.origin || \'model\'\n      event.__executionToolResults = (this.observedToolResults || []).map(item => ({ ...item }))'],
      ['    return sendBoundedWebSocket(this.ws, JSON.stringify(body), {', '    const sent = sendBoundedWebSocket(this.ws, JSON.stringify(body), {'],
      ['    })\n  }\n\n  send(payload, options = {}) {', `    })
    if (sent) {
      try { recordSentConfiguration(this, body) } catch { /* Observation never changes transport. */ }
    }
    return sent
  }

  send(payload, options = {}) {`],
    ]],
  ];
  const planned = [];
  for (const [relative, replacements] of changes) {
    const path = join(root, relative);
    let content = await readFile(path, 'utf8');
    // Upgrade the previous observer patch before checking its complete anchor.
    content = content.replace("if (context) observeExecution({ kind: 'input', turnId: context.turnId, text: event.transcript })",
      "if (context) observeExecution({ kind: 'input', turnId: context.turnId, text: event.transcript, itemId: event.item_id })");
    for (const [before, after] of replacements) {
      if (content.includes(after)) continue;
      if (content.split(before).length !== 2) throw new Error('frontend_observation_patch_target_changed');
      content = content.replace(before, after);
    }
    planned.push([path, content]);
  }
  const projection = await readFile(new URL('./frontend-execution-events.mjs', import.meta.url), 'utf8');
  await writeFile(join(root, 'server/src/voice/frontend-execution-events.mjs'), projection);
  for (const [path, content] of planned) await writeFile(path, content);
}
