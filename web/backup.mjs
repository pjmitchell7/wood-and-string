// Pure preflight: no persistent storage changes occur here.
export async function preflightRestore(data, existing, budgetBytes) {
  const fail = message => { throw new Error(message); };
  if (!data || data.format !== 'wood-string-export' || data.version !== 1 || !Array.isArray(data.items) || data.items.length > 1000) fail('Unsupported library export.');
  if (!Array.isArray(data.queue) || data.queue.length > 10000 || data.queue.some(id => typeof id !== 'string')) fail('Invalid exported queue.');
  if (!Number.isFinite(budgetBytes) || budgetBytes < 10 * 1048576) fail('Set a download budget of at least 10 MB.');
  const ids = new Set(), prepared = [], unique = new Map();
  let decodedBytes = 0;
  for (const item of data.items) {
    const t = item?.track;
    if (!t || !/^[a-f0-9]{64}$/.test(t.sha256 || '') || !/^[a-zA-Z0-9-]{1,80}$/.test(t.id || '') || ids.has(t.id) || typeof t.name !== 'string' || !t.name.length || t.name.length > 1000 || !Number.isSafeInteger(t.size) || t.size < 1 || t.size > 100 * 1048576 || typeof t.type !== 'string' || !/^(audio\/[a-zA-Z0-9.+-]+|application\/octet-stream)$/.test(t.type) || typeof t.variant !== 'string' || t.variant.length > 200 || (t.source_id !== undefined && (typeof t.source_id !== 'string' || t.source_id.length > 200)) || (t.favorite !== undefined && typeof t.favorite !== 'boolean')) fail('Invalid track metadata. Nothing was restored.');
    if (typeof item.audio !== 'string' || item.audio.length > Math.ceil(t.size / 3) * 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(item.audio)) fail('Invalid audio encoding. Nothing was restored.');
    ids.add(t.id);
    decodedBytes += t.size;
    if (decodedBytes > 100 * 1048576) fail('Export exceeds the 100 MB restore limit.');
    const old = existing.find(x => x.id === t.id);
    if (old && old.sha256 !== t.sha256) fail('An asset identity conflicts with existing audio. Nothing was restored.');
    let bytes;
    try { bytes = Uint8Array.from(atob(item.audio), c => c.charCodeAt(0)); } catch { fail('Invalid audio encoding. Nothing was restored.'); }
    const digest = [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map(n => n.toString(16).padStart(2, '0')).join('');
    if (bytes.byteLength !== t.size || digest !== t.sha256) fail('Audio verification failed. Nothing was restored.');
    const track = { id: t.id, sha256: t.sha256, name: t.name, size: t.size, type: t.type, variant: t.variant, favorite: !!t.favorite, cached: true };
    if (t.source_id !== undefined) track.source_id = t.source_id;
    // A current, local authority URL is trusted; an imported URL is never used.
    if (old?.url) track.url = old.url;
    prepared.push({ track, bytes }); unique.set(track.sha256, track.size);
  }
  for (const t of existing) if (t.cached && !ids.has(t.id)) unique.set(t.sha256, t.size);
  if ([...unique.values()].reduce((n, size) => n + size, 0) > budgetBytes) fail('Restore exceeds this device’s download budget. Nothing was restored.');
  const validIds = new Set([...existing.map(t => t.id), ...ids]);
  if (data.queue.some(id => !validIds.has(id))) fail('The exported queue refers to missing music. Nothing was restored.');
  return { prepared, queue: [...data.queue] };
}
