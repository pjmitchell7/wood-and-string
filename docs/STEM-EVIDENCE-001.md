# Synchronized stem evidence, version1

This is a standalone backend contract and silent authored-PCM validation component. It is not wired to the Library, renderer, server or player. No feature is enabled, no existing result changes, and no real recording, full song or device is accepted by these tests.

`stem_evidence.py` validates a synthesized performer/percussion bundle and its default combined export. Analysis stems and original/master audio are excluded from the declared input kind. A source hash, instrument label or trusted-renderer receipt hash cannot establish that a file contains no original vocals: the verifier blocks known excluded byte identities but reports content exclusion and renderer execution **unverified**. Actual controlled rendering/lineage integration remains necessary.

## Shared manifest

`version` is `stem-bundle-v1`. Required objects:

- `source: {id,sha256}` identifies the unchanged original.
- `clock: {sample_rate,origin_sample,body_frames,tail_frames,frames}` uses44100Hz, safe nonnegative JavaScript integer samples and `frames=body_frames+tail_frames`. Every file covers that complete identical interval. There is no per-stem offset, leading trim or independently shortened tail field.
- `arrangement: {revision,sha256,performers,label}` binds musical notes/settings/ledger, one to three pitched performers and a bounded descriptive label. Labels can describe Keep close, Essentials or Reimagine; these are interpretations, not additional instrument modes. An Essential omission belongs in the bound arrangement ledger, separately from a recognition failure.
- `renderer: {id,version,receipt_sha256,source_kind,content_exclusion}` requires `source_kind:"synthesized"` and `content_exclusion:"declared_not_verified"`. Its receipt is a caller-trusted lineage reference, not independently inspected here.
- `stems` contains one to six parts, including at most three percussion groups. Each has `{id,instrument,role,gain,default_audible,file,provenance}`. Instrument is the output acoustic/electric/bass/drums choice, not an assertion about the original source instrument. Roles may be composite: one guitarist can carry several voices. Gain is finite0–1 and default-audible is Boolean.
- Each `file` is `{name,bytes,sha256,format,channels,sample_rate,frames}`. V1 requires an ordinary single-link file, safe unique basename, canonical44-byte-header stereo PCM16 WAV, and exact common frame count, length and complete SHA. No arbitrary WAV chunks, codecs or resampling are accepted.
- `provenance: {source_ids,assignment,adaptations,newly_composed,source_kind}` is an orthogonal declared summary. Source IDs reference evidence/voice records; detailed per-event lineage stays in the arrangement identified by its SHA. Redistribution/adaptation need not mean newly composed, and unknown source instrument/estimated evidence remain expressible. This checker does not certify those musical claims.
- `combined: {file,mix_point,active_stem_ids,quantization,tolerance_lsb}` requires `linear_pre_master`, the exact set of default-audible parts, `sum_retained_pcm16_then_nearest_ties_even`, and tolerance1LSB against that nearest integer. It is derived from **retained PCM stems**, not an independently quantized floating-point master.

## API and resource boundary

`validate_manifest(value, *, max_frames=31*44100, max_total_audio_bytes=64MiB)` returns detached validated metadata without opening media. `parse_manifest(bytes, **policy)` also rejects duplicate JSON keys, trailing data and nonfinite values. The schema supports later bounded streaming without a new timing format; explicit caller policy may grow only within900seconds/2GiB hard ceilings. Those ceilings are not a disk reservation, measured throughput or authorization under the existing7/5/8GiB research limits. Current tests use tiny authored fixtures; production/full-song admission remains separate.

`protect_bundle(folder, manifest, *, expected_binding, expected_manifest_sha256=None, forbidden_audio_sha256=(), cancel=..., **policy)` verifies and holds all files with Windows read-share-only handles until context exit. Expected binding contains the source/clock/arrangement/renderer objects from caller-trusted state. An optional saved SHA checks the canonical **full manifest**, including file hashes, roles, gains and default mix. This is the render identity; the musical arrangement SHA alone is insufficient. The manifest canonical JSON uses sorted keys, compact separators and UTF-8.

The context yields `{manifest,verification}`. `verify_bundle(...)` returns that completed snapshot after closing handles. Callers own resource admission, immutable storage and any staging/job lock; this component publishes, copies and deletes nothing. File integrity is verified through the held context, not promised forever after handles close. Ordinary path and hard-link checks follow the existing evidence style. No POSIX sharing guarantee is supplied.

## Linear mix and live state

Verification streams4096-frame chunks. It checks the default export against the declared constant-gain sum of stored stems. Reported integer error versus nearest quantization and fractional residual versus the unrounded sum are separate; a1LSB nearest-integer allowance can allow up to1.5LSB fractional residual. No normalization or automatic loudness compensation occurs in this verifier. It cannot establish whether a trusted producer normalized individual stems earlier.

For every frame/channel it also sums positive and negative contributions separately at declared maximum gains. These extrema bound **every mute subset and every gain reduction0–declared gain**. Any possible PCM overload is rejected, even if canceling parts make the full mix safe. A future player must respect those gain ceilings and keep its master gain at or below1, or perform a separate admission check. Shared nonlinear effects lie after this declared comparison point and cannot be verified by the fixed linear export.

Live mute/gain state is separate from this immutable manifest. Muting the second performer never changes a Duo revision into Solo. Switching to a complete Solo or Essentials revision requires a different bound arrangement and a separately tested musical transition. The coordinated browser component accepts already-validated buffers and checks their identities/geometry; metadata checks alone do not prove browser byte/hash verification or actual device playback.

## What a pass means

The pass verifies exact file identities, common geometry, declared linear mix and safe subset range. It does not prove physical source alignment, untrimmed acoustic attacks, correct note recognition, meaningful roles, articulation/sustain quality, vocal exclusion, renderer execution or musical naturalness. A shifted stem with an unchanged combined export is detected by the mix check; coherently shifted/rehashed files require the previously saved full-manifest identity to reject replacement. Content interpretation still requires trusted source/render evidence and descriptive listening.

Silent tests cover actual tiny stereo files, nonzero origin, nonzero tails, muting, byte and header tampering, resource boundaries, gain/sum failures, a safe full mix with unsafe muted subsets, stale render identities, excluded source/master hashes, hard links, active writer denial and cancellation cleanup. No real bank, model, native audio process or playback is used.
