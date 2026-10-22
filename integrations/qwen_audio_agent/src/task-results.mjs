import { createHash } from 'node:crypto';

/** Project native messages; never infer generated files from model prose or scan disk. */
export async function finalResult(state, client, handle) {
  const all = state.values?.messages || [];
  const lastInput = all.findLastIndex(m => ['human', 'user'].includes(m.type || m.role));
  const messages = all.slice(lastInput + 1);
  const last = messages.at(-1);
  const message = ['ai', 'assistant'].includes(last?.type || last?.role) && !last.tool_calls?.length ? last : null;
  const text = typeof message?.content === 'string' ? message.content :
    (Array.isArray(message?.content) ? message.content.filter(p => p.type === 'text').map(p => p.text).join('\n') : '');
  const threadRef = createHash('sha256').update(`${client.identity}\0${handle.threadId}`).digest('hex').slice(0, 32);
  const prefix = `/artifacts/${threadRef}/generated/`;
  const artifacts = [], seen = new Set();
  let failed = false;
  for (const m of messages) {
    if ((m.type || m.role) !== 'tool' || m.status === 'error') continue;
    const delivery = m.artifact?.assistant_agent_delivery_v1 ?? m.artifact?.structured_content?.assistant_agent_delivery_v1;
    if (!delivery) continue;
    if (!Array.isArray(delivery.output_refs) || delivery.output_refs.length > 4) { failed = true; continue; }
    for (const outputRef of delivery.output_refs) {
      const ref = typeof outputRef === 'string' && outputRef.startsWith('artifact://v1/')
        ? outputRef.replace('artifact://v1/', '/artifacts/') : outputRef;
      if (seen.has(ref)) continue;
      seen.add(ref);
      const filename = typeof ref === 'string' && ref.startsWith(prefix) ? ref.slice(prefix.length) : '';
      if (!/^[a-zA-Z0-9_-]+\.(png|jpe?g|gif|webp)$/i.test(filename) || artifacts.length >= 32) { failed = true; continue; }
      try {
        const mediaType = await client.checkArtifact(ref);
        artifacts.push({ artifactId: ref, name: filename,
          parts: [{ url: `${client.url}${ref}`, mediaType, filename }] });
      } catch { failed = true; }
    }
  }
  // Detail cards are additive; preserve the existing image and answer projection.
  const latestTools = new Map();
  for (const m of messages) if ((m.type || m.role) === 'tool' && m.name) latestTools.set(m.name, m);
  const details = new Set();
  let detailSize = 0;
  for (const m of latestTools.values()) {
    if (m.status === 'error') continue;
    const delivery = m.artifact?.assistant_agent_delivery_v1 ?? m.artifact?.structured_content?.assistant_agent_delivery_v1;
    const value = delivery?.text;
    if (typeof value !== 'string' || !value.startsWith('<detail>') || !value.endsWith('</detail>') || details.has(value)) continue;
    if (detailSize + value.length + details.size > 16000) { failed = true; continue; }
    details.add(value); detailSize += value.length;
  }
  if (details.size) {
    const value = [...details].join('\n');
    if (artifacts.length >= 32) failed = true;
    else artifacts.push({ artifactId: `detail:${createHash('sha256').update(value).digest('hex')}`,
      parts: [{ text: value, mediaType: 'text/plain' }] });
  }
  if (!text.trim() && !artifacts.length && !failed) throw Error('business_result_missing');
  return { content: failed ? '业务运行已结束，但部分产物暂时无法交付。不要自动重新生成。'
    : text.slice(0, 16000) || '产物已就绪。', artifacts,
    execution_status: 'completed', delivery_status: failed ? (artifacts.length ? 'partial' : 'failed') : 'ready' };
}
