import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';

// Upstream 2.0.1 assumes one client per owner; retain leases within each session.
export async function patchFrontendConcurrency() {
  const root = fileURLToPath(new URL('../../../', import.meta.resolve('qwen-audio-agent/gateway-application')));
  const { version } = JSON.parse(await readFile(join(root, 'package.json'), 'utf8'));
  if (version !== '2.0.1') throw Error('review_frontend_concurrency_patch_for_new_upstream');
  const changes = [
    ['server/src/transport/gateway-client-transport.mjs', [
      ["    const replayKey = `${ownerId}\\u0000${sessionId}`", "    const replayKey = `${ownerId}\\u0000${sessionId}`\n    const occupancyKey = replayKey"],
      ['  const broadcastVoiceOwnership = ownerId => {\n    const active = activeVoiceClients.active(ownerId)',
        '  const broadcastVoiceOwnership = (ownerId, occupancyKey) => {\n    const active = activeVoiceClients.active(occupancyKey)'],
      ["    for (const client of voiceConnections.get(ownerId) || []) {\n      send(client.ws, {", "    for (const client of voiceConnections.get(ownerId) || []) {\n      if (client.occupancyKey !== occupancyKey) continue\n      send(client.ws, {"],
      ['    const voiceClient = {\n      ws,', '    const voiceClient = {\n      occupancyKey,\n      ws,'],
      ['activeVoiceClients.isActive(ownerId, voiceClient)', 'activeVoiceClients.isActive(occupancyKey, voiceClient)'],
      ['activeVoiceClients.activate(ownerId, voiceClient, {', 'activeVoiceClients.activate(occupancyKey, voiceClient, {'],
      ['activeVoiceClients.release(ownerId, voiceClient)', 'activeVoiceClients.release(occupancyKey, voiceClient)'],
      ['broadcastVoiceOwnership(ownerId)', 'broadcastVoiceOwnership(ownerId, occupancyKey)'],
      ['activeClientLeases.claim(ownerId, leaseParticipant, {', 'activeClientLeases.claim(occupancyKey, leaseParticipant, {'],
      ['activeClientLeases.isActive(\n          ownerId,', 'activeClientLeases.isActive(\n          occupancyKey,', 2],
      ['activeClientLeases.release(\n        ownerId,', 'activeClientLeases.release(\n        occupancyKey,'],
    ]],
    ['server/src/orchestration/session-task-coordinator.mjs', [
      ['includeOtherSessions = !taskIds?.length', 'includeOtherSessions = false'],
      ['if (this.closed || event.ownerId !== this.ownerId) return',
        'if (this.closed || event.ownerId !== this.ownerId || event.task?.sessionId !== this.sessionId) return'],
    ]],
  ];
  // Validate every source before writing any patch.
  const outputs = [];
  for (const [relative, replacements] of changes) {
    const path = join(root, relative);
    const original = await readFile(path, 'utf8');
    let text = original;
    for (const [before, after, count = 1] of replacements) {
      if (text.split(after).length === count + 1) continue;
      if (text.split(before).length !== count + 1) throw Error('frontend_concurrency_patch_target_changed');
      text = text.replaceAll(before, after);
    }
    if (text !== original) outputs.push([path, text]);
  }
  for (const [path, text] of outputs) await writeFile(path, text);
}
