import assert from 'node:assert/strict';
import test from 'node:test';
import {VERSION, mixIdentity, createIndexedMixStore} from '../web/acoustic-mix-store.mjs';

const JOB = '12345678-1234-1234-1234-123456789abc';
const OTHER_JOB = '12345678-1234-1234-1234-123456789abd';
const SHA = 'a'.repeat(64), OTHER_SHA = 'b'.repeat(64);
const clone = value => structuredClone(value);
const trusted = (players = 2) => ({jobId: JOB, reference: {bundle_id: SHA},
  roles: Array.from({length: players}, (_, index) => ({id: `performer-${index + 1}`}))});
const identity = () => mixIdentity(trusted());
const tick = async () => { for (let index = 0; index < 8; index++) await Promise.resolve(); };

// No IndexedDB installation, disk, DOM or AudioContext is used. Requests can
// succeed before their transaction commits, aborts or reports an error.
class Database {
  constructor({auto = true} = {}) {
    this.auto = auto; this.data = new Map(); this.transactions = []; this.closed = false;
  }
  transaction(name, mode) {
    assert.equal(name, 'settings'); assert.ok(['readonly', 'readwrite'].includes(mode));
    if (this.closed) throw Error('database closed');
    const db = this;
    const transaction = {mode, requests: [], done: false, error: null,
      objectStore(name) {
        assert.equal(name, 'settings');
        const request = (kind, key, value) => {
          assert.equal(typeof key, 'string');
          const operation = {kind, key, value: clone(value), result: undefined, error: null};
          transaction.requests.push(operation); return operation;
        };
        return {put: (value, key) => request('put', key, value),
          get: key => request('get', key), count: key => request('count', key)};
      },
      abort() { queueMicrotask(() => transaction.finish('abort')); },
      succeedRequests() {
        for (const request of transaction.requests) {
          if (request.delivered) continue;
          request.result = request.kind === 'get' ? clone(db.data.get(request.key)) :
            request.kind === 'count' ? Number(db.data.has(request.key)) : request.key;
          request.delivered = true; request.onsuccess?.({target: request});
        }
      },
      finish(outcome = 'complete') {
        assert.equal(transaction.done, false, 'Transaction settled only once.');
        transaction.succeedRequests(); transaction.done = true;
        if (outcome === 'complete') {
          for (const request of transaction.requests) if (request.kind === 'put') db.data.set(request.key, clone(request.value));
          transaction.oncomplete?.({target: transaction});
        } else {
          transaction.error = Error(`transaction ${outcome}`);
          if (outcome === 'error') transaction.onerror?.({target: transaction});
          else transaction.onabort?.({target: transaction});
        }
      },
      failRequest(index = 0) {
        const request = transaction.requests[index]; request.error = Error('request refused');
        request.onerror?.({target: request}); transaction.finish('abort');
      }
    };
    this.transactions.push(transaction);
    if (this.auto) queueMicrotask(() => transaction.finish());
    return transaction;
  }
}

async function saved(players = 2) {
  const db = new Database(), store = createIndexedMixStore(db), id = mixIdentity(trusted(players));
  await store.save(id, ['performer-1']);
  return {db, store, id, key: [...db.data.keys()][0]};
}

test('identity is exact, detached and limited to the saved acoustic roster', () => {
  const context = trusted(), id = mixIdentity(context);
  assert.deepEqual(id, {version: VERSION, job_id: JOB, bundle_id: SHA,
    stem_ids: ['performer-1', 'performer-2']});
  context.jobId = OTHER_JOB; context.reference.bundle_id = OTHER_SHA; context.roles[0].id = 'changed';
  assert.equal(id.job_id, JOB); assert.equal(id.bundle_id, SHA); assert.equal(id.stem_ids[0], 'performer-1');
  id.stem_ids.reverse(); assert.deepEqual(mixIdentity(trusted()).stem_ids, ['performer-1', 'performer-2']);
  assert.deepEqual(mixIdentity(trusted(1)).stem_ids, ['performer-1']);
  for (const value of [null, {}, {jobId: JOB, reference: {bundle_id: SHA}, roles: []},
    {...trusted(), jobId: JOB.toUpperCase()}, {...trusted(), jobId: '../bad'},
    {...trusted(), reference: {bundle_id: 'A'.repeat(64)}},
    {...trusted(), roles: [{id: 'performer-2'}]},
    {...trusted(), roles: [{id: 'performer-2'}, {id: 'performer-1'}]},
    {...trusted(), roles: [{id: 'performer-1'}, {id: 'performer-1'}]},
    {...trusted(), roles: [{id: 'performer-1'}, {id: 'performer-2'}, {id: 'performer-3'}]}]) {
    assert.throws(() => mixIdentity(value));
  }
});

