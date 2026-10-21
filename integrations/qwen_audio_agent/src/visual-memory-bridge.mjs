/** Poll local snapshots only; remote recall follows initialisation and upload count. */
export class VisualMemoryBridge {
  constructor({ client, ownerId }) {
    Object.assign(this, { client, ownerId });
    this.snapshot = [];
    this.status = 'pending';
    this.listeners = new Set();
    this.requestVersion = 0;
  }
  describe() { return { key: 'visual-memory', label: 'Visual background' }; }
  initialize() { return this.tools(); }
  execute() { throw Error('tool_unavailable'); }
  health() { return { ok: !this.closed && !this.lastError }; }
  tools() { return []; }
  visualMemorySnapshot(ownerId) { return !this.closed && ownerId === this.ownerId ? this.snapshot : []; }
  visualMemoryStatus(ownerId) { return !this.closed && ownerId === this.ownerId ? this.status : undefined; }
  update(texts, status) {
    const changed = status !== this.status || JSON.stringify(texts) !== JSON.stringify(this.snapshot);
    this.snapshot = texts;
    this.status = status;
    if (changed) for (const listener of this.listeners) listener();
  }
  subscribeVisualMemory(listener) {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
      if (!this.listeners.size) clearTimeout(this.timer);
    };
  }
  async beginVisualMemorySession({ ownerId }) {
    if (ownerId !== this.ownerId || this.closed) return;
    await this.poll('POST');
    this.schedule();
  }
  schedule() {
    clearTimeout(this.timer);
    if (!this.closed && this.listeners.size) {
      this.timer = setTimeout(async () => { await this.poll(); this.schedule(); }, 2000);
      this.timer.unref?.();
    }
  }
  async poll(method = 'GET') {
    if (this.closed) return;
    const version = ++this.requestVersion;
    try {
      const result = await this.client.request('/qwen-audio/visual-memory/snapshot', { method });
      if (this.closed || version !== this.requestVersion) return;
      if (!Array.isArray(result.texts) || result.texts.some(text => typeof text !== 'string')) throw Error('invalid_snapshot');
      let remaining = 12000;
      const texts = result.enabled === true ? result.texts.slice(0, 32).map(text => {
        const bounded = text.slice(0, Math.min(4000, remaining));
        remaining -= bounded.length;
        return bounded;
      }).filter(Boolean) : [];
      const status = result.enabled !== true ? 'disabled'
        : ['empty', 'refreshing', 'unavailable'].includes(result.status) ? result.status
        : texts.length ? 'ready' : 'ready_empty';
      this.lastError = status === 'unavailable' ? 'visual_snapshot_unavailable' : null;
      this.update(texts, status);
    } catch {
      if (this.closed || version !== this.requestVersion) return;
      this.lastError = 'visual_snapshot_unavailable';
      this.update(this.snapshot, 'unavailable');
    }
  }
  async close() {
    this.closed = true;
    clearTimeout(this.timer);
    this.snapshot = [];
    this.listeners.clear();
  }
}
