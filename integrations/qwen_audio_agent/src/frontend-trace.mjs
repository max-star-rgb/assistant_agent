import { Client } from 'langsmith';
import { RunTree } from 'langsmith/run_trees';
import { configurationFields } from './frontend-execution-events.mjs';

function exportFailure(error, operation) {
  const status = error?.status ?? error?.response?.status;
  const code = error?.cause?.code ?? error?.code;
  const reason = Number.isInteger(status) && status >= 400 && status <= 599 ? `http_${status}`
    : ['ECONNRESET', 'ECONNREFUSED', 'ENOTFOUND', 'EAI_AGAIN', 'ETIMEDOUT', 'UND_ERR_CONNECT_TIMEOUT'].includes(code) ? code
      : ['TimeoutError', 'AbortError', 'TypeError', 'RangeError'].includes(error?.name) ? error.name : 'unknown';
  return { status: `${operation}:${reason}` };
}

function safeValue(value, depth = 0, budget = { chars: 32000, nodes: 1024 }) {
  if (depth > 8 || --budget.nodes < 0 || budget.chars <= 0) return '[truncated]';
  if (typeof value === 'string') {
    const text = value.slice(0, Math.min(16000, budget.chars));
    budget.chars -= text.length;
    return text;
  }
  if (value === null || typeof value !== 'object') return value;
  if (Array.isArray(value)) return value.slice(0, 128).map(item => safeValue(item, depth + 1, budget));
  return Object.fromEntries(Object.entries(value).slice(0, 128)
    .filter(([key]) => !/authorization|api.?key|token$|password|secret|base64|audio|image|pcm/i.test(key))
    .map(([key, item]) => [key, safeValue(item, depth + 1, budget)]));
}

function trimHistory(session) {
  let chars = session.history.reduce((sum, item) => sum + JSON.stringify(item.messages).length, 0);
  let count = session.history.reduce((sum, item) => sum + item.messages.length, 0);
  while (count > 256 || chars > 128 * 1024) {
    const removed = session.history.shift();
    chars -= JSON.stringify(removed.messages).length;
    count -= removed.messages.length;
    session.trimmed = true;
  }
}

/** Consumes execution facts only. Never drives the frontend or its tools. */
export class FrontendTrace {
  constructor({ client, project = 'default', model, namespace, hideInputs = false, hideOutputs = false, warn = () => {} } = {}) {
    Object.assign(this, { client, project, model, namespace, hideInputs, hideOutputs, warn });
    // RunTree logs caught SDK errors verbatim; sanitize at its client boundary.
    this.exporter = Object.fromEntries(['createRun', 'updateRun'].map(method => [method,
      async (...args) => {
        if (this.stoppingExports) return;
        // A PATCH without end_time may be finalized by the ingestion API.
        if (method === 'updateRun' && args[1].end_time === undefined) args[1].end_time = null;
        try { await client[method](...args); }
        catch (error) { warn('frontend_trace_export_failed', exportFailure(error, method)); }
      }]));
    this.sessions = new Map();
    this.queue = Promise.resolve();
    this.pending = 0;
  }

  onExecution({ sessionId, event }) {
    if (this.closed) return;
    // Bound observer backlog during exporter outages; do not retain audio/payloads.
    if (this.pending >= 2048) { this.warn('frontend_trace_overflow'); return; }
    event = { ...event, input: safeValue(event.input), output: safeValue(event.output),
      usage: safeValue(event.usage), text: typeof event.text === 'string' ? event.text.slice(0, 16000) : undefined };
    event.configuration = !this.hideInputs && event.configuration ? {
      session: configurationFields(event.configuration.session), response: configurationFields(event.configuration.response),
    } : undefined;
    event.toolResults = !this.hideInputs && !this.hideOutputs ? event.toolResults?.slice(-64).map(item => {
      let output = item.output;
      try { output = JSON.parse(output); } catch { /* Plain text result. */ }
      const filtered = safeValue(output);
      return { call_id: item.call_id, output: typeof filtered === 'string' ? filtered : JSON.stringify(filtered) };
    }) : undefined;
    if (this.hideInputs) { delete event.input; if (event.kind === 'input') delete event.text; }
    if (this.hideOutputs) { delete event.output; if (event.kind !== 'input') delete event.text; }
    this.pending++;
    this.queue = this.queue.then(() => this.accept(sessionId, event))
      .catch(error => this.warn('frontend_trace_export_failed', exportFailure(error, 'projection')))
      .finally(() => this.pending--);
  }

