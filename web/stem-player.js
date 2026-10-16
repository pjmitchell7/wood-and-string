/* One immutable, prevalidated arrangement. No fetching, decoding, or output on import. */
(function (root, factory) {
  'use strict';
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.StemPlayer = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const VERSION = 'stem-bundle-v1', MIX_VERSION = 'stem-mix-v1';
  const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
  function requireThat(test, message) { if (!test) throw new Error(message); }
  function text(value, name, maximum = 128) {
    requireThat(typeof value === 'string' && value.length > 0 && value.length <= maximum && !/[\x00-\x1f]/.test(value), `Invalid ${name}.`);
    return value;
  }
  function integer(value, name, minimum = 0) {
    requireThat(Number.isSafeInteger(value) && value >= minimum, `Invalid ${name}.`);
    return value;
  }
  function gain(value, maximum = 1) {
    requireThat(typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= maximum, 'Gain exceeds the declared mix ceiling or is invalid.');
    return value;
  }
  function sha(value, name) {
    requireThat(typeof value === 'string' && /^[a-f0-9]{64}$/.test(value), `Invalid ${name}.`);
    return value;
  }
  function freeze(object) {
    for (const value of Object.values(object)) if (value && typeof value === 'object') freeze(value);
    return Object.freeze(object);
  }
  function equal(left, right) {
    if (left === right) return true;
    if (!left || !right || typeof left !== 'object' || typeof right !== 'object' || Array.isArray(left) !== Array.isArray(right)) return false;
    const keys = Object.keys(left);
    return keys.length === Object.keys(right).length && keys.every(key => own(right, key) && equal(left[key], right[key]));
  }
  function validateArrangement(manifest, buffers, maxDecodedBytes) {
    integer(maxDecodedBytes, 'maxDecodedBytes', 1);
    requireThat(manifest && manifest.version === VERSION, 'Unsupported stem manifest.');
    const source = manifest.source || {}, clock = manifest.clock || {}, arrangement = manifest.arrangement || {}, renderer = manifest.renderer || {};
    const identity = {
      source: {id: text(source.id, 'source id'), sha256: sha(source.sha256, 'source hash')},
      arrangement: {
        revision: text(arrangement.revision, 'arrangement revision'),
        sha256: sha(arrangement.sha256, 'arrangement hash'),
        performers: integer(arrangement.performers, 'performers', 1),
        label: text(arrangement.label, 'arrangement label', 512)
      },
      clock: {
        sample_rate: integer(clock.sample_rate, 'sample rate', 8000),
        origin_sample: integer(clock.origin_sample, 'source origin'),
        body_frames: integer(clock.body_frames, 'body frames', 1),
        tail_frames: integer(clock.tail_frames, 'tail frames'),
        frames: integer(clock.frames, 'render frames', 1)
      },
      renderer: {
        id: text(renderer.id, 'renderer id'), version: text(renderer.version, 'renderer version'),
        receipt_sha256: sha(renderer.receipt_sha256, 'renderer receipt hash'),
        source_kind: text(renderer.source_kind, 'renderer source kind'),
        content_exclusion: text(renderer.content_exclusion, 'renderer content exclusion', 512)
      }
    };
    requireThat(renderer.source_kind === 'synthesized' && renderer.content_exclusion === 'declared_not_verified', 'Unsupported renderer lineage declaration.');
    requireThat(identity.arrangement.performers <= 3 && identity.clock.sample_rate <= 192000, 'Unsupported performer count or sample rate.');
    requireThat(clock.body_frames + clock.tail_frames === clock.frames && Number.isSafeInteger(clock.origin_sample + clock.frames), 'Invalid common render interval.');
    requireThat(Array.isArray(manifest.stems) && manifest.stems.length > 0 && manifest.stems.length <= 16, 'Invalid stem list.');
    requireThat(buffers instanceof Map || (buffers && typeof buffers === 'object' && !Array.isArray(buffers)), 'Provide decoded buffers by stem id.');
    const entries = buffers instanceof Map ? [...buffers.entries()] : Object.entries(buffers);
    const bufferMap = new Map(entries), usedBuffers = new Set(), ids = new Set();
    requireThat(bufferMap.size === manifest.stems.length, 'Decoded buffer set must exactly match available stems.');
    let decodedBytes = 0;
    const parts = manifest.stems.map(stem => {
      requireThat(stem && typeof stem === 'object', 'Invalid stem metadata.');
      const id = text(stem.id, 'stem id');
      requireThat(/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(id) && !ids.has(id), 'Invalid or duplicate stem id.');
      ids.add(id);
      requireThat(typeof stem.default_audible === 'boolean', 'Invalid default audible state.');
      const file = stem.file || {}, buffer = bufferMap.get(id);
      const metadata = {
        id, instrument: text(stem.instrument, 'stem instrument'), role: text(stem.role, 'stem role'),
        gain: gain(stem.gain), default_audible: stem.default_audible,
        file: {
          name: text(file.name, 'stem filename', 256), bytes: integer(file.bytes, 'encoded stem bytes', 1),
          sha256: sha(file.sha256, 'stem file hash'), format: file.format,
          channels: file.channels, sample_rate: file.sample_rate, frames: file.frames
        }
      };
      requireThat(file.format === 'pcm_s16le' && [1, 2].includes(file.channels), 'Unsupported stem format or channels.');
      requireThat(file.sample_rate === clock.sample_rate && file.frames === clock.frames, 'Stem metadata is not aligned to the common clock.');
      requireThat(buffer && typeof buffer.getChannelData === 'function' && !usedBuffers.has(buffer), 'Missing or reused decoded stem buffer.');
      usedBuffers.add(buffer);
      requireThat(buffer.sampleRate === clock.sample_rate && buffer.length === clock.frames && buffer.numberOfChannels === file.channels &&
        Number.isFinite(buffer.duration) && Math.abs(buffer.duration - clock.frames / clock.sample_rate) <= 1e-9, 'Decoded stem rate, frames, channels or duration do not match.');
      const bytes = buffer.length * buffer.numberOfChannels * 4;
      requireThat(Number.isSafeInteger(bytes) && Number.isSafeInteger(decodedBytes + bytes) && decodedBytes + bytes <= maxDecodedBytes, 'Decoded stems exceed maxDecodedBytes. Use a bounded streaming player.');
      decodedBytes += bytes;
      return {metadata: freeze(metadata), buffer, gain: metadata.gain, audible: metadata.default_audible};
    });
    requireThat(entries.every(([id]) => ids.has(id)), 'Decoded buffer set contains an unavailable part.');
    identity.stems = parts.map(part => part.metadata);
    return {identity: freeze(identity), parts, decodedBytes};
  }

  function createStemPlayer({context, destination, manifest, buffers, maxDecodedBytes, startLeadSeconds = 0.02, rampSeconds = 0.015, onEnded = () => {}} = {}) {
    // Structural admission happens before creating nodes. Byte/hash/media evidence is caller-owned.
    const admitted = validateArrangement(manifest, buffers, maxDecodedBytes);
    requireThat(context && typeof context.createGain === 'function' && typeof context.createBufferSource === 'function' &&
      typeof context.resume === 'function' && Number.isFinite(context.currentTime) && context.currentTime >= 0, 'An existing AudioContext is required.');
    destination = destination || context.destination;
    requireThat(destination && typeof onEnded === 'function', 'An audio destination and valid callback are required.');
    requireThat(Number.isFinite(startLeadSeconds) && startLeadSeconds >= 0 && startLeadSeconds <= 1, 'Invalid scheduled start lead.');
    requireThat(Number.isFinite(rampSeconds) && rampSeconds >= 0.001 && rampSeconds <= 0.1, 'Gain ramps must be 1 to 100 milliseconds.');
    const {identity, parts, decodedBytes} = admitted, partMap = new Map(parts.map(part => [part.metadata.id, part]));
    buffers = null; manifest = null; // Retain only admitted buffers and copied metadata.
    const duration = identity.clock.frames / identity.clock.sample_rate;
    let status = 'paused', offset = 0, generation = 0, voices = [], anchorTime = 0, anchorOffset = 0, flight = null;
    const usable = () => requireThat(status !== 'disposed', 'This stem player is disposed.');
    const target = part => part.audible ? part.gain : 0;
    const position = () => status === 'playing' ? Math.min(duration, anchorOffset + Math.max(0, context.currentTime - anchorTime)) : offset;
    function release(list) {
      for (const voice of list) {
        if (voice.source) {
          voice.source.onended = null;
          try { voice.source.stop(); } catch (_) { /* Already stopped or not started. */ }
          try { voice.source.disconnect(); } catch (_) { /* Best effort after allocation failure. */ }
          voice.source.buffer = null;
        }
        if (voice.node) { try { voice.node.disconnect(); } catch (_) { /* Only our gain node. */ } }
      }
    }
    function cancel(nextStatus = 'paused') {
      offset = position(); generation++; flight = null;
      const old = voices; voices = []; status = nextStatus; release(old);
    }
    function finish(token) {
      if (token !== generation || status !== 'playing') return;
      // All buffers have the same declared endpoint. An early/stale ended event is insufficient.
      if (context.currentTime + 1 / identity.clock.sample_rate < anchorTime + duration - anchorOffset) return;
      cancel('ended'); offset = duration; onEnded(identity);
    }
    function sync() {
      if (status === 'playing' && context.currentTime >= anchorTime + duration - anchorOffset) finish(generation);
    }
    function play() {
      usable(); sync();
      if (status === 'playing') return Promise.resolve(true);
      if (flight) return flight.promise;
      if (offset >= duration) offset = 0;
      const token = generation, request = {token}; status = 'starting';
      request.promise = (async () => {
        await Promise.resolve(); // Publish the flight before a caller's resume can throw.
        if (token !== generation || status === 'disposed') return false;
        try {
          await context.resume();
          if (token !== generation || status === 'disposed') return false;
          requireThat(context.state !== 'closed', 'The caller AudioContext is closed.');
          const created = [];
          try {
            for (const part of parts) {
              const voice = {part, source: null, node: null}; created.push(voice);
              voice.node = context.createGain(); voice.source = context.createBufferSource();
              voice.source.buffer = part.buffer; voice.source.connect(voice.node); voice.node.connect(destination);
              const value = target(part), now = context.currentTime;
              voice.node.gain.setValueAtTime(value, now);
              voice.ramp = {from: value, to: value, start: now, end: now};
            }
            // Choose one start only after creating every source, never one time per stem.
            const when = context.currentTime + startLeadSeconds, start = offset;
            anchorTime = when; anchorOffset = start; voices = created; status = 'playing';
            for (const voice of created) {
              voice.source.onended = () => {
                if (token !== generation || status !== 'playing' || !voices.includes(voice)) return;
                finish(token);
              };
              voice.source.start(when, start);
            }
            return true;
          } catch (error) {
            voices = []; release(created); status = 'paused'; throw error;
          }
        } catch (error) {
          if (token !== generation || status === 'disposed') return false;
          status = 'paused'; throw error;
        } finally { if (flight === request) flight = null; }
      })();
      flight = request; return request.promise;
    }
    function pause() { usable(); sync(); cancel(status === 'ended' ? 'ended' : 'paused'); }
    function stop() { usable(); cancel(); offset = 0; }
    function seek(seconds) {
      usable(); requireThat(typeof seconds === 'number' && Number.isFinite(seconds), 'Seek requires finite render seconds.');
      const resume = status === 'playing' || status === 'starting';
      cancel(); offset = Math.max(0, Math.min(duration, seconds));
      if (offset === duration) status = 'ended';
      return resume && offset < duration ? play() : Promise.resolve(false);
    }
    function partFor(id) {
      usable(); requireThat(partMap.has(id), 'This part is unavailable. Create a new arrangement to add it.');
      return partMap.get(id);
    }
    function applyGain(part) {
      const voice = voices.find(item => item.part === part); if (!voice) return;
      const now = context.currentTime, previous = voice.ramp;
      const fraction = previous.end <= previous.start ? 1 : Math.max(0, Math.min(1, (now - previous.start) / (previous.end - previous.start)));
      const current = previous.from + (previous.to - previous.from) * fraction, value = target(part);
      voice.node.gain.cancelScheduledValues(now);
      voice.node.gain.setValueAtTime(current, now);
      voice.node.gain.linearRampToValueAtTime(value, now + rampSeconds);
      voice.ramp = {from: current, to: value, start: now, end: now + rampSeconds};
    }
    function setMuted(id, muted) {
      const part = partFor(id); requireThat(typeof muted === 'boolean', 'Muted must be boolean.');
      part.audible = !muted; applyGain(part);
    }
    function setGain(id, value) {
      const part = partFor(id); part.gain = gain(value, part.metadata.gain); applyGain(part);
    }
    function setAllMuted(muted) {
      usable(); requireThat(typeof muted === 'boolean', 'Muted must be boolean.');
      for (const part of parts) { part.audible = !muted; applyGain(part); }
    }
    function mixSnapshot() {
      usable();
      return {
        version: MIX_VERSION, identity: JSON.parse(JSON.stringify(identity)), mix_point: 'linear_pre_master', normalization: 'none',
        stems: parts.map(part => ({id: part.metadata.id, gain: part.gain, audible: part.audible})),
        active_stem_ids: parts.filter(part => part.audible && part.gain > 0).map(part => part.metadata.id)
      };
    }
    function restoreMixSnapshot(snapshot) {
      usable();
      // Validate everything before changing any live mix state.
      requireThat(snapshot && snapshot.version === MIX_VERSION && snapshot.mix_point === 'linear_pre_master' && snapshot.normalization === 'none', 'Invalid saved mix.');
      requireThat(equal(identity, snapshot.identity), 'Saved mix belongs to another arrangement or render.');
      requireThat(Array.isArray(snapshot.stems) && snapshot.stems.length === parts.length, 'Saved mix parts are incomplete.');
      const seen = new Set(), next = snapshot.stems.map(row => {
        requireThat(row && partMap.has(row.id) && !seen.has(row.id) && typeof row.audible === 'boolean', 'Saved mix contains an unavailable, duplicate or invalid part.');
        seen.add(row.id); const part = partMap.get(row.id);
        return {part, gain: gain(row.gain, part.metadata.gain), audible: row.audible};
      });
      const active = next.filter(row => row.audible && row.gain > 0).map(row => row.part.metadata.id).sort();
      requireThat(Array.isArray(snapshot.active_stem_ids) && JSON.stringify([...snapshot.active_stem_ids].sort()) === JSON.stringify(active), 'Saved mix active parts do not match its gains.');
      for (const row of next) { row.part.gain = row.gain; row.part.audible = row.audible; applyGain(row.part); }
    }
    function dispose() {
      if (status === 'disposed') return;
      cancel('disposed'); for (const part of parts) part.buffer = null;
    }
    return {
      play, resume: play, pause, stop, seek, sync, setMuted, setGain, setAllMuted,
      mixSnapshot, exportMixSnapshot: () => JSON.stringify(mixSnapshot()), restoreMixSnapshot, dispose,
      position() { sync(); return position(); },
      sourcePosition() { sync(); return identity.clock.origin_sample + position() * identity.clock.sample_rate; },
      get state() { sync(); return status; },
      get allMuted() { return parts.every(part => !part.audible || part.gain === 0); },
      get identity() { return identity; },
      get stems() { return Object.freeze(parts.map(part => part.metadata)); },
      get duration() { return duration; },
      get decodedBytes() { return status === 'disposed' ? 0 : decodedBytes; }
    };
  }
  return Object.freeze({createStemPlayer, validateArrangement});
});
