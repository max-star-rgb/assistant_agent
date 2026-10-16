import { pathToFileURL } from 'node:url';
import { AgentServerClient } from './agent-server-client.mjs';
import { loadConfig, instanceNamespace } from './config.mjs';

export async function taskStatus(client, taskId) {
  if (typeof taskId !== 'string' || !taskId.trim() || taskId.length > 128) throw new Error('task id required');
  const { threadId, run } = await client.lookup(taskId);
  return { task_id: taskId, thread_id: threadId, run_id: run?.run_id || null, status: run?.status || 'submission_unknown' };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const config = loadConfig({ ...process.env, MULTIMODAL_AGENT_PROVIDER_MODE: 'mock' });
    const result = await taskStatus(new AgentServerClient({
      url: config.url, identity: config.identity, namespace: await instanceNamespace(config.stateDirectory),
    }), process.argv[2]);
    console.log(JSON.stringify(result, null, 2));
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}
