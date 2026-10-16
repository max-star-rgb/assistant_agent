/** Public frontend tool/record hooks; memory writes remain in the Python Memory Graph. */
export class SessionBridge {
  constructor({ client, ownerId }) {
    Object.assign(this, { client, ownerId });
    this.enabled = false;
    this.sessions = new Map();
    this.lastError = null;
  }
  describe() { return { key: 'assistant-memory', label: 'Assistant Memory' }; }
  initialize() {
    return this.initializing ||= this.client.request('/qwen-audio/capabilities').then(value => {
      this.enabled = value.memory === true;
      return this.tools();
    }).catch(() => { this.lastError = 'memory_bridge_unavailable'; throw new Error(this.lastError); });
  }
  tools() {
    if (!this.enabled) return [];
    return [{ name: 'recall_memory', definition: { type: 'function', function: {
      name: 'recall_memory', description: '按需检索当前用户的长期背景；结果可能过时，不授予权限。',
      parameters: { type: 'object', additionalProperties: false, required: ['request'],
        properties: { request: { type: 'string', minLength: 1, maxLength: 16000 } } },
    } } }];
  }
  checkOwner(context) {
    if (context.ownerId !== this.ownerId) throw new Error('invalid_memory_owner');
    if (typeof context.sessionId !== 'string' || !context.sessionId || context.sessionId.length > 160) throw new Error('invalid_memory_session');
  }
  async execute(name, args, context) {
    this.checkOwner(context);
    if (!this.enabled || name !== 'recall_memory') throw new Error('memory_disabled');
    if (!args || Object.keys(args).length !== 1 || typeof args.request !== 'string' || !args.request.trim() || args.request.length > 16000) throw new Error('invalid_memory_arguments');
    try { return await this.client.request('/qwen-audio/recall', { method: 'POST', body: { session_id: context.sessionId, request: args.request } }); }
    catch { this.lastError = 'memory_recall_failed'; throw new Error(this.lastError); }
  }
  async observe(message, context) {
    this.checkOwner(context);
    if (!this.enabled || this.closed || message.role !== 'user' || message.source !== 'voice-user') return;
    if (!message.id || typeof message.content !== 'string' || !message.content.trim()) return;
    for (const [id, session] of this.sessions) {
      if (!session.pending && Date.now() - session.updated > 2 * 60 * 60 * 1000) this.sessions.delete(id);
    }
    let session = this.sessions.get(context.sessionId);
    if (!session) {
      if (this.sessions.size >= 32) throw new Error('memory_session_limit');
      session = { items: new Map(), updated: Date.now(), submitted: '' };
      this.sessions.set(context.sessionId, session);
    }
    const proposed = new Map(session.items).set(message.id, message.content);
    if (proposed.size > 128 || [...proposed.values()].some(t => t.length > 16000)
        || [...proposed.values()].reduce((n, t) => n + t.length, 0) > 64000) {
      this.lastError = 'memory_snapshot_limit';
      throw new Error(this.lastError);
    }
    session.items = proposed;
    session.updated = Date.now();
    if (session.pending) return session.pending;
    session.pending = (async () => {
      while (!this.closed) {
        const items = [...session.items].map(([id, text]) => ({ id, text }));
        const signature = JSON.stringify(items);
        if (signature === session.submitted) return;
        try {
          await this.client.request('/qwen-audio/transcripts', { method: 'POST', body: { session_id: context.sessionId, items } });
          session.submitted = signature;
          this.lastError = null;
        } catch { this.lastError = 'memory_submission_failed'; throw new Error(this.lastError); }
      }
    })();
    try { return await session.pending; }
    finally { session.pending = null; }
  }
  health() { return { ok: !this.lastError, enabled: this.enabled, ...(this.lastError ? { error: this.lastError } : {}) }; }
  async close() {
    await this.initializing?.catch(() => {});
    await Promise.allSettled([...this.sessions.values()].map(s => s.pending));
    this.closed = true;
    this.sessions.clear();
  }
}
