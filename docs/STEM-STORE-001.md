# Private immutable acoustic companion store

16 October 2026. `stem_store.py` supplies the next private storage boundary for the acoustic capture helper. It creates no Library asset rows, job updates, public URLs, browser controls or playback feature. Existing jobs and accepted immutable results remain unchanged. The caller retains `worker.lock`, resource admission and the eventual job-result commit.

## Concrete integration API

```python
from stem_store import StemStore, CompanionStoreError

store = StemStore(library)
capture_folder = store.capture_folder(job_id)
# Exactly Library.stage / (canonical_job_UUID + '.stem-capture').
# Construct the optional AcousticStemCapture at this fresh folder.

try:
    reference = store.publish(
        job_id,
        expected_binding=trusted_binding,  # source, clock, arrangement, renderer
        expected_manifest_sha256=full_canonical_manifest_sha256,
        expected_renderer_receipt_sha256=actual_saved_renderer_receipt_sha256,
        max_total_audio_bytes=admitted_audio_byte_cap,
        max_publish_bytes=admitted_publication_copy_cap,
        forbidden_audio_sha256=known_excluded_source_audio_hashes,
        cancel=cancelled,
    )
except CompanionStoreError as error:
    # Optional companion failure only. Keep the normal master preview/result.
    # error.cancelled distinguishes cancellation; error.phase is bounded.
    # A path-free error.pending_stage ledger can support controlled cleanup.
    pass
# After successful publication and final verification, the caller may commit
# reference in THIS new result while still holding its worker lock.
```

The caller supplies the actual original source ID/SHA, source clock, immutable arrangement revision/hash and renderer identity. Both expected hashes are mandatory, lowercase, complete SHA-256 values. The full canonical manifest SHA is the `bundle_id`; the arrangement SHA is a distinct musical identity. A capture's prior `admitted` flag or saved verifier receipt is insufficient publication authority.

`store.verify(reference, **context)` reopens and rechecks an existing companion and returns detached `reference`, `manifest`, `renderer_receipt` and `verification` metadata. Here `context` contains the same trusted binding, two expected hashes, explicit `max_total_audio_bytes`, optional excluded hashes and cancellation callback used for publication. It does not include `max_publish_bytes`.

`with store.protect(reference, **context) as checked:` additionally provides `checked['audio']`, mapping the fixed audio basenames to protected binary readers. Those readers remain valid only inside that context. A future checked-job/media route must separately authorize the requested job and player before using these readers. Caller-supplied filesystem paths or URLs are never accepted from references.

`validate_reference(reference)` checks reference shape and returns a detached copy; it does not verify stored media. References contain only version, full manifest address, bounded hash/byte records for all metadata, and fixed audio basenames with their hashes/bytes. They contain no local absolute paths. Internal protected readers are never part of a saved reference.

## Publication and immutable identity

Only `Library.stage/<canonical UUID>.stem-capture` is accepted as an input. The store does not query job state or acquire locks; the caller controls those checks. It parses the exact saved canonical manifest and renderer receipt through Windows readers that deny write/delete access. It verifies both supplied hashes and the current acoustic producer schema, then uses `stem_evidence.protect_bundle` to reverify the trusted binding, canonical full-manifest identity, canonical stereo PCM16 WAVs, complete common interval, exact default mix within the backend's one-LSB tolerance and safe retained-PCM mute subsets/gain reductions.

Source metadata and all source WAV readers remain held throughout copying, staged verification and final verification. Only `performer-1.wav`, optional `performer-2.wav`, `combined.wav`, canonical `manifest.json`, the bound `renderer-receipt.json` and a freshly generated canonical `verification.json` are copied. Float64 scratch, source recordings, unrelated files, old diagnostics and the capture's old verifier receipt are excluded. The original recording and standalone rendered master are never opened for writing or consumed by this store.

The fresh verifier receipt uses the strict 31-second maximum and the actual admitted audio sum as its backend byte policy. Caller caps are checked separately before that verification. This makes the immutable receipt/reference deterministic across retries with different adequate caller caps.

