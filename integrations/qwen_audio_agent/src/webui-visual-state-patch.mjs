import { readFile, readdir, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';

const before = '客户端已停止实时视觉输入，当前无法看到新的画面。之前收到的画面仅代表历史，不代表当前环境。';
const after = '客户端当前没有提供实时画面，不能描述当前环境。这不表示此前曾开启摄像头或收到过画面；如确有历史画面，也不代表当前环境。这只是能力状态，不是用户请求，无需主动向用户播报。';

// shortcut: 2.0.1 emits the disabled camera state even before first use.
// Remove this compatibility patch when upstream distinguishes initial/off states.
export async function patchWebuiVisualState() {
  const root = fileURLToPath(new URL('../../../', import.meta.resolve('qwen-audio-agent/gateway-application')));
  const { version } = JSON.parse(await readFile(join(root, 'package.json'), 'utf8'));
  if (version !== '2.0.1') throw new Error('review_visual_state_patch_for_new_upstream');
  const assets = join(root, 'web/dist/assets');
  const candidates = (await readdir(assets)).filter(name => /^index-.*\.js$/.test(name));
  const matches = [];
  for (const name of candidates) {
    const path = join(assets, name);
    const text = await readFile(path, 'utf8');
    if (text.includes(before) || text.includes(after)) matches.push({ path, text });
  }
  if (matches.length !== 1) throw new Error('webui_visual_state_patch_target_changed');
  const { path, text } = matches[0];
  if (text.includes(after) && !text.includes(before)) return;
  if (text.split(before).length !== 2 || text.includes(after)) throw new Error('webui_visual_state_patch_ambiguous');
  await writeFile(path, text.replace(before, after));
}
