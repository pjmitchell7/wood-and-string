# Optional acoustic performer capture

16 October 2026. This capture/producer helper makes no native calls or sample loads itself. A subsequent optional renderer hook can now supply its observations; job/Library/browser integration and feature activation remain separate. ACOUSTIC-STEM-RENDER-001 records that source and silent-test checkpoint.

`audio/acoustic_stem_capture.py` observes the existing acoustic render's native instance outputs. It never renders a performer again, normalizes a performer separately, changes notes or returns a replacement master. The caller continues the existing master addition order, float32 spool and PCM16 writer unchanged. The existing generated WAV becomes the companion's combined export by a protected read-only copy. The original recording is never copied or opened by this helper.

## Concrete API

`forecast_capture(performers=N, frames=F)` returns conservative extra staging/publication-copy disk bounds. `N` is one or two and `F` is at most `31*44100`. The caller must separately admit resources and hold its job lock. The forecast does not inspect disk space or reserve bytes.

```python
plan = forecast_capture(performers=len(players), frames=total)
capture = AcousticStemCapture(
    fresh_capture_folder,
    source={"id": original_source_id, "sha256": original_source_sha256},
    arrangement=already_assigned_arrangement,
    revision=immutable_job_revision,
    performers=players,                 # Exactly [1] or [1, 2].
    origin_sample=origin,
    total_frames=total,
    max_work_bytes=plan["additional_disk_bytes"],
    cancel=cancelled,
)

# Inside the EXISTING block/instance loops, after each native instance call:
capture.begin_block(start, size)
for player, string, left, right in existing_instance_outputs:
    capture.add_instance(player, string, left, right)
    # Keep the existing master accumulation statements and order unchanged.
capture.end_block(existing_float32_master_values)  # Before any byteswap.

# Only after the existing standalone WAV and normal renderer metrics exist:
outcome = capture.finish(
    existing_generated_wav,
    raw_peak=original_master_raw_peak,
    global_gain=original_master_gain,
    renderer_metrics=normal_renderer_metrics,
)
```

The loop is schematic: observe each native buffer before its next reuse; do not retain reused native-buffer references or move native calls. The helper's `instances` correspond to the renderer's existing sorted `(player,string)` instance order. Do not use the optional helper to drive the native render or choose its roster.

The optional `audio.render.render(..., stem_capture=capture)` hook performs those observations and first calls `bind_render(arrangement, origin_sample=origin, total_frames=total)`. That compares the helper's canonical snapshot and common interval with the actual renderer input. A mismatch refuses only the companion. The caller still finalizes and cleans up; the renderer's ordinary return is unchanged.

`begin_block`, `add_instance` and `end_block` return Boolean success. An operational, validation or cancellation failure makes the observer sticky `refused`/`cancelled`; later observation calls do nothing. The constructor also returns a refused object for invalid admission rather than making the normal render fail. `outcome` provides a detached status/diagnostic snapshot. `finish` returns either that refusal or `status:"admitted"` with the manifest, verification, actual renderer receipt, forecast and owned folder. Exceptions in companion processing are contained; the caller must still inspect status before publishing any companion reference.

`cleanup(keep_diagnostic=True)` closes owned float writers and removes only files this object exclusively created whose path/file identities still match. It uses no recursive deletion. Foreign, replaced, linked and unrelated paths are preserved. A refusal's bounded `diagnostic.json` remains by default. Call cleanup after successful verified publication or an abandoned attempt; the helper never changes the Library, publishes a bundle, updates a job or removes the standalone WAV.

## Identity, levels and endpoints

Admission snapshots the actual caller source ID/SHA and the canonical complete assigned arrangement. It verifies that both arrangement/source-event ledgers use that source ID and 44100 Hz, every assigned command references the retained effective source ledger, and assigned performers exactly match the requested roster. A missing second performer refuses a Duo companion; it does not invent a silent or duplicated part. One Solo stem contains all assigned strings and roles of its one performer.

The endpoint must equal the existing renderer's `max(note.end)-origin+44100`. The declared body ends at its last assigned note gate and its tail is exactly one second, matching that existing implementation. It does not substitute the requested passage end or prove that this fixed tail captures all audible sample decay. Every native instance must appear exactly once in every contiguous block, including silent intervals and the complete shared tail. Gaps, overlaps, reordered instances and nonfinite contributions refuse capture.

Performer sums are stored as stereo float64 to preserve the Python accumulation precision of native float contributions. The existing float32 master blocks are hashed and their peak measured without mutation. Finish requires that measured peak to equal the caller's original raw peak, verifies the common gain exactly against the existing `peak` or `legacy_fixed` policy, and checks its gain-dB receipt. Supply the original scalar or recompute the exact existing policy expression from its raw peak; do not derive it through a gain-dB round trip.

