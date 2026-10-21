import { randomUUID } from 'node:crypto';
import { setTimeout as delay } from 'node:timers/promises';
import { AgentServerError } from './agent-server-client.mjs';
import { finalResult } from './task-results.mjs';

function interrupts(state) { return (state.tasks || []).flatMap(task => task.interrupts || []); }


/** Owns correlation and observation only; Agent Server owns execution and checkpoints. */
export class LangGraphBackendAdapter {
  constructor({ client, ownerId, pollIntervalMs = 1000, timeoutMs = 3600000 }) {
    Object.assign(this, { client, ownerId, pollIntervalMs, timeoutMs });
    this.active = new Map();
    this.listeners = new Set();
    this.closed = false;
  }
  describe() { return { protocol: 'langgraph', label: 'Assistant Agent', configured: true,
    capabilities: { authorization: false, interactiveInput: true } }; }
  async start() { if (this.closed) throw new Error('adapter_closed'); return { ok: true }; }
  async health() { await this.client.request('/info'); return { ok: !this.closed }; }
  status(taskId, { ownerId } = {}) {
    if (!taskId) return { state: this.closed ? 'closed' : 'ready', active: this.active.size };
    const record = this.active.get(taskId);
    return record && ownerId === this.ownerId
      ? { taskId, state: 'working', phase: record.phase }
      : { taskId, state: 'not_found' };
  }
  subscribe(listener) { this.listeners.add(listener); return () => this.listeners.delete(listener); }
  emit(record, type, details) {
    if (this.closed || record.stopped) return;
    const event = { type, taskId: record.task.id, ownerId: this.ownerId, ...details };
    for (const listener of new Set([...this.listeners, record.onEvent].filter(Boolean))) {
      try { listener(event); } catch { /* Observers do not control business execution. */ }
    }
  }
  record(taskId, ownerId) {
    const record = this.active.get(taskId);
    if (!record || ownerId !== this.ownerId) throw new Error('task_not_found_for_owner');
    return record;
  }
  async submit(task, { onEvent, signal } = {}) {
    if (!task?.id || !task.ownerId || typeof task.instruction !== 'string' || !task.instruction.trim()) throw new Error('requires task id, owner and input');
    if (task.ownerId !== this.ownerId) throw new Error('invalid_owner');
    if (task.id.length > 128 || task.instruction.length > 16000) throw new Error('task_limit_exceeded');
    if (task.inputParts?.length) throw new Error('unsupported_attachment');
    if (this.closed) throw new Error('adapter_closed');
    if (this.active.has(task.id)) throw new Error('task already active');
    const record = { task, onEvent, phase: 'submitting', controller: new AbortController(), mutation: null, pending: null };
    this.active.set(task.id, record);
    const aborted = () => { record.stopped = 'observation_closed'; record.controller.abort(); };
    signal?.addEventListener('abort', aborted, { once: true });
    try {
      if (signal?.aborted) throw new Error('observation_closed');
      record.submission = this.client.submit(task);
      record.handle = await record.submission;
      if (record.stopped || this.closed) throw new Error('adapter_closed');
      record.phase = 'running';
      this.emit(record, 'backend.activity', { activity: { id: 'run', kind: 'status', status: 'running', message: '业务已受理' } });
      return await this.observe(record);
    } finally {
      signal?.removeEventListener('abort', aborted);
      this.active.delete(task.id);
    }
  }
  async observe(record) {
    const deadline = Date.now() + this.timeoutMs;
    let failures = 0;
    while (!record.stopped && !this.closed) {
      if (Date.now() >= deadline) throw new Error('observation_timeout_business_may_continue');
      if (record.mutation) {
        // Operation errors belong to their caller; keep observing the remote run.
        await record.mutation.catch(() => {});
        await delay(this.pollIntervalMs);
        continue;
      }
      if (record.stopped) break;
      const handle = record.handle;
      try {
        const run = await this.client.run(handle);
        if (record.mutation) continue;
        if (record.stopped) break;
        if (['error', 'timeout'].includes(run.status)) throw new Error(`business_${run.status}`);
        if (['success', 'interrupted'].includes(run.status)) {
          const state = await this.client.state(handle);
          if (record.mutation || record.handle !== handle) continue;
          if (record.stopped) break;
          // Thread state can advance while the old run still reports success.
          const latest = await this.client.latestRun(handle.threadId);
          if (record.mutation || record.stopped) continue;
          if (latest && latest.run_id !== handle.runId) {
            record.handle = { threadId: handle.threadId, runId: latest.run_id };
            this.resolvePending(record, 'resolved');
            record.phase = 'running';
            continue;
          }
          const pending = interrupts(state);
          if (pending.length) await this.waitForInput(record, pending, state.checkpoint);
          else if (run.status === 'success') {
            const result = await finalResult(state, this.client, handle);
            if (record.stopped || this.closed) throw Error(record.stopped || 'adapter_closed');
            return result;
          }
          else if (!record.pending) throw new Error('business_interrupted');
        } else if (!['pending', 'running'].includes(run.status)) throw new Error('unsupported_run_status');
        failures = 0;
      } catch (error) {
        if (!(error instanceof AgentServerError) || (error.status && error.status < 500)) throw error;
        if (++failures >= 5) throw error;
      }
      try { await delay(Math.min(this.pollIntervalMs * 2 ** failures, 10000), undefined, { signal: record.controller.signal }); }
      catch { /* Close/cancel wakes monitoring without cancelling a remote run. */ }
    }
    throw new Error(record.stopped || 'adapter_closed');
  }
  async waitForInput(record, pending, checkpoint) {
    const signature = JSON.stringify(pending);
    if (record.pending?.signature === signature && JSON.stringify(record.pending.checkpoint) === JSON.stringify(checkpoint)) return;
    if (record.pending) this.resolvePending(record, 'cancelled');
    const input = pending.length === 1 && pending[0].value?.type === 'input' && pending[0].value?.mode === 'text';
    if (!input) throw new Error('unsupported_interrupt_use_Studio');
    const id = randomUUID();
    record.pending = { id, signature, interrupts: pending, checkpoint, kind: 'input' };
    record.phase = 'waiting_input';
    const request = { id, taskId: record.task.id, status: 'pending', kind: 'input', mode: 'text',
      prompt: String(pending[0].value.prompt).slice(0, 4000), createdAt: Date.now() };
    record.pending.public = request;
    this.emit(record, 'backend.input.requested', { input: request });
  }