test('missing means absent; mute choices persist in roster order and remain detached', async () => {
  const db = new Database(), store = createIndexedMixStore(db), id = identity();
  assert.equal(await store.load(id), null);
  const supplied = ['performer-2', 'performer-1'];
  const pending = store.save(id, supplied); supplied.length = 0; id.stem_ids.reverse();
  await pending;
  assert.deepEqual(await store.load(identity()), ['performer-1', 'performer-2']);
  const first = await store.load(identity()); first.pop();
  assert.deepEqual(await store.load(identity()), ['performer-1', 'performer-2']);
  await store.save(identity(), []); assert.deepEqual(await store.load(identity()), []);
  const record = [...db.data.values()][0];
  assert.deepEqual(Object.keys(record).sort(), ['bundle_id', 'job_id', 'muted_ids', 'stem_ids', 'version']);
  assert.ok(new TextEncoder().encode(JSON.stringify(record)).byteLength <= 1024);
  assert.equal(db.transactions.every(tx => tx.mode === 'readonly' || tx.mode === 'readwrite'), true);
});

test('job and bundle identities isolate records and preserve unrelated settings', async () => {
  const {db, store, id, key} = await saved();
  db.data.set('queue', ['unchanged']);
  const otherJob = {...id, job_id: OTHER_JOB}, otherBundle = {...id, bundle_id: OTHER_SHA};
  assert.equal(await store.load(otherJob), null); assert.equal(await store.load(otherBundle), null);
  await store.save(otherJob, ['performer-2']); await store.save(otherBundle, []);
  assert.deepEqual(await store.load(id), ['performer-1']);
  assert.deepEqual(await store.load(otherJob), ['performer-2']);
  assert.deepEqual(await store.load(otherBundle), []);
  assert.deepEqual(db.data.get('queue'), ['unchanged']); assert.equal(db.data.size, 4);
  assert.ok(key.startsWith(VERSION + ':') && key.includes(JOB) && key.endsWith(SHA));
});

test('invalid identities and selections refuse before creating any transaction', async () => {
  const db = new Database(), store = createIndexedMixStore(db), id = identity();
  const invalid = [{...id, version: 'old'}, {...id, extra: true}, {...id, job_id: false},
    {...id, bundle_id: SHA + '0'}, {...id, stem_ids: ['performer-1', 'performer-1']},
    {...id, stem_ids: ['performer-2']}, {...id, stem_ids: ['performer-2', 'performer-1']}];
  const accessor = {...id}; Object.defineProperty(accessor, 'job_id', {get() { throw Error('getter must stay unused'); }});
  invalid.push(accessor);
  for (const value of invalid) {
    await assert.rejects(store.load(value)); await assert.rejects(store.save(value, []));
  }
  const sparse = Array(1), augmented = ['performer-1']; augmented.extra = 'bad';
  for (const muted of [null, {}, 'performer-1', ['unknown'], ['performer-1', 'performer-1'],
    ['performer-1', 'performer-2', 'unknown'], sparse, augmented]) await assert.rejects(store.save(id, muted));
  assert.equal(db.transactions.length, 0);
  assert.throws(() => createIndexedMixStore(null));
});

test('malformed, stale, foreign and stored undefined records throw instead of resetting', async () => {
  const {db, store, id, key} = await saved(); const valid = clone(db.data.get(key));
  const invalid = [undefined, null, false, [], 'saved', {...valid, version: 'old'},
    {...valid, job_id: OTHER_JOB}, {...valid, bundle_id: OTHER_SHA},
    {...valid, stem_ids: ['performer-1']}, {...valid, muted_ids: ['performer-2', 'performer-1']},
    {...valid, muted_ids: ['performer-1', 'performer-1']}, {...valid, muted_ids: ['unknown']},
    {...valid, muted_ids: true}, {...valid, extra: 'x'.repeat(4096)}];
  const symbol = clone(valid); symbol[Symbol('extra')] = true; invalid.push(symbol);
  const hidden = clone(valid); Object.defineProperty(hidden, 'extra', {value: true}); invalid.push(hidden);
  for (const candidate of invalid) {
    // Inject only fake returned data; real IndexedDB cannot retain symbol keys.
    db.data.set(key, candidate);
    const txStore = db.transaction.bind(db);
    if (candidate === symbol || candidate === hidden) {
      db.transaction = (...args) => { const tx = txStore(...args); const finish = tx.succeedRequests;
        tx.succeedRequests = () => { for (const request of tx.requests) if (request.kind === 'get') {
          request.result = candidate; request.delivered = true; request.onsuccess?.();
        } finish(); }; return tx; };
    }
    await assert.rejects(store.load(id)); db.transaction = txStore;
    assert.equal(db.data.has(key), true, 'Malformed records remain preserved.');
  }
});