Every performer uses that single common gain and the existing PCM16 nearest/ties-even conversion with full-scale refusal. The manifest gain ceilings are one because the common scalar is already baked into these retained stem samples. There is no independent normalization, clipping, retiming, trim, new note, damping decision or waveform change to the existing combined export. Float-stream digests recorded while observing are checked again during encoding.

The actual canonical renderer receipt binds source, arrangement revision/hash, complete captured command/role ledger, native instance order, captured float digests, observed master float digest, common clock, measured peak/scalar, normal renderer metrics and exact renderer/helper module hashes. Its SHA is computed from its saved bytes; callers cannot supply an arbitrary receipt hash. Native execution, acoustic alignment, original/vocal exclusion and musical acceptance remain unverified. This receipt records caller-owned execution declarations and observed data; it is not a fresh independent bank or native-process attestation.

`stem_evidence.verify_bundle` checks the exact source/clock/arrangement/renderer binding, canonical full manifest SHA, retained files, default linear mix, and every mute subset/nonnegative gain reduction at retained PCM sample points. The saved manifest, renderer receipt and verification JSON are also bounded, protected-read and checked against their canonical byte digests before admission. The normal combined WAV is independently quantized, so a one-LSB stem/mix discrepancy can occur. The existing verifier's tolerance governs admission; the helper never rewrites the master to obtain a pass. Cancellation between performers can also conceal individual overload after master normalization. Either problem refuses only this optional companion. Downstream master gain/EQ/effects, device resampling and inter-sample peaks require separate evidence.

## Resource and commit boundary

For `N` performers and `F` frames, each performer float stream is `16*F` bytes. Each stereo PCM16 WAV is `44+4*F` bytes. Metadata allowances are 2 MiB renderer receipt, 128 KiB manifest, 16 KiB verification and 8 KiB diagnostic. The helper forecasts:

- Staging: `N*16*F + (N+1)*(44+4*F) + metadata`.
- Extra disk including one later publication copy: `N*16*F + 2*((N+1)*(44+4*F) + metadata)`.
- Existing master float/PCM allowance, separately: `12*F+44`.
- Capture numeric block payload: at most 10,240 bytes for Duo at 256 frames, excluding Python/container overhead, bounded arrangement/receipt snapshots, native buffers, caller inputs and runtime overhead.

The explicit `max_work_bytes` must cover the extra disk forecast before a fresh folder is created. Its later publication-copy allowance covers WAVs and bounded metadata; the `.f64` files remain scratch and must not also be copied under this forecast. This is a caller byte allowance, not a disk reservation or proof that another process cannot consume free space. The caller should check its Library reserve/admission before capture and publication. Write failures refuse the companion and preserve the normal output. Per-block writes, PCM conversion, copying and backend verification check cancellation. Only one/two complete performers and the 31-second hard duration are supported.

The caller must publish verified companion copies while it still owns the job lock, verify the final immutable copy, and only then commit its reference and complete normal job result. The existing preview asset/result must survive a companion refusal with a separate bounded diagnostic. Do not make optional refusal enter the worker's general preview-failure handler. A caller must define cancellation/commit ordering explicitly, and not append a later companion into a previously accepted immutable result. Keep the full manifest digest separate from the musical arrangement SHA and from a saved playback mix. Existing cached jobs without companions need explicit new work or availability handling.

No flat Library media rows, media route, saved mix controls, offline downloads or browser decoding are added here. Future wiring can use the existing PCM16 loader and saved settings keyed by the full manifest SHA, while enforcing player identity and gain ceilings. Its destination must separately respect the verified comparison point.

## Silent authored verification

`python -B -m unittest discover -s tests -p test_acoustic_stem_capture.py -v` is the focused test command. Tests use authored float contributions and the existing pure PCM16 writer; no sample bank, native renderer, AudioContext, browser, audio device, model or network is used.

The final focused run passed all 13 tests, with zero failures/skips, in 5.633 seconds on 16 October 2026. Independent source review found a metadata admission gap before handoff; protected saved-JSON rereads and a tampering regression now cover it.

Checks cover unchanged standalone WAV bytes with capture enabled/disabled; actual independent backend re-verification; complete polyphonic Solo/Duo identity and nonzero tail; real saved receipt/stream hashes; a safe master hiding individual overload; wrong retained combined mix; captured-float and all saved-JSON tampering; cancellation during capture and protected verification; injected write failure; foreign-file preservation and idempotent cleanup; missing/reordered native contributions; absent second performer; source mismatch; common gain mismatch; and exact maximum forecast refusal. The fixtures retain only eleven body frames plus the existing one-second tail. These checks establish the authored observer/producer boundary, not production wiring, native repeatability, browser/device behavior, musical fidelity or listening acceptance.
