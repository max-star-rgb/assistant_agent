/** Read declared backend images for the media wire; never execute or regenerate. */
export async function readMediaImage(part, { backendUrl, identity, signal }) {
  const url = new URL(part.url);
  if (url.origin !== new URL(backendUrl).origin || url.username || url.password || url.search || url.hash
      || !/^\/artifacts\/[a-f0-9]{32}\/generated\/[a-zA-Z0-9_-]+\.(png|jpe?g|gif|webp)$/i.test(url.pathname)) {
    throw Error('invalid_media_artifact');
  }
  const response = await fetch(url, { headers: { 'X-Assistant-User': identity }, redirect: 'error',
    signal: AbortSignal.any([signal, AbortSignal.timeout(10000)]) });
  const maxBytes = 25 * 1024 * 1024;
  const types = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp']);
  if (!response.ok || !types.has(response.headers.get('content-type')?.split(';')[0])
      || Number(response.headers.get('content-length')) > maxBytes) {
    await response.body?.cancel(); throw Error('media_artifact_unavailable');
  }
  const chunks = []; let size = 0;
  for await (const chunk of response.body) {
    size += chunk.length;
    if (size > maxBytes) throw Error('media_artifact_too_large');
    chunks.push(chunk);
  }
  const bytes = Buffer.concat(chunks);
  const valid = bytes.subarray(0, 8).equals(Buffer.from('89504e470d0a1a0a', 'hex'))
    || bytes.subarray(0, 3).equals(Buffer.from('ffd8ff', 'hex'))
    || ['GIF87a', 'GIF89a'].includes(bytes.subarray(0, 6).toString())
    || (bytes.subarray(0, 4).toString() === 'RIFF' && bytes.subarray(8, 12).toString() === 'WEBP');
  if (!valid) throw Error('invalid_media_image');
  return { type: 'IMAGE', imageId: url.pathname.split('/').at(-1).replace(/\.[^.]+$/, ''), image: bytes.toString('base64') };
}