  resolvePending(record, status) {
    const pending = record.pending;
    if (!pending) return;
    const publicStatus = status === 'resolved' ? 'accepted' : status;
    if (pending.public) this.emit(record, 'backend.input.resolved', { input: { ...pending.public, status: publicStatus, resolvedAt: Date.now() } });
    record.pending = null;
  }
  async respondAuthorization(taskId, _authorizationId, _decision, { ownerId } = {}) {
    this.record(taskId, ownerId);
    throw new Error('backend_authorization_not_supported');
  }
  async respondInput(taskId, inputId, response, { ownerId } = {}) {
    const record = this.record(taskId, ownerId);
    const pending = record.pending;
    if (record.mutation || pending?.kind !== 'input' || pending.id !== inputId) throw new Error('input_not_pending');
    if (typeof response?.text !== 'string' || !response.text.trim() || response.text.length > 16000 || response.action || response.values) throw new Error('unsupported_input_response');
    record.mutation = (async () => {
      const latest = await this.client.latestRun(record.handle.threadId);
      const state = await this.client.state(record.handle);
      if (latest?.run_id !== record.handle.runId || JSON.stringify(interrupts(state)) !== pending.signature) throw new Error('stale_interrupt');
      record.handle = await this.client.resume(record.handle, taskId, state.checkpoint, pending.interrupts[0].id, response.text);
      this.resolvePending(record, 'accepted');
      record.phase = 'running';
      return { ...pending.public, status: 'accepted' };
    })();
    try { return await record.mutation; }
    finally { record.mutation = null; }
  }
  async cancel(taskId, { ownerId } = {}) {
    if (ownerId !== this.ownerId) throw new Error('task_not_found_for_owner');
    const record = this.active.get(taskId);
    if (!record) return { taskId, state: 'not_found' };
    if (!record.handle) record.handle = await record.submission;
    if (record.mutation) throw new Error('task_operation_pending');
    record.mutation = (async () => {
      await this.client.verifyThread(record.handle.threadId, taskId);
      const latest = await this.client.latestRun(record.handle.threadId);
      if (!latest) throw new Error('run_not_found');
      record.handle = { threadId: record.handle.threadId, runId: latest.run_id };
      if (['success', 'interrupted'].includes(latest.status)) {
        const state = await this.client.state(record.handle);
        if (interrupts(state).length) throw new Error('interrupt_requires_Studio_resolution');
        throw new Error('business_already_finished_query_Server_result');
      }
      if (['error', 'timeout'].includes(latest.status)) throw new Error('business_already_finished_query_Server_result');
      await this.client.cancel(record.handle);
      this.resolvePending(record, 'cancelled');
      record.stopped = 'business_cancelled';
      record.controller.abort();
      return { taskId, state: 'cancelled' };
    })();
    try { return await record.mutation; }
    finally { record.mutation = null; }
  }
  async close() {
    this.closed = true;
    for (const record of this.active.values()) { record.stopped = 'adapter_closed'; record.controller.abort(); }
    this.listeners.clear();
  }
}
