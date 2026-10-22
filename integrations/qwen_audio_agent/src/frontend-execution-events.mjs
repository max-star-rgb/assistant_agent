/** Backend-neutral execution facts. No exporter, I/O, audio or provider payloads. */
export function configurationFields(value = {}) {
  if (value.observation_omitted === 'configuration_size_limit') return { observation_omitted: value.observation_omitted };
  const selected = Object.fromEntries(['instructions', 'tools', 'tool_choice', 'temperature',
    'max_output_tokens', 'max_response_output_tokens', 'modalities', 'voice', 'turn_detection']
    .filter(key => value[key] !== undefined).map(key => [key, value[key]]));
  const json = JSON.stringify(selected);
  return json.length <= 1024 * 1024 ? JSON.parse(json) : { observation_omitted: 'configuration_size_limit' };
}

// Called only after the transport accepts a message; bounded observation, never replayed.
export function recordSentConfiguration(frontend, body) {
  if (body.type === 'conversation.item.create' && body.item?.type === 'message'
    && frontend.conversationItemWaiters?.get(body.item.id)?.contextOnly) {
    const text = (body.item.content || []).filter(part => ['input_text', 'text'].includes(part.type))
      .map(part => part.text || '').join('\n');
    if (text) frontend.onEvent?.({ type: 'observation.context.sent', itemId: body.item.id,
      role: body.item.role, text: text.slice(0, 16000) });
  }
  if (body.type === 'conversation.item.create' && body.item?.type === 'function_call_output') {
    const { call_id, output } = body.item;
    frontend.observedToolResults = [...(frontend.observedToolResults || []).filter(item => item.call_id !== call_id),
      { call_id, output: typeof output === 'string' && output.length <= 16000 ? output : '[omitted: tool result size limit]' }].slice(-64);
  }
  if (body.type === 'session.update') frontend.observedSessionConfiguration = {
    ...frontend.observedSessionConfiguration, ...configurationFields(body.session),
  };
  if (body.type === 'response.create') {
    const pending = frontend.pendingResponses.at(-1);
    if (pending) pending.observedConfiguration = {
      session: configurationFields(frontend.observedSessionConfiguration),
      response: configurationFields(body.response),
    };
  }
}

export function publicExecutionEvent(event) {
  if (event.type === 'turn.started') return { kind: 'turn.start', turnId: event.turnId };
  if (event.type === 'transcript.final' && event.role === 'user') return { kind: 'input', turnId: event.turnId, text: event.content };
  if (event.type === 'tool.call' && event.status === 'received') return {
    kind: 'tool.start', turnId: event.turnId, responseId: event.responseId,
    callId: event.callId, name: event.name, input: event.arguments,
  };
  return null;
}

export function modelExecutionEvent(event, context) {
  const type = event.type;
  if (type === 'observation.context.sent') return { kind: 'history.message', itemId: event.itemId, role: event.role, text: event.text };
  if (type === 'session.created') return { kind: 'session.start', providerSessionId: event.session?.id };
  if (type === 'conversation.item.deleted' || type === 'conversation.item.truncated') {
    return { kind: 'history.remove', itemId: event.item_id };
  }
  const responseId = event.response_id || event.response?.id;
  if (!responseId) return null;
  const common = { responseId, turnId: context?.origin === 'announcement'
    ? `announcement:${responseId}` : context?.turnId || `response:${responseId}`,
    origin: context?.origin, taskId: context?.taskId,
    ...(Array.isArray(context?.taskIds) ? { taskIds: context.taskIds.slice(0, 8) } : {}) };
  if (type === 'response.created') return { ...common, kind: 'model.start', configuration: event.__executionConfiguration,
    toolResults: event.__executionToolResults };
  if (['response.text.delta', 'response.audio_transcript.delta', 'response.output_audio_transcript.delta'].includes(type)) {
    return { ...common, kind: 'model.delta', text: event.delta };
  }
  if (['response.text.done', 'response.audio_transcript.done', 'response.output_audio_transcript.done'].includes(type)) {
    return { ...common, kind: 'model.text', text: event.text || event.transcript };
  }
  if (type !== 'response.done') return null;
  const output = event.response?.output || [];
  const usage = event.response?.usage;
  return { ...common, kind: 'model.end', status: event.response?.status,
    itemIds: output.map(item => item.id).filter(Boolean),
    hasTools: context?.awaitsToolFollowUp ?? (context?.hasFunctionCall || output.some(item => item.type === 'function_call')),
    text: output.flatMap(item => item.content || []).map(part => part.text || part.transcript || '').join(''),
    ...(usage ? { usage: Object.fromEntries(['input_tokens', 'output_tokens', 'total_tokens']
      .filter(key => Number.isFinite(usage[key])).map(key => [key, usage[key]])) } : {}),
  };
}
