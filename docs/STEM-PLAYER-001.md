# Bounded shared-clock stem player

16 October 2026. Source component and silent control tests only. Production playback is not activated. `web/app.js`, the service worker, server, renderer and existing transport are unchanged by this component.

## What exists

`web/stem-player.js` is dependency-free CommonJS and a classic-browser-script global, `StemPlayer`. Importing it does not create an AudioContext, fetch, decode, start sound, install a timer or change the application. The caller supplies one existing AudioContext, its destination, an independently verified `stem-bundle-v1` manifest and an exact set of already decoded, aligned AudioBuffers.

Each available performer or percussion group has one complete buffer. Its dry sound and pre-rendered effects tail pass through one owned gain node. Muting fades that whole channel over 15 ms by default, including the tail. This component has no separate unmuted effects bus. Shared or nonlinear effects after the supplied destination need their own processing-chain and export evidence.

All source nodes use one future AudioContext start time and one render offset. Position advances from that audio clock. Pausing captures it; resuming and seeking recreate single-use source nodes at a common offset. All-muted playback continues advancing and can restore parts at the current position. Repeated play does not restart an active generation. Pause, stop, seek and dispose invalidate pending resumes and old end handlers. Disposal releases owned sources, gains and buffer references, and never closes, suspends or disconnects the caller's context or destination.

Solo and Duo remain separate immutable arrangements. Muting a duet performer leaves its arrangement identity and performer count intact. No live arrangement switching, musical redistribution or instrument creation occurs. A requested absent stem is refused with a Create a new arrangement message; All only changes available manifest stems.

## Caller contract

The engine consumes the following backend-contract fields:

| Field | Structural checks |
| --- | --- |
| `version` | Exactly `stem-bundle-v1`. |
| `source.id`, `source.sha256` | Bounded nonempty ID, lowercase 64-digit SHA-256 text. |
| `arrangement.revision`, `sha256`, `performers`, `label` | Bounded strings/hash; one to three musical performers. The hash identifies the musical arrangement, independently of render files. |
| `clock.sample_rate`, `origin_sample`, `body_frames`, `tail_frames`, `frames` | Safe integers; 8,000 to 192,000 Hz; positive body and total; body plus tail equals total; source origin plus total stays a safe integer. |
| `renderer.id`, `version`, `receipt_sha256`, `source_kind`, `content_exclusion` | Bounded ID/version and required receipt hash. V1 requires the declarations `synthesized` and `declared_not_verified`; the labels do not prove either property. |
| `stems` | One to sixteen unique available stem IDs; bounded instrument/role strings; finite declared gain from zero to one; boolean `default_audible`. |
| `stems[].file` | Name, positive encoded byte count, SHA-256 text, `pcm_s16le`, one/two channels, and the exact common sample rate/frame count. The current backend v1 verifier uses stereo PCM16 WAV. |
| `buffers` | Map or own-property object keyed by exactly the stem IDs. No missing, surplus or reused buffer objects. Every buffer's frames, channels, rate and duration must match. |
| `maxDecodedBytes` | Required positive safe integer. Sum of `frames * channels * 4` for every admitted buffer, including silent/default-muted stems and tails, must fit. |

There is no decode fallback or silent relaxation of the limit. Admission happens before audio nodes are created. This bounds the decoded PCM references the engine adopts; it does not bound buffers already allocated elsewhere, browser-internal copies, processing overhead, process RAM or combined retained/cumulative research storage. A caller must perform pre-decode admission too. Large full songs need a separately validated bounded streaming/preload design.

Structural validation does not hash file bytes or prove that a supplied AudioBuffer came from a declared file. The backend verification receipt must bind the exact manifest bytes and retained PCM files independently; the integration must bind decoding to those verified bytes. Claimed hashes, `source_kind`, provenance and content-exclusion labels alone do not prove source preservation, synthesis, vocal exclusion or musical fidelity. The engine does not inspect, normalize, copy or mutate PCM. The caller must keep its supplied buffers unchanged while retained.

One declared clock rate covers source origin and render frames. `position()` returns seconds relative to the rendered interval. `sourcePosition()` returns source samples, potentially fractional between samples: `origin_sample + position() * sample_rate`. Resampling offsets must already have been resolved into this common clock. Any output-device resampling is outside this structural proof.

## Interface and saved mix

