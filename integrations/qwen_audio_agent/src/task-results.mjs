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
  const latestTools = new Map();
  for (const m of messages) if ((m.type || m.role) === 'tool' && m.name) latestTools.set(m.name, m);
  const seenTexts = new Set();
  let textChars = 0;
  let failed = false;
  for (const m of messages) {
    if ((m.type || m.role) !== 'tool' || m.status === 'error') continue;
    if (m.name && latestTools.get(m.name) !== m) continue;
    const delivery = m.artifact?.assistant_agent_delivery_v1 ?? m.artifact?.structured_content?.assistant_agent_delivery_v1;
    if (!delivery) continue;
    if (!Array.isArray(delivery.output_refs) || delivery.output_refs.length > 4) { failed = true; continue; }
    const deliveryText = delivery.text ?? '';
    if (typeof deliveryText !== 'string' || deliveryText.length > 16000) { failed = true; continue; }
    if (deliveryText.trim() && !seenTexts.has(deliveryText)) {
      if (textChars + deliveryText.length > 16000 || artifacts.length >= 32) failed = true;
      else {
        seenTexts.add(deliveryText);
        textChars += deliveryText.length;
        artifacts.push({ artifactId: `text:${createHash('sha256').update(deliveryText).digest('hex')}`,
          parts: [{ text: deliveryText, mediaType: 'text/plain' }] });
      }
    }
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
  if (!text.trim() && !artifacts.length && !failed) throw Error('business_result_missing');
  return { content: failed ? '业务运行已结束，但部分产物暂时无法交付。不要自动重新生成。'
    : text.slice(0, 16000) || '产物已就绪。', artifacts,
    execution_status: 'completed', delivery_status: failed ? (artifacts.length ? 'partial' : 'failed') : 'ready' };
}
