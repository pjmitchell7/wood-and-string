import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import vm from 'node:vm';
import {createRequire} from 'node:module';
const require = createRequire(import.meta.url);
const {createStemPlayer, validateArrangement} = require('../web/stem-player.js');
const RATE = 48000, FRAMES = 480000, ORIGIN = 96000, BYTES = FRAMES * 2 * 4;
const close = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-8, `${actual} != ${expected}`);
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };

// Stateful silent Web Audio boundary: no browser, native device, PCM fixture or output.
class Param {
  events = [];
  setValueAtTime(value, time) { this.events.push({kind: 'set', value, time}); }
  linearRampToValueAtTime(value, time) { this.events.push({kind: 'ramp', value, time}); }
  cancelScheduledValues(time) {
    this.events = this.events.filter(event => event.time < time);
  }
  at(time) {
    let previous = {kind: 'set', value: 1, time: 0};
    for (const event of this.events) {
      if (event.time > time) {
        if (event.kind === 'ramp') return previous.value + (event.value - previous.value) * (time - previous.time) / (event.time - previous.time);
        break;
      }
      previous = event;
    }
    return previous.value;
  }
}
class Node {
  connections = []; disconnected = false;
  connect(other) { this.connections.push(other); }
  disconnect() { this.connections = []; this.disconnected = true; }
}
class Source extends Node {
  constructor(context) { super(); this.context = context; }
  buffer = null; stopped = false; started = false; delivered = false; stopCalls = 0;
  start(when, offset) {
    assert.equal(this.started, false, 'AudioBufferSourceNode is single-use');
    if (this.context.failStart === this.context.sources.indexOf(this)) throw Error('authored start failure');
    this.started = true; this.when = when; this.offset = offset;
    this.end = when + this.buffer.duration - offset;
  }
  stop() { this.stopCalls++; this.stopped = true; }
}
class Context {
  currentTime = 0; sampleRate = RATE; state = 'suspended'; sources = []; gains = [];
  resumes = 0; closes = 0; suspends = 0; destination = new Node(); callbacks = [];
  async resume() { this.resumes++; this.state = 'running'; }
  close() { this.closes++; }
  suspend() { this.suspends++; }
  createGain() {
    if (this.failGain === this.gains.length) throw Error('authored allocation failure');
    const node = new Node(); node.gain = new Param(); this.gains.push(node); return node;
  }
  createBufferSource() { const node = new Source(this); this.sources.push(node); return node; }
  advance(time, deliver = true) {
    assert.ok(time >= this.currentTime); this.currentTime = time;
    for (const source of this.sources) {
      if (source.started && !source.stopped && !source.delivered && source.end <= time) {
        source.delivered = true;
        if (source.onended) this.callbacks.push(source.onended);
      }
    }
    if (deliver) this.drain();
  }
  drain() { for (const callback of this.callbacks.splice(0)) callback(); }
}
function fakeBuffer(overrides = {}) {
  return Object.freeze({sampleRate: RATE, length: FRAMES, numberOfChannels: 2, duration: FRAMES / RATE,
    getChannelData() { throw Error('The player must not inspect or mutate PCM.'); }, ...overrides});
}
function fixture() {
  const file = name => ({name: `${name}.wav`, bytes: FRAMES * 4 + 44, sha256: '3'.repeat(64), format: 'pcm_s16le', channels: 2, sample_rate: RATE, frames: FRAMES});
  const manifest = {
    version: 'stem-bundle-v1', source: {id: 'unchanged-source', sha256: '1'.repeat(64)},
    clock: {sample_rate: RATE, origin_sample: ORIGIN, body_frames: FRAMES - RATE, tail_frames: RATE, frames: FRAMES},
    arrangement: {revision: 'duo-r1', sha256: '2'.repeat(64), performers: 2, label: 'Keep it close, melody and backing'},
    renderer: {id: 'authored-silent-fixture', version: '1', receipt_sha256: '6'.repeat(64), source_kind: 'synthesized', content_exclusion: 'declared_not_verified'},
    stems: [
      {id: 'lead', instrument: 'acoustic', role: 'melody', gain: 0.4, default_audible: true, file: file('lead')},
      {id: 'backing', instrument: 'acoustic', role: 'backing', gain: 0.3, default_audible: false, file: file('backing')}
    ]
  };
  return {manifest, buffers: {lead: fakeBuffer(), backing: fakeBuffer()}};
}
function rig(options = {}) {
  const context = new Context(), ended = [], inputs = fixture();
  const player = createStemPlayer({context, ...inputs, maxDecodedBytes: BYTES * 2, onEnded: identity => ended.push(identity), ...options});
  return {context, player, ended, ...inputs};
}
function assertReleased(context) {
  assert.ok(context.sources.every(node => node.stopped && node.disconnected && node.buffer === null && node.onended === null));
  assert.ok(context.gains.every(node => node.disconnected));
  assert.equal(context.closes, 0); assert.equal(context.suspends, 0); assert.equal(context.destination.disconnected, false);
}