```js
const player = StemPlayer.createStemPlayer({
  context, destination: context.destination,
  manifest: independentlyVerifiedManifest,
  buffers: alreadyValidatedBuffersByStemId,
  maxDecodedBytes: explicitCallerBudget
});
```

`play()`/`resume()` return a promise: true when this generation is scheduled, false when superseded or cancelled. They share a pending request. `pause()` retains position; `stop()` resets to zero. `seek(renderSeconds)` rejects nonfinite/non-numeric inputs, clamps finite positions to the interval, and resumes only if playback was requested. Seeking exactly to the end leaves an ended transport; play from an ended transport restarts at zero. `sync()`, state/position reads and owned ended events complete a natural endpoint exactly once. `onEnded(identity)` is optional. There is no UI polling timer in this module.

`setMuted(id, boolean)`, `setAllMuted(boolean)` and `setGain(id, number)` update a separate transient mix. A gain can only be reduced from its declared ceiling. Turning a part on never increases another part's gain, applies loudness compensation or rewrites defaults. Fast mute reversals start from the interpolated current ramp level. Ramp duration is configurable from 1 to 100 ms; scheduled start lead defaults to 20 ms and is configurable from zero to one second.

`mixSnapshot()` returns detached JSON-compatible state; `exportMixSnapshot()` returns its JSON string. The snapshot includes the exact active stem IDs, each transient gain/audible state, `linear_pre_master` comparison point and `normalization: none`. The caller persists it or uses it as a rendering/export request. These methods do not write storage or create an audio export. `restoreMixSnapshot()` validates the whole snapshot before applying it and rejects another arrangement/render, incomplete/duplicate parts, a gain over its ceiling or a contradictory active set.

Frozen identity binds source, arrangement, clock, renderer fields/receipt, and every stem's ID, instrument, role, declared gain/default state and complete file metadata. Comparison ignores object key ordering and requires exact keys/values. Changing a file hash under the same arrangement SHA therefore cannot silently reuse an old mix. Transient mix changes never alter this identity. The complete backend manifest-byte digest remains a separate verification receipt.

Headroom depends on the independently verified retained PCM and declared ceilings. A safe complete sum alone does not prove safe subsets: opposing stems can cancel until one is muted. Backend positive/negative subset-extrema verification is the relevant control. These Node tests do not measure or establish that PCM property.

## Silent verification and limits

Run `node --test tests/test_stem_player.mjs`. On 16 October 2026, all 24 tests passed, with zero failures/skips, in 171 ms. They use stateful fake sources, gain automation and a fake audio clock; no AudioContext, browser, native audio device, downloaded fixture or audible output is used.

| Tested source | Bytes | SHA-256 |
| --- | ---: | --- |
| `web/stem-player.js` | 16,635 | `3d3568a4b676360238ae54d0d8e31d29ff577b668ad99d74e87f96d4acb3d908` |
| `tests/test_stem_player.mjs` | 20,686 | `3b2c396a2108c1870e667fadb0ce9473838a2da98142b3a04db49071f66718b0` |

Checks cover shared scheduling/offsets, source-origin math, complete tail duration, channel routing and interpolated mute ramps, all-muted clock continuity, original gain ceilings, seek/pause/resume, rapid repeated play/seeks, pending-resume cancellation, stale rejection/end handlers, delayed endpoint delivery, partial allocation/start cleanup, snapshot export/reload and immutable defaults, changed render identity rejection, malformed/missing buffers, explicit memory refusal and context ownership.

An isolated in-memory historical source variant preserves the synchronous-resume race: removing the microtask before caller `resume()` reproduces a rejected promise cached as the next play request. The variant test confirms that old retry failure; the production regression confirms successful retry. No defective variant is activated or stored as production code.

Performer mix changes ramp; pause, stop and seek stop owned sources immediately. Their audible discontinuity/click behavior remains unverified. Fake automation tests establish requested values and time relationships, not audible quality, actual hardware start precision, sample equality, no-click behavior or a measured latency budget. The 20 ms scheduling lead is a design choice, not measured device evidence.

Remaining acceptance includes production wiring with verified manifest/PCM decoding, actual browser audio execution, standalone-vs-stem combined comparison at the declared comparison point, measured subset headroom, real listening, real phone/laptop offline/background/lock-screen/interruption behavior, full-song memory/streaming and sustained clock/seek/tail checks. No full-product, musical-fidelity or device acceptance follows from this component.