  async finish(run, status, time, output = {}) {
    if (run.end_time) return;
    await run.end(this.hideOutputs ? {} : { ...output, status },
      status === 'failed' ? 'frontend_execution_failed' : undefined, time);
    await run.patchRun({ excludeInputs: false });
  }

  async finishTurn(turn, status, time) {
    for (const run of [...turn.models.values(), ...turn.tools.values()]) {
      await this.finish(run, status, time);
    }
    await this.finish(turn.root, status, time, { messages: turn.answer ? [{ role: 'assistant', content: turn.answer }] : [] });
  }

  async accept(sessionId, e) {
    let session = this.sessions.get(sessionId);
    if (e.kind === 'session.start' && session) {
      if (e.providerSessionId && session.providerSessionId === e.providerSessionId) return;
      for (const turn of session.turns.values()) await this.finishTurn(turn, 'closed', e.time);
      this.sessions.delete(sessionId);
      session = undefined;
    }
    if (!session) {
      if (this.sessions.size >= 32) { this.warn('frontend_trace_session_limit'); return; }
      session = { turns: new Map(), history: [], trimmed: false, providerSessionId: e.providerSessionId };
      this.sessions.set(sessionId, session);
    }
    const turns = session.turns;
    if (e.kind === 'session.start') return;
    if (e.kind === 'history.remove') {
      // Without text/audio alignment, omit the affected item from future snapshots.
      session.history = session.history.filter(item => !item.itemIds.includes(e.itemId));
      return;
    }
    if (e.kind === 'session.end' || e.kind === 'interrupt' || e.kind === 'session.error') {
      for (const turn of turns.values()) await this.finishTurn(turn,
        e.kind === 'interrupt' ? 'interrupted' : e.kind === 'session.error' ? 'failed' : 'closed', e.time);
      if (e.kind === 'session.end' || e.kind === 'session.error') this.sessions.delete(sessionId);
      return;
    }
    if (!e.turnId) return;
    if (e.kind === 'turn.start') {
      for (const [id, turn] of turns) if (id !== e.turnId) await this.finishTurn(turn, 'interrupted', e.time);
    }
    let turn = turns.get(e.turnId);
    if (!turn) {
      if (turns.size >= 128) {
        const [id, oldest] = turns.entries().next().value;
        await this.finishTurn(oldest, 'closed', e.time);
        turns.delete(id);
      }
      const root = new RunTree({ name: 'FrontendAgent', run_type: 'chain', client: this.exporter,
        project_name: this.project, start_time: e.time, inputs: {},
        extra: { metadata: { thread_id: sessionId, voice_session_id: sessionId, turn_id: e.turnId,
          source: 'qwen_audio_frontend', context_representation: 'observed_text' } } });
      root.extra.metadata.qwen_namespace = this.namespace;
      root.extra.metadata.provider_session_id = session.providerSessionId;
      turn = { root, models: new Map(), tools: new Map(), answer: '' };
      turns.set(e.turnId, turn);
      if (!this.hideInputs && e.origin !== 'announcement' && !e.turnId.startsWith('announcement:')) {
        // Reserve the input position so late ASR never moves behind its answer.
        session.history.push({ key: `user:${e.turnId}`, itemIds: [], messages: [{ role: 'user', content: '' }] });
        trimHistory(session);
      }
      await root.postRun();
    }
    if (e.kind === 'input') {
      turn.root.inputs = this.hideInputs ? {} : { messages: [{ role: 'user', content: e.text }] };
      await turn.root.patchRun({ excludeInputs: false });
      if (!this.hideInputs) {
        const key = `user:${e.turnId}`;
        const entry = session.history.find(item => item.key === key);
        if (entry) {
          entry.messages = structuredClone(turn.root.inputs.messages);
          if (e.itemId && !entry.itemIds.includes(e.itemId)) entry.itemIds.push(e.itemId);
          trimHistory(session);
        }
        // Only replace reserved slots; never append later events to an old snapshot.
        for (const observedTurn of turns.values()) for (const model of observedTurn.models.values()) {
          const index = model.observedInputKeys?.indexOf(key) ?? -1;
          if (index < 0) continue;
          model.inputs.messages[index] = structuredClone(turn.root.inputs.messages[0]);
          const offset = model.observedInputKeys[0] === null ? 1 : 0;
          const snapshot = { history: [], trimmed: model.inputs.observation.history_trimmed };
          for (let i = offset; i < model.inputs.messages.length; i++) {
            const key = model.observedInputKeys[i];
            if (snapshot.history.at(-1)?.key !== key) snapshot.history.push({ key, messages: [] });
            snapshot.history.at(-1).messages.push(model.inputs.messages[i]);
          }
          trimHistory(snapshot);
          model.inputs.messages = [...model.inputs.messages.slice(0, offset), ...snapshot.history.flatMap(item => item.messages)];
          model.observedInputKeys = [...model.observedInputKeys.slice(0, offset), ...snapshot.history.flatMap(item => item.messages.map(() => item.key))];
          model.inputs.observation.history_trimmed = snapshot.trimmed;
          model.extra.metadata.asr_backfilled = true;
          await model.patchRun({ excludeInputs: false });
        }
      }
      return;
    }
    if (turn.root.end_time) return;
    const isTool = e.kind.startsWith('tool.');
    const children = isTool ? turn.tools : turn.models;
    const id = isTool ? e.callId : e.responseId;
    if (!id) return;
    let run = children.get(id);
    if (!run) {
      if (children.size >= 256) { await this.finishTurn(turn, 'closed', e.time); return; }
      const metadata = isTool ? { call_id: id, response_id: e.responseId } : {
        response_id: id, ls_provider: 'dashscope', ls_model_name: this.model,
        // response.created is observable; it is not the generation request time.
        ttft_origin: 'unavailable', origin: e.origin, task_id: e.taskId,
      };
      const configuration = e.configuration;
      const parameters = configuration ? { ...configuration.session, ...configuration.response } : {};
      const messages = [
        ...(typeof parameters.instructions === 'string' ? [{ role: 'system', content: parameters.instructions }] : []),
      ];
      const inputKeys = messages.map(() => null);
      if (!isTool && !this.hideInputs) {
        const sentResults = new Map((e.toolResults || []).map(item => [item.call_id, item.output]));
        for (const entry of session.history) {
          for (const call of entry.messages[0]?.tool_calls || []) {
            if (sentResults.has(call.id) && !entry.messages.some(message => message.tool_call_id === call.id)) {
              entry.messages.push({ role: 'tool', tool_call_id: call.id, content: sentResults.get(call.id) });
            }
          }
        }
        trimHistory(session);
        for (const entry of session.history) {
          messages.push(...structuredClone(entry.messages));
          inputKeys.push(...entry.messages.map(() => entry.key));
        }
      }
      const { instructions: _instructions, ...invocationParams } = parameters;
      run = turn.root.createChild({ name: isTool ? e.name || 'frontend_tool' : 'QwenRealtime',
        run_type: isTool ? 'tool' : 'llm', start_time: e.time,
        inputs: this.hideInputs ? {} : isTool ? { arguments: e.input } : { messages,
          observation: { representation: 'observed_text_history', history_trimmed: session.trimmed },
          ...(configuration ? { session_configuration: configuration.session, response_configuration: configuration.response } : {}) },
        extra: { metadata: { ...metadata, ...(!isTool ? { conversation_history: 'provider_managed', input_scope: 'observed_session_history', tool_result_source: 'sent_wire', configuration_source: configuration ? 'sent_wire' : 'unavailable' } : {}) },
          ...(!this.hideInputs && !isTool && configuration ? { invocation_params: invocationParams } : {}) } });
      children.set(id, run);
      if (!isTool && !this.hideInputs) run.observedInputKeys = inputKeys;
      await run.postRun();
    }
    if (run.end_time) return;
    if (e.kind === 'model.delta') {
      run.observedText = ((run.observedText || '') + (e.text || '')).slice(0, 16000);
      return;
    }
    if (e.kind === 'model.text') { run.observedText = e.text || run.observedText; return; }
    if (e.kind === 'tool.end') {
      run.extra.metadata.qwen_task_id = e.taskId;
      await this.finish(run, e.failed ? 'failed' : 'completed', e.time, { result: e.output });
      if (turn.finalResponse && [...turn.tools.values()].every(t => t.end_time)) {
        await this.finishTurn(turn, 'completed', e.time);
      }
    } else if (e.kind === 'model.end') {
      const text = e.text || run.observedText || '';
      turn.answer = text || turn.answer;
      if (e.usage) run.extra.metadata.usage_metadata = e.usage;
      const calls = [...turn.tools.entries()].filter(([, tool]) => tool.extra.metadata.response_id === e.responseId)
        .map(([id, tool]) => ({ id, type: 'function', function: { name: tool.name, arguments: JSON.stringify(tool.inputs.arguments || {}) } }));
      const status = e.status === 'completed' ? 'completed' : e.status === 'cancelled' ? 'interrupted' : 'failed';
      turn.finalResponse = !e.hasTools;
      await this.finish(run, status, e.time, { message: { role: 'assistant', content: text,
        ...(!this.hideInputs && calls.length ? { tool_calls: calls } : {}) } });
      if (!this.hideInputs && !this.hideOutputs && (text || calls.length)) {
        session.history.push({ key: `model:${id}`, itemIds: e.itemIds || [], messages: [structuredClone(run.outputs.message)] });
        trimHistory(session);
      }
      if (status !== 'completed' || (!e.hasTools && [...turn.tools.values()].every(t => t.end_time))) {
        await this.finishTurn(turn, status, e.time);
      }
    }
  }

  async close() {
    this.closed = true;
    this.queue = this.queue.then(async () => {
      for (const id of [...this.sessions.keys()]) await this.accept(id, { kind: 'session.end', time: Date.now() });
    });
    let timer;
    await Promise.race([this.queue, new Promise(resolve => {
      timer = setTimeout(() => { this.stoppingExports = true; this.warn('frontend_trace_flush_timeout'); resolve(); }, 5000);
    })]);
    clearTimeout(timer);
    await this.queue;
    try { await this.client.awaitPendingTraceBatches?.(); }
    catch (error) { this.warn('frontend_trace_export_failed', exportFailure(error, 'flush')); }
  }
}

export function createFrontendTrace(config, warn) {
  if (!config?.enabled) return null;
  try {
    const client = new Client({ apiKey: config.apiKey, apiUrl: config.endpoint,
      timeout_ms: 5000, callerOptions: { maxRetries: 0 },
      autoBatchTracing: false,
      maxIngestMemoryBytes: 8 * 1024 * 1024,
      hideInputs: config.hideInputs, hideOutputs: config.hideOutputs });
    return new FrontendTrace({ ...config, client, warn });
  } catch { warn?.('frontend_trace_configuration_failed'); return null; }
}
