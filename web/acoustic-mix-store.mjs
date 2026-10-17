// Local playback choices only. No audio bytes or arrangement changes are stored.
export const VERSION = 'acoustic-playback-mix-v1';
const PREFIX = VERSION + ':';
const MAX_RECORD_BYTES = 1024;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SHA = /^[0-9a-f]{64}$/;
const queuesByDatabase = new WeakMap();

function need(value, message) {
  if (!value) throw Error(message);
}

function object(value, keys) {
  need(value !== null && typeof value === 'object' && !Array.isArray(value) &&
    [Object.prototype, null].includes(Object.getPrototypeOf(value)), 'Invalid saved playback mix.');
  const names = Reflect.ownKeys(value);
  need(names.length === keys.length && names.every(name => typeof name === 'string' && keys.includes(name)),
    'Saved playback mix fields changed.');
  need(names.every(name => Object.hasOwn(Object.getOwnPropertyDescriptor(value, name), 'value')),
    'Saved playback mix must contain plain values.');
}

function ids(value, roster, {complete = false} = {}) {
  need(Array.isArray(value) && value.length <= 2, 'Invalid saved player selection.');
  const keys = Reflect.ownKeys(value);
  need(keys.length === value.length + 1 && keys.every(key => key === 'length' ||
    (typeof key === 'string' && /^(0|1)$/.test(key) && Number(key) < value.length)),
    'Saved player selection must be a complete plain list.');
  const seen = new Set();
  for (let index = 0; index < value.length; index++) {
    const entry = Object.getOwnPropertyDescriptor(value, String(index));
    need(entry && Object.hasOwn(entry, 'value') && typeof entry.value === 'string' &&
      roster.includes(entry.value) && !seen.has(entry.value), 'Unknown or duplicate saved player.');
    seen.add(entry.value);
  }
  need(!complete || (value.length === roster.length && value.every((id, index) => id === roster[index])),
    'Saved player roster changed.');
  return roster.filter(id => seen.has(id));
}

function identity(value) {
  object(value, ['version', 'job_id', 'bundle_id', 'stem_ids']);
  need(value.version === VERSION && typeof value.job_id === 'string' && UUID.test(value.job_id) &&
    typeof value.bundle_id === 'string' && SHA.test(value.bundle_id), 'Invalid saved preview identity.');
  need(Array.isArray(value.stem_ids) && value.stem_ids.length >= 1 && value.stem_ids.length <= 2,
    'Saved playback requires one or two players.');
  const roster = value.stem_ids.length === 1 ? ['performer-1'] : ['performer-1', 'performer-2'];
  return {version: VERSION, job_id: value.job_id, bundle_id: value.bundle_id,
    stem_ids: ids(value.stem_ids, roster, {complete: true})};
}

export function mixIdentity(trusted) {
  need(trusted && typeof trusted === 'object' && trusted.reference && Array.isArray(trusted.roles),
    'Verified preview context required.');
  return identity({version: VERSION, job_id: trusted.jobId, bundle_id: trusted.reference.bundle_id,
    stem_ids: trusted.roles.map(role => role?.id)});
}

function keyFor(value) {
  return PREFIX + value.job_id + ':' + value.bundle_id;
}

function recordFor(value, mutedIds) {
  const record = {...value, stem_ids: [...value.stem_ids], muted_ids: ids(mutedIds, value.stem_ids)};
  need(new TextEncoder().encode(JSON.stringify(record)).byteLength <= MAX_RECORD_BYTES,
    'Saved playback mix exceeds its local limit.');
  return record;
}

function readRecord(value, wanted) {
  object(value, ['version', 'job_id', 'bundle_id', 'stem_ids', 'muted_ids']);
  const actual = identity({version: value.version, job_id: value.job_id, bundle_id: value.bundle_id,
    stem_ids: value.stem_ids});
  need(actual.job_id === wanted.job_id && actual.bundle_id === wanted.bundle_id &&
    actual.stem_ids.length === wanted.stem_ids.length &&
    actual.stem_ids.every((id, index) => id === wanted.stem_ids[index]),
    'Saved playback mix belongs to another preview.');
  const checked = recordFor(actual, value.muted_ids);
  need(value.muted_ids.every((id, index) => id === checked.muted_ids[index]),
    'Saved playback mix order changed.');
  return [...checked.muted_ids];
}

function failure(source, fallback) {
  return source?.error instanceof Error ? source.error : Error(fallback);
}

function write(db, key, record) {
  return new Promise((resolve, reject) => {
    let transaction, succeeded = false;
    try {
      transaction = db.transaction('settings', 'readwrite');
      transaction.oncomplete = () => succeeded ? resolve() : reject(Error('Playback mix was not saved.'));
      transaction.onerror = () => reject(failure(transaction, 'Playback mix could not be saved.'));
      transaction.onabort = () => reject(failure(transaction, 'Playback mix save was cancelled.'));
      const request = transaction.objectStore('settings').put(record, key);
      request.onsuccess = () => { succeeded = true; };
      request.onerror = () => reject(failure(request, 'Playback mix could not be saved.'));
    } catch (error) {
      if (transaction) { try { transaction.abort(); } catch {} }
      reject(error);
    }
  });
}

function read(db, key, wanted) {
  return new Promise((resolve, reject) => {
    let transaction, value, count, gotValue = false, gotCount = false;
    try {
      transaction = db.transaction('settings', 'readonly');
      transaction.oncomplete = () => {
        try {
          need(gotValue && gotCount && Number.isInteger(count) && count >= 0 && count <= 1,
            'Playback mix lookup did not finish.');
          if (count === 0) { need(value === undefined, 'Playback mix lookup changed.'); resolve(null); }
          else resolve(readRecord(value, wanted));
        } catch (error) { reject(error); }
      };
      transaction.onerror = () => reject(failure(transaction, 'Saved playback mix could not be loaded.'));
      transaction.onabort = () => reject(failure(transaction, 'Playback mix load was cancelled.'));
      const settings = transaction.objectStore('settings');
      const get = settings.get(key), exists = settings.count(key);
      get.onsuccess = () => { value = get.result; gotValue = true; };
      exists.onsuccess = () => { count = exists.result; gotCount = true; };
      get.onerror = () => reject(failure(get, 'Saved playback mix could not be loaded.'));
      exists.onerror = () => reject(failure(exists, 'Saved playback mix could not be located.'));
    } catch (error) {
      if (transaction) { try { transaction.abort(); } catch {} }
      reject(error);
    }
  });
}

export function createIndexedMixStore(db) {
  need(db && typeof db === 'object' && typeof db.transaction === 'function',
    'The local settings database is unavailable.');
  let queues = queuesByDatabase.get(db);
  if (!queues) { queues = new Map(); queuesByDatabase.set(db, queues); }
  return {
    async load(supplied) {
      const wanted = identity(supplied), key = keyFor(wanted), pending = queues.get(key);
      if (pending) await pending;
      return read(db, key, wanted);
    },
    async save(supplied, mutedIds) {
      // Snapshot before the first await, even when an older write is pending.
      const wanted = identity(supplied), key = keyFor(wanted), record = recordFor(wanted, mutedIds);
      const before = queues.get(key);
      const pending = (before ? before.catch(() => {}) : Promise.resolve()).then(() => write(db, key, record));
      queues.set(key, pending);
      try { await pending; }
      finally { if (queues.get(key) === pending) queues.delete(key); }
    }
  };
}