The private destination is `Library.root/stem-bundles/<full manifest SHA>`. Each call creates a fresh `.publish-<UUID>` directory within that namespace, writes bounded files exclusively, flushes them, verifies the entire staged bundle and checks file identities before a Windows no-replace directory rename. The final directory is reverified before any reference is returned. Existing content addresses are fully verified and reused; damaged, linked, incomplete or foreign occupied addresses are preserved and refused. An existing valid blob is never overwritten or repaired in place.

No DB transaction is part of publication. If a later caller-owned DB commit fails, or cancellation arrives after the atomic rename, a complete unreferenced blob remains available for a verified retry. No successful reference is returned on cancellation. The store never appends a companion to an old accepted result or removes complete blobs to roll back a failed job commit.

## Bounds, cleanup and evidence limits

The schema pins are `acoustic-stem-store-v1`, `stem-bundle-v1`, `acoustic-stem-capture-v1`, renderer ID `sfizz-meta-acoustic` and renderer version `sfizz-freewheel-live-quality-v1`. Exactly Acoustic Solo or Acoustic Duo is supported, with ordered complete performers, all default audible at gain one, 44100 Hz stereo PCM16 and the producer's shared one-second tail. Total duration is at most 1,367,100 frames, including tail. Three performers, percussion, electric stems, alternate mix points, missing performers or a changed renderer schema are refused.

Per-call `max_total_audio_bytes` and `max_publish_bytes` are mandatory positive integer policies. Hard audio and whole-bundle bounds are 16,405,332 and 18,649,940 bytes. Manifest, renderer receipt and fresh verifier receipt are capped at 128 KiB, 2 MiB and 16 KiB respectively. Metadata is bounded before reading; WAVs are streamed in 64 KiB chunks. No audio cache or full directory load is used. Final-directory membership checks stop after the fixed expected member count plus the first unexpected entry.

After exact source verification and cap admission, `library.space(required_copy_bytes)` enforces the Library's existing 8 GiB free-space reserve before any copy or new stage. A verified duplicate requests zero new copy bytes. Neither this check nor the caller's resource admission reserves physical disk against another process. Files are flushed, but no power-loss durability or portable POSIX sharing/rename guarantee is claimed. Other platforms fail closed.

Failure cleanup uses only the call's owned stage ledger: directory identity, fixed member names, file identities, sizes, timestamps and actual copied-byte hashes. It removes a member only when those checks still match, and removes an empty directory with a nonrecursive operation. Replaced, modified, linked, unknown and unrelated paths survive. A bounded path-free ledger is available as `error.pending_stage` when a copy/stage failure occurs. While holding its lock, a caller may retry exactly that ledger with `store.cleanup_stage(record)`; this performs no namespace scan. Unknown crash stages without a retained ownership ledger remain untouched. A complete published bundle is never a cleanup-stage target.

The renderer receipt retains module hashes and captured float-stream digests, but this store copies no float streams and does not re-execute the renderer or attest a sample bank. The caller's expected renderer-receipt SHA binds those historical declarations exactly. Native execution, source-clock acoustic alignment, source/vocal exclusion, musical fidelity and human listening acceptance remain unverified. This storage checkpoint does not activate companion playback or alter the master render.

## Silent authored tests

`python -B -m unittest discover -s tests -p test_stem_store.py -v` uses temporary authored PCM, the existing pure quantizer/capture helper and a fake Library that rejects any DB access. It uses no bank, native calls, model, browser, audio device, network, download, provider or process control.

The focused final run passed 15 tests with no failures/skips. Checks cover valid complete Solo/Duo tails/default mixes, fresh verifier replacement, protected source/media readers, exact metadata preservation, immutable duplicate reuse, reference and all binding mismatches, changed WAV/manifest/renderer/verifier receipts, unsupported renderer/roster/duration/schema, original/master preservation, excluded-source hashes, hard links, pre-copy byte/reserve refusal, injected partial copy/cancellation, changed/foreign-stage cleanup preservation, occupied-stage refusal, atomic destination races and complete orphan preservation after cancellation or a later caller commit failure. These tests establish the bounded local store contract; production worker/media/browser wiring and native/listening acceptance remain separate.