test('import is inert and the browser global and CommonJS expose the same API', () => {
  const sandbox = {}; vm.runInNewContext(fs.readFileSync(new URL('../web/stem-player.js', import.meta.url), 'utf8'), sandbox);
  assert.deepEqual(Object.keys(sandbox.StemPlayer).sort(), ['createStemPlayer', 'validateArrangement']);
  assert.equal(typeof sandbox.StemPlayer.createStemPlayer, 'function');
});
test('one common scheduled start and offset; source origin and complete tail interval stay intact', async () => {
  const r = rig(); await r.player.seek(3.5); r.context.currentTime = 12;
  assert.equal(await r.player.play(), true);
  assert.equal(r.context.sources.length, 2);
  for (const source of r.context.sources) { close(source.when, 12.02); close(source.offset, 3.5); close(source.end, 18.52); }
  assert.equal(r.player.position(), 3.5); assert.equal(r.player.sourcePosition(), ORIGIN + 3.5 * RATE);
  r.context.advance(14.02); close(r.player.position(), 5.5); close(r.player.sourcePosition(), ORIGIN + 5.5 * RATE);
  assert.equal(r.player.duration, 10); assert.equal(r.player.identity.clock.tail_frames, RATE);
  assert.equal(r.context.gains[0].gain.at(14.02), 0.4); assert.equal(r.context.gains[1].gain.at(14.02), 0);
  r.player.dispose(); assertReleased(r.context);
});
test('repeated play and resume use only one source generation', async () => {
  const r = rig(); const first = r.player.play(), second = r.player.resume();
  assert.equal(first, second); await first; await r.player.play();
  assert.equal(r.context.resumes, 1); assert.equal(r.context.sources.length, 2);
  r.player.dispose();
});
test('pause and resume use the audio clock, including scheduled lead time', async () => {
  const r = rig(); await r.player.play(); r.context.advance(0.01); r.player.pause();
  assert.equal(r.player.position(), 0); assertReleased(r.context);
  await r.player.play(); r.context.advance(2.03); r.player.pause(); close(r.player.position(), 2);
  r.context.advance(9); await r.player.resume();
  for (const source of r.context.sources.slice(-2)) { close(source.offset, 2); close(source.when, 9.02); }
  r.player.stop(); assert.equal(r.player.position(), 0); assert.equal(r.player.state, 'paused'); assertReleased(r.context);
});
test('muting the complete performer channel ramps through its effects tail without rescheduling', async () => {
  const r = rig(); await r.player.play(); r.context.advance(9.2);
  const source = r.context.sources[0], node = source.connections[0];
  r.player.setMuted('lead', true);
  assert.equal(source.connections[0], node); assert.equal(source.stopped, false);
  close(node.gain.at(9.2), 0.4); close(node.gain.at(9.2075), 0.2); close(node.gain.at(9.215), 0);
  assert.equal(r.player.allMuted, true); assert.equal(r.player.state, 'playing');
  r.context.advance(9.3); r.player.setMuted('lead', false); close(node.gain.at(9.315), 0.4);
  assert.equal(r.context.sources.length, 2); assert.equal(r.player.identity.arrangement.performers, 2);
  r.player.dispose();
});
test('rapid mute reversal interpolates current ramp level rather than jumping to the old target', async () => {
  const r = rig({rampSeconds: 0.02}); await r.player.play(); r.context.advance(1);
  r.player.setMuted('lead', true); r.context.advance(1.01); r.player.setMuted('lead', false);
  const param = r.context.gains[0].gain;
  close(param.at(1.01), 0.2); close(param.at(1.02), 0.3); close(param.at(1.03), 0.4);
  r.player.dispose();
});
test('all-muted transport keeps position; restoring only available saved parts does not normalize', async () => {
  const r = rig(); r.player.setAllMuted(true); await r.player.play();
  r.context.advance(2.02); close(r.player.position(), 2); assert.equal(r.player.allMuted, true);
  r.player.setAllMuted(false); r.context.advance(2.04);
  close(r.context.gains[0].gain.at(2.04), 0.4); close(r.context.gains[1].gain.at(2.04), 0.3);
  assert.equal(r.player.allMuted, false); assert.equal(r.context.sources.length, 2);
  assert.throws(() => r.player.setMuted('new-drums', false), /unavailable/);
  assert.throws(() => r.player.setGain('new-drums', 0.1), /unavailable/);
  assert.throws(() => r.player.setGain('lead', 0.401), /ceiling/);
  r.player.setGain('lead', 0.1); r.context.advance(2.06); close(r.context.gains[0].gain.at(2.06), 0.1);
  r.player.dispose();
});
test('playing seek stops the old generation and aligns new sources at the requested frame', async () => {
  const r = rig(); await r.player.play(); r.context.advance(1.02);
  const staleEnded = r.context.sources[0].onended;
  await r.player.seek(4); assert.equal(r.context.sources.length, 4);
  for (const source of r.context.sources.slice(-2)) { close(source.offset, 4); close(source.when, 1.04); }
  staleEnded(); assert.equal(r.player.state, 'playing'); assert.equal(r.ended.length, 0);
  r.player.pause(); await r.player.seek(7); assert.equal(r.player.position(), 7); assert.equal(r.context.sources.length, 4);
  r.player.dispose(); assertReleased(r.context);
});
for (const operation of ['pause', 'stop', 'dispose']) {
  test(`pending AudioContext resume cancelled by ${operation} never creates sources`, async () => {
    const r = rig(); let release;
    r.context.resume = () => new Promise(resolve => { release = resolve; });
    const pending = r.player.play(); await flush(); assert.equal(r.player.state, 'starting');
    r.player[operation](); release(); assert.equal(await pending, false);
    assert.equal(r.context.sources.length, 0); assert.equal(r.context.closes, 0);
    if (operation !== 'dispose') r.player.dispose();
  });
}
test('rapid seeks during pending resume discard every stale request', async () => {
  const r = rig(), releases = [];
  r.context.resume = () => new Promise(resolve => releases.push(resolve));
  const initial = r.player.play(); await flush();
  const first = r.player.seek(2); await flush();
  const second = r.player.seek(6); await flush(); assert.equal(releases.length, 3);
  releases[2](); assert.equal(await second, true); assert.equal(r.context.sources.length, 2);
  releases[1](); releases[0](); assert.equal(await first, false); assert.equal(await initial, false);
  assert.ok(r.context.sources.every(source => source.offset === 6)); r.player.dispose();
});
test('late rejection from cancelled resume cannot replace the active generation', async () => {
  const r = rig(); let reject;
  r.context.resume = () => new Promise((resolve, fail) => { reject = fail; });
  const stale = r.player.play(); await flush(); r.player.pause();
  r.context.resume = async () => {}; await r.player.play();
  reject(Error('stale rejected resume')); assert.equal(await stale, false);
  assert.equal(r.player.state, 'playing'); assert.equal(r.context.sources.length, 2); r.player.dispose();
});
test('immediate pause cancels play before touching the caller context', async () => {
  const r = rig(); const pending = r.player.play(); r.player.pause();
  assert.equal(await pending, false); assert.equal(r.context.resumes, 0); r.player.dispose();
});
test('natural end and stale queued handlers finish exactly once, including the complete tail', async () => {
  const r = rig(); await r.player.play();
  const early = r.context.sources[0].onended; r.context.advance(9.5); early();
  assert.equal(r.ended.length, 0); assert.equal(r.player.state, 'playing');
  r.context.advance(10.02, false); assert.equal(r.context.callbacks.length, 2);
  r.context.drain(); assert.equal(r.ended.length, 1); assert.equal(r.player.state, 'ended');
  assert.equal(r.player.position(), 10); assert.equal(r.player.sourcePosition(), ORIGIN + FRAMES); assertReleased(r.context);
  await r.player.play(); early(); assert.equal(r.player.position(), 0); assert.equal(r.ended.length, 1);
  r.player.dispose();
});
test('clock sync finishes when ended dispatch is delayed; queued old end cannot stop a resumed arrangement', async () => {
  const r = rig(); await r.player.play(); r.context.advance(10.02, false);
  assert.equal(r.player.state, 'ended'); assert.equal(r.ended.length, 1);
  await r.player.resume(); r.context.drain(); assert.equal(r.player.state, 'playing'); assert.equal(r.ended.length, 1);
  r.player.dispose();
});
test('allocation or partial source-start failure cleans every owned source and gain', async () => {
  for (const kind of ['failGain', 'failStart']) {
    const r = rig(); r.context[kind] = 1;
    await assert.rejects(r.player.play(), /authored/); assert.equal(r.player.state, 'paused');
    assertReleased(r.context); r.context[kind] = undefined; assert.equal(await r.player.play(), true); r.player.dispose();
  }
});
test('a synchronously thrown resume can be retried and does not leave a stuck flight', async () => {
  const r = rig(); r.context.resume = () => { throw Error('authored synchronous resume error'); };
  await assert.rejects(r.player.play(), /authored/); assert.equal(r.player.state, 'paused');
  r.context.resume = async () => {}; assert.equal(await r.player.play(), true); r.player.dispose();
});
test('historical synchronous-resume race reproduces in an isolated source variant', async () => {
  const source = fs.readFileSync(new URL('../web/stem-player.js', import.meta.url), 'utf8');
  const marker = '        await Promise.resolve(); // Publish the flight before a caller\'s resume can throw.\n';
  assert.ok(source.includes(marker));
  const sandbox = {}; vm.runInNewContext(source.replace(marker, ''), sandbox);
  const context = new Context(), inputs = fixture(); context.resume = () => { throw Error('preserved historical resume failure'); };
  const player = sandbox.StemPlayer.createStemPlayer({context, ...inputs, maxDecodedBytes: 2 * BYTES});
  const first = player.play(); await assert.rejects(first, /preserved historical/);
  context.resume = async () => {};
  const retry = player.play(); assert.equal(retry, first); await assert.rejects(retry, /preserved historical/);
  assert.equal(context.sources.length, 0); player.dispose();
});
test('saved/exported mix reloads separately without modifying arrangement, buffers or defaults', async () => {
  const r = rig(), before = JSON.stringify(r.manifest), identity = r.player.identity;
  assert.ok(Object.isFrozen(identity) && Object.isFrozen(identity.arrangement));
  r.player.setMuted('lead', true); r.player.setMuted('backing', false); r.player.setGain('backing', 0.2);
  const saved = JSON.parse(r.player.exportMixSnapshot()); assert.deepEqual(saved.active_stem_ids, ['backing']);
  const other = rig(); other.player.restoreMixSnapshot(saved);
  assert.deepEqual(other.player.mixSnapshot(), saved); assert.equal(other.player.identity.arrangement.performers, 2);
  saved.stems[0].audible = true; assert.equal(other.player.mixSnapshot().stems[0].audible, false);
  r.manifest.arrangement.label = 'mutated caller label'; r.manifest.stems[0].gain = 1;
  assert.equal(identity.arrangement.label, 'Keep it close, melody and backing'); assert.equal(r.player.stems[0].gain, 0.4);
  r.manifest.arrangement.label = 'Keep it close, melody and backing'; r.manifest.stems[0].gain = 0.4;
  assert.equal(JSON.stringify(r.manifest), before); assert.equal(r.player.decodedBytes, 2 * BYTES);
  await r.player.play(); assert.equal(r.context.sources[0].buffer, r.buffers.lead);
  r.player.dispose(); other.player.dispose(); assert.equal(r.player.decodedBytes, 0);
});
test('mix restore validates identity, full active set and gain ceilings atomically', () => {
  const r = rig(), initial = r.player.mixSnapshot();
  const changes = [
    row => row.identity.arrangement.revision = 'solo-r2',
    row => row.identity.arrangement.sha256 = '4'.repeat(64),
    row => row.identity.clock.origin_sample++,
    row => row.identity.stems[0].file.sha256 = '4'.repeat(64),
    row => row.identity.stems[0].file.bytes++,
    row => row.identity.stems[0].gain = 0.39,
    row => row.identity.stems[0].default_audible = false,
    row => row.identity.renderer.version = 'another-renderer',
    row => row.identity.renderer.receipt_sha256 = '5'.repeat(64),
    row => row.identity.source.unrecognized = 'unexpected identity field',
    row => row.stems[1].id = row.stems[0].id,
    row => row.stems[1].gain = 1,
    row => row.stems[0].audible = 0,
    row => row.active_stem_ids = [],
    row => row.normalization = 'automatic'
  ];
  for (const change of changes) {
    const saved = structuredClone(initial); change(saved); assert.throws(() => r.player.restoreMixSnapshot(saved));
    assert.deepEqual(r.player.mixSnapshot(), initial);
  }
  r.player.dispose();
});
test('saved mix identity comparison ignores object key order, binds receipt hashes, and rejects changed file renders', () => {
  const f = fixture(); f.manifest.renderer.receipt_sha256 = '6'.repeat(64);
  const first = rig(f), snapshot = first.player.mixSnapshot();
  snapshot.identity.source = {sha256: snapshot.identity.source.sha256, id: snapshot.identity.source.id};
  first.player.restoreMixSnapshot(snapshot);
  const changed = fixture(); changed.manifest.renderer.receipt_sha256 = '6'.repeat(64);
  changed.manifest.stems[0].file.sha256 = '7'.repeat(64);
  const second = rig(changed); assert.throws(() => second.player.restoreMixSnapshot(snapshot), /another arrangement or render/);
  first.player.dispose(); second.player.dispose();
});
test('malformed, missing and oversized buffer admissions allocate no audio nodes', () => {
  const changes = [
    f => f.manifest.version = 'other',
    f => f.manifest.source.sha256 = 'bad',
    f => f.manifest.clock.origin_sample = Number.MAX_SAFE_INTEGER,
    f => f.manifest.clock.tail_frames++,
    f => f.manifest.clock.frames = Infinity,
    f => f.manifest.arrangement.performers = 0,
    f => delete f.manifest.renderer.receipt_sha256,
    f => f.manifest.renderer.source_kind = 'original',
    f => f.manifest.renderer.content_exclusion = 'verified',
    f => f.manifest.stems[1].id = 'lead',
    f => f.manifest.stems[0].gain = NaN,
    f => f.manifest.stems[0].gain = -0.1,
    f => f.manifest.stems[0].default_audible = 1,
    f => f.manifest.stems[0].file.channels = 6,
    f => f.manifest.stems[0].file.sample_rate = 44100,
    f => f.manifest.stems[0].file.frames--,
    f => f.buffers.lead = fakeBuffer({length: FRAMES - 1}),
    f => f.buffers.lead = fakeBuffer({sampleRate: 44100}),
    f => f.buffers.lead = fakeBuffer({numberOfChannels: 1}),
    f => f.buffers.lead = fakeBuffer({duration: 9}),
    f => f.buffers.backing = f.buffers.lead,
    f => delete f.buffers.backing,
    f => f.buffers.missing = fakeBuffer()
  ];
  for (const change of changes) {
    const context = new Context(), f = fixture(); change(f);
    assert.throws(() => createStemPlayer({context, ...f, maxDecodedBytes: 2 * BYTES}));
    assert.equal(context.sources.length, 0); assert.equal(context.gains.length, 0);
  }
  const f = fixture();
  assert.equal(validateArrangement(f.manifest, new Map(Object.entries(f.buffers)), 2 * BYTES).decodedBytes, 2 * BYTES);
  for (const limit of [undefined, NaN, Infinity, 0, 2 * BYTES - 1]) assert.throws(() => validateArrangement(f.manifest, f.buffers, limit));
});
test('invalid operations do not disturb playback; disposal is idempotent and context stays caller-owned', async () => {
  const r = rig(); await r.player.play();
  for (const value of [NaN, Infinity, '2']) assert.throws(() => r.player.seek(value));
  assert.throws(() => r.player.setMuted('lead', 1)); assert.equal(r.context.sources.length, 2);
  r.player.dispose(); r.player.dispose(); assert.equal(r.player.state, 'disposed'); assertReleased(r.context);
  for (const operation of [() => r.player.play(), () => r.player.seek(0), () => r.player.setAllMuted(false), () => r.player.mixSnapshot()]) assert.throws(operation, /disposed/);
});
