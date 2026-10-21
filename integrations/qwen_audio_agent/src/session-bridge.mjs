/** Authenticated search; text memory belongs to the native runtime. */
const SEARCH_TOOL = 'mcp_bailian_bailian_web_search';
export class SessionBridge {
  constructor({ client, ownerId }) { Object.assign(this, { client, ownerId }); }
  describe() { return { key: 'assistant-voice', label: 'Assistant Voice Tools' }; }
  initialize() {
    return this.initializing ||= this.client.request('/qwen-audio/capabilities').then(value => {
      this.searchTool = value.search_tool?.function?.name === SEARCH_TOOL ? value.search_tool : null;
      return this.tools();
    }).catch(() => { this.lastError = 'voice_bridge_unavailable'; throw Error(this.lastError); });
  }
  tools() {
    return this.searchTool ? [{ name: SEARCH_TOOL, definition: this.searchTool }] : [];
  }
  async execute(name, args, context) {
    if (this.closed) throw Error('voice_bridge_closed');
    if (context.ownerId !== this.ownerId) throw Error('invalid_owner');
    if (typeof context.sessionId !== 'string' || !context.sessionId || context.sessionId.length > 160) throw Error('invalid_session');
    if (name === SEARCH_TOOL && this.searchTool) {
      if (!args || typeof args !== 'object' || Array.isArray(args)) throw Error('invalid_search_arguments');
      try { return await this.client.request('/qwen-audio/search', { method: 'POST', body: { session_id: context.sessionId, arguments: args } }); }
      catch { throw Error('bailian_search_failed'); }
    }
    throw Error('tool_unavailable');
  }
  health() { return { ok: !this.closed && !this.lastError, ...(this.lastError ? { error: this.lastError } : {}) }; }
  async close() { this.closed = true; await this.initializing?.catch(() => {}); }
}