test('save resolves only after commit; an aborted or errored transaction rejects', async () => {
  for (const outcome of ['abort', 'error']) {
    const db = new Database({auto: false}), store = createIndexedMixStore(db);
    const promise = store.save(identity(), ['performer-1']); let settled = false;
    promise.then(() => { settled = true; }, () => { settled = true; });
    await tick(); const tx = db.transactions[0]; tx.succeedRequests(); await tick();
    assert.equal(settled, false); assert.equal(db.data.size, 0);
    tx.finish(outcome); await assert.rejects(promise); assert.equal(db.data.size, 0);
  }
  const db = new Database({auto: false}), store = createIndexedMixStore(db);
  const promise = store.save(identity(), []); let complete = false; promise.then(() => { complete = true; });
  await tick(); db.transactions[0].succeedRequests(); await tick(); assert.equal(complete, false);
  db.transactions[0].finish(); await promise; assert.equal(complete, true);
});

test('load also waits for transaction completion and refuses request and transaction faults', async () => {
  for (const outcome of ['abort', 'error', 'request']) {
    const db = new Database({auto: false}), store = createIndexedMixStore(db);
    const promise = store.load(identity()); const rejected = assert.rejects(promise);
    const tx = db.transactions[0];
    if (outcome === 'request') tx.failRequest(); else tx.finish(outcome);
    await rejected;
  }
  const db = new Database({auto: false}), store = createIndexedMixStore(db);
  const promise = store.load(identity()); let settled = false; promise.then(() => { settled = true; });
  db.transactions[0].succeedRequests(); await tick(); assert.equal(settled, false);
  db.transactions[0].finish(); assert.equal(await promise, null);
  const missing = store.load(identity()); const tx = db.transactions[1];
  tx.done = true; tx.oncomplete(); await assert.rejects(missing, /did not finish/);
});

test('rapid edits survive disposal and immediate same-key remount across store instances', async () => {
  const db = new Database({auto: false});
  const firstMount = createIndexedMixStore(db), nextMount = createIndexedMixStore(db), id = identity();
  const first = firstMount.save(id, ['performer-1']);
  const second = firstMount.save(id, ['performer-1', 'performer-2']);
  const lastIntent = []; const all = firstMount.save(id, lastIntent); lastIntent.push('performer-1');
  const reopened = nextMount.load(id); let loaded = false; reopened.then(() => { loaded = true; });
  await tick(); assert.equal(db.transactions.length, 1); assert.equal(loaded, false);
  db.transactions[0].finish(); await first; await tick();
  assert.equal(db.transactions.length, 2); assert.equal(loaded, false);
  db.transactions[1].finish(); await second; await tick();
  assert.equal(db.transactions.length, 3); assert.equal(loaded, false);
  db.transactions[2].finish(); await all; await tick();
  assert.equal(db.transactions.length, 4); assert.equal(db.transactions[3].mode, 'readonly');
  db.transactions[3].finish(); assert.deepEqual(await reopened, []);
});

test('a failed pending save rejects its waiting load but later explicit saves can recover', async () => {
  const db = new Database({auto: false}), first = createIndexedMixStore(db), second = createIndexedMixStore(db);
  const failed = first.save(identity(), ['performer-1']); const badSave = assert.rejects(failed);
  const waiting = second.load(identity()); const badLoad = assert.rejects(waiting);
  const recovered = second.save(identity(), ['performer-2']);
  await tick(); db.transactions[0].finish('abort'); await badSave; await badLoad; await tick();
  assert.equal(db.transactions.length, 2); db.transactions[1].finish(); await recovered;
  assert.deepEqual([...db.data.values()][0].muted_ids, ['performer-2']);
});

test('different identities do not block each other and closed databases reject cleanly', async () => {
  const db = new Database({auto: false}), store = createIndexedMixStore(db), first = identity();
  const a = store.save(first, []), b = store.save({...first, job_id: OTHER_JOB}, ['performer-2']);
  await tick(); assert.equal(db.transactions.length, 2);
  db.transactions[1].finish(); await b; db.transactions[0].finish(); await a;
  db.closed = true;
  await assert.rejects(store.load(first), /closed/); await assert.rejects(store.save(first, []), /closed/);
});
