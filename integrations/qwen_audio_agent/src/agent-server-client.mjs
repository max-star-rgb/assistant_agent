import { createHash, randomUUID } from 'node:crypto';

export const BUSINESS_GRAPH = 'assistant-business-v2';

export class AgentServerError extends Error {
  constructor(code, status) { super(code); this.code = code; this.status = status; }
}

export function loopbackUrl(value) {
  const url = new URL(value);
  if (url.protocol !== 'http:' || !['127.0.0.1', '[::1]'].includes(url.hostname)
      || url.username || url.password || url.search || url.hash || url.pathname !== '/') {
    throw new Error('Agent Server requires a literal loopback HTTP origin');
  }
  return url.origin;
}

function threadId(namespace, identity, taskId) {
  const hash = createHash('sha256').update(JSON.stringify(['qwen-audio-v1', namespace, identity, taskId])).digest('hex');
  return `${hash.slice(0, 8)}-${hash.slice(8, 12)}-4${hash.slice(13, 16)}-a${hash.slice(17, 20)}-${hash.slice(20, 32)}`;
}

/** Native Agent Server HTTP only; no business execution or retrying mutations. */
export class AgentServerClient {
  constructor({ url, identity, namespace = 'default', requestTimeoutMs = 10000, fetchImpl = fetch }) {
    this.url = loopbackUrl(url);
    if (typeof identity !== 'string' || !identity.trim() || identity.length > 128 || /[\r\n]/.test(identity)) throw new Error('Invalid identity');
    Object.assign(this, { identity, namespace, requestTimeoutMs, fetchImpl });
  }

  async request(path, { method = 'GET', body } = {}) {
    let response;
    try {
      response = await this.fetchImpl(`${this.url}${path}`, {
        method, redirect: 'error', signal: AbortSignal.timeout(this.requestTimeoutMs),
        headers: { 'content-type': 'application/json', 'X-Assistant-User': this.identity },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      if (!response.ok) throw new AgentServerError('agent_server_http_error', response.status);
      const text = await response.text();
      return text ? JSON.parse(text) : null;
    } catch (error) {
      if (error instanceof AgentServerError) throw error;
      // Never include upstream bodies, URLs, credentials or task contents in errors.
      throw new AgentServerError('agent_server_connection_unknown');
    }
  }

  path(id) { return `/threads/${encodeURIComponent(id)}`; }

  async checkArtifact(path) {
    if (!/^\/artifacts\/[a-f0-9]{32}\/generated\/[a-zA-Z0-9_-]+\.(png|jpe?g|gif|webp)$/i.test(path)) throw Error('invalid_artifact');
    const response = await this.fetchImpl(`${this.url}${path}`, {
      redirect: 'error', signal: AbortSignal.timeout(this.requestTimeoutMs),
      headers: { 'X-Assistant-User': this.identity },
    });
    try {
      const type = response.headers.get('content-type')?.split(';')[0];
      if (!response.ok || !['image/png', 'image/jpeg', 'image/gif', 'image/webp'].includes(type)) throw Error('artifact_unavailable');
      return type;
    } finally { await response.body?.cancel(); }
  }

  async verifyThread(id, taskId) {
    const thread = await this.request(this.path(id));
    const metadata = thread.metadata || {};
    if (metadata.graph_id !== BUSINESS_GRAPH || metadata.owner !== this.identity || metadata.qwen_task_id !== taskId || metadata.qwen_namespace !== this.namespace) {
      throw new AgentServerError('thread_identity_mismatch');
    }
    return thread;
  }

  async latestRun(id) {
    const runs = await this.request(`${this.path(id)}/runs?limit=1`);
    return runs[0] || null;
  }

  async submit(task) {
    const id = threadId(this.namespace, this.identity, task.id);
    const fingerprint = createHash('sha256').update(task.instruction).digest('hex');
    let exists = true;
    try { await this.verifyThread(id, task.id); }
    catch (error) { if (error.status === 404) exists = false; else throw error; }
    if (!exists) {
      try {
        await this.request('/threads', { method: 'POST', body: { thread_id: id, if_exists: 'raise',
          metadata: { graph_id: BUSINESS_GRAPH, qwen_task_id: task.id, qwen_namespace: this.namespace, qwen_input_hash: fingerprint } } });
      } catch (error) { if (error.status === 409) exists = true; else throw error; }
    }
    const thread = await this.verifyThread(id, task.id);
    if (thread.metadata.qwen_input_hash !== fingerprint) throw new AgentServerError('task_input_mismatch');
    if (exists) {
      const run = await this.latestRun(id);
      // An empty pre-existing thread may have an in-flight or lost run submission.
      if (!run) throw new AgentServerError('submission_unknown');
      return { threadId: id, runId: run.run_id };
    }
    return this.createRun(id, task.id, { input: {
      messages: [{ role: 'user', name: 'voice_agent', content: task.instruction }],
    } });
  }

  async createRun(id, taskId, payload) {
    const submissionId = randomUUID();
    try {
      const run = await this.request(`${this.path(id)}/runs`, { method: 'POST', body: {
        ...payload, assistant_id: BUSINESS_GRAPH, multitask_strategy: 'reject',
        context: { enable_memory: false, require_tool_approval: false },
        metadata: { qwen_task_id: taskId, qwen_submission_id: submissionId },
      } });
      return { threadId: id, runId: run.run_id };
    } catch (error) {
      if (error.status >= 400 && error.status < 500 && error.status !== 408) throw error;
      try {
        const run = await this.latestRun(id);
        if (run?.metadata?.qwen_submission_id === submissionId) return { threadId: id, runId: run.run_id };
      } catch { /* The outcome remains unknown; never replay a mutation. */ }
      throw new AgentServerError('submission_unknown');
    }
  }

  run(handle) { return this.request(`${this.path(handle.threadId)}/runs/${encodeURIComponent(handle.runId)}`); }
  state(handle) { return this.request(`${this.path(handle.threadId)}/state`); }
  cancel(handle) { return this.request(`${this.path(handle.threadId)}/runs/${encodeURIComponent(handle.runId)}/cancel?wait=1&action=interrupt`, { method: 'POST' }); }
  resume(handle, taskId, checkpoint, interruptId, answer) {
    return this.createRun(handle.threadId, taskId, { checkpoint, command: { resume: { [interruptId]: answer } } });
  }

  async lookup(taskId) {
    const id = threadId(this.namespace, this.identity, taskId);
    await this.verifyThread(id, taskId);
    return { threadId: id, run: await this.latestRun(id) };
  }
}
