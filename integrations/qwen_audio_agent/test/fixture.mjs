import { createServer } from 'node:http';
import { once } from 'node:events';

// A protocol fixture, not a substitute for real Agent Server acceptance testing.
export async function serverFixture({ hold = false, interrupt = null, dropSubmit = false, memory = false } = {}) {
  const threads = new Map();
  const requests = [];
  let accepted;
  const started = new Promise(resolve => { accepted = resolve; });
  const server = createServer(async (req, res) => {
    let raw = '';
    for await (const chunk of req) raw += chunk;
    const body = raw ? JSON.parse(raw) : null;
    requests.push({ method: req.method, path: req.url, body, user: req.headers['x-assistant-user'] });
    const parts = new URL(req.url, 'http://localhost').pathname.split('/').filter(Boolean);
    const json = (value, status = 200) => { res.writeHead(status, { 'content-type': 'application/json' }); res.end(JSON.stringify(value)); };
    if (parts[0] === 'info') return json({ version: 'fixture' });
    if (parts[0] === 'qwen-audio' && parts[1] === 'capabilities') return json({ memory });
    if (parts[0] === 'qwen-audio' && parts[1] === 'transcripts') return json({ status: 'submitted' });
    if (parts.length === 1 && parts[0] === 'threads' && req.method === 'POST') {
      if (!threads.has(body.thread_id)) threads.set(body.thread_id, {
        thread_id: body.thread_id, metadata: { ...body.metadata, owner: req.headers['x-assistant-user'] }, runs: [], interrupt,
      });
      const thread = threads.get(body.thread_id);
      return json({ thread_id: thread.thread_id, metadata: thread.metadata });
    }
    const thread = threads.get(parts[1]);
    if (!thread) return json({ detail: 'not found' }, 404);
    if (parts.length === 2) return json({ thread_id: thread.thread_id, metadata: thread.metadata });
    if (parts[2] === 'state') return json({
      checkpoint: { checkpoint_id: `cp-${thread.runs.length}` },
      tasks: thread.interrupt ? [{ interrupts: [thread.interrupt] }] : [],
      values: { messages: [{ type: 'ai', content: '业务完成', tool_calls: [] }] },
    });
    if (parts[2] === 'runs' && parts.length === 3) {
      if (req.method === 'GET') return json([...thread.runs].reverse());
      if (body.command) thread.interrupt = null;
      const run = { run_id: `run-${thread.runs.length + 1}`, thread_id: thread.thread_id,
        metadata: body.metadata, status: thread.interrupt ? 'success' : hold ? 'running' : 'success' };
      thread.runs.push(run);
      accepted();
      if (dropSubmit) { req.socket.destroy(); return; }
      return json(run);
    }
    const run = thread.runs.find(run => run.run_id === parts[3]);
    if (!run) return json({}, 404);
    if (parts[4] === 'cancel') { run.status = 'interrupted'; thread.interrupt = null; return json(null); }
    return json(run);
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  return { url: `http://127.0.0.1:${server.address().port}`, threads, requests, started,
    close: async () => { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); } };
}

export const work = { id: 'task-one', ownerId: 'user_personal', instruction: '整理旅行计划' };
