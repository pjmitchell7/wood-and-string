# Saved acoustic parts in previews

16 October 2026. New acoustic preview code now captures and saves each guitarist's part, serves verified parts for the completed job, and connects explicit Load, Play, mute and seek controls to the existing preview page. Silent integration checks pass. This source change still needs a fresh production worker trial and live browser/device playback; the running server has not been restarted.

## Preview generation

New mixed, refined and selected-instrumental acoustic requests carry `acoustic_companion_version=acoustic-companion-v1`. The renderer version and ordinary master sound remain unchanged. Historical request absence is preserved during replay, refinement reconstruction and queued dispatch. Electric and Jazz Bass do not acquire acoustic parts, and diagnostic/extraction jobs retain their existing paths.

The worker observes the final assigned arrangement during its single normal render. Capture binds the actual original ID/SHA, final canonical arrangement hash, job UUID revision, performer roster and source clock. It finishes with the pristine renderer metrics and exact common gain, then publishes through `StemStore` before `Library.publish` consumes the master. Later worker timing and process fields do not enter the capture receipt.

Fresh disk admission covers capture and its full publication copy alongside the normal render allowance. The maximum 31-second Duo forecast remains 81,063,464 additional bytes plus 16,405,244 normal bytes, with the Library's 8 GiB free-space reserve. Companion admission, capture or publication failure leaves the good master available. Genuine job cancellation still propagates.

The job stores a path-free immutable reference and the canonical arrangement hash. Refusals retain a simple reason, safe error type, known capture stage and bounded exact frame counters where available. Raw operating-system messages, paths and tracebacks are excluded. Cleanup removes only helper-owned scratch; unknown crash remnants and complete bundles left by a later database failure are preserved. Unknown remnants can require recovery attention rather than an automatic retry that deletes them.

## Saved job verification

`acoustic_companion.py` derives trusted context from the committed request/result and checks the actual original and master Library descriptors and bytes, final commands, arrangement revision, common clock and renderer receipt. Source and master readers remain protected throughout bundle verification and streaming. An invalid companion does not rewrite the saved job or invalidate its independently verified master.

Authenticated `GET /api/jobs/{id}/parts` returns bounded metadata. The fixed WAV members are `performer-1.wav`, optional `performer-2.wav` and `combined.wav`. Responses carry exact length/hash identities and no-store headers. The response owns the open verification context until success or disconnect, and the gateway closes upstream responses under cancellation. Unknown paths, missing versions, incomplete jobs and changed associations are refused. Gateway route/static allowlists include only the required new surfaces.

## Playback controls

The shell27 player mounts saved-part controls only when the server advertises the matching capability and the job has an available companion. Load verifies the committed context, manifest, fixed media routes, encoded lengths, WAV geometry and SHA256 before any audio context or nodes are created. Requests have byte limits, a 30-second deadline and stale-load cancellation.

Only explicit Play creates the owned context. The bridge reuses `transport.pcm16`, retaining 44100 Hz buffers even when the device context runs at 48000 Hz, and the existing scheduler starts both parts on one clock. Each player can be muted; All restores the saved performers. Muting one part keeps the Duo identity. A complete Solo, Essentials or a different role allocation requires another arrangement revision.

Main playback, native seeking and comparison cannot continue hidden part playback. Normal playback, comparison, recording, restore and sleep release the owned part audio while retaining an inert panel for explicit reload. Opening another result or leaving the page removes the old panel. Legacy jobs, unsupported servers and failed part loading retain ordinary master playback. Parts are not downloaded for offline storage in this slice. Mix choices are session controls and are not persisted. Existing mute ramps remain 15 ms; click-free pause/seek, browser autoplay behavior and hardware continuity still require actual playback checks.

## Validation

All automated checks were silent and used authored PCM, fake native calls or in-process HTTP. No new bank, model, browser window or audible playback was used.

| Check | Result | What it establishes |
| --- | --- | --- |
| Producer and worker controls | 21 passed; independently repeated | All three worker paths, frozen absence, disk/capture/publication refusal, cancellation, commit failure, saved association checks and bounded diagnostics |
| API and gateway controls | 14 passed; independently repeated | Authentication, fixed routes, unchanged accepted jobs, protected stream completion/disconnect and legacy queue compatibility |
| Existing backend compatibility | 116 plus 43 passed | Library/identity/gateway, mixed/refinement/instrumental/processing paths, pairing/preferences/snapshots and Electric compatibility |
| Eligible Node compatibility | 66 passed, zero skips/failures | Part scheduler/bridge plus existing player, queue, comparison, recording, ownership and offline shell checks |
| Actual Python fixture to JavaScript bridge | Passed | Backend metadata and three saved WAVs accepted at 44100 Hz, 44,111 frames, origin 987,654; four corruption/association controls refused; zero audio contexts |
| Resource accounting | Seven pure controls passed independently | The same 32 MiB pool, exact reserve reallocation and unchanged 7/5/8 GiB limits |

The first broad Node command included one browser-only module and exposed four outdated extracted-function harness dependencies plus one shell25 expectation. Its failed log is retained. Five harnesses were repaired without production changes, preserving their existing assertions and adding actual handoff checks. The browser-only module was excluded from the corrected Node command, not counted as a pass. The first cross-language runner also encountered its fixture's deliberate global process-launch guard; fixture teardown now precedes the separate Node reader. That failure is retained separately from the passing bridge result.

Independent review then found that normal playback/comparison removed the entire part panel while its job remained visible. The repaired unload path retains an explicit reloadable panel and removes old role-button handlers. Three additional controls exercise unload/reload, pending load/resume cancellation and actual main/comparison callbacks with a mounted bridge. The final complete Node batch passes66 controls in3.0283464seconds, followed by a passing backend-to-JavaScript unload/reload check with zero audio contexts. The original source and first passing bridge receipt/runners remain archived at their recorded hashes. Shell27 prevents a cached provisional shell26 from retaining the earlier lifecycle behavior.

The earlier six-cell real sampled-guitar comparison remains the evidence for unchanged master PCM and retained performer sums under the then-frozen renderer. It does not validate these later worker/API/player changes on real hardware, improve note recognition or establish a musically useful new duet. Private review receipts and logs are retained in `work/acoustic-parts-integration-001`; third-party recordings and derived audio remain outside public source.

## Next validation

Freeze a fresh current-source closure before a bounded production worker trial, with a reviewed peak disk allowance. Then verify loading, explicit playback, mute/seek/handoff and cleanup in one owned silent browser context before arranging a human listening check. SERVER-RESTART-POLICY-001 still governs the separately rejected server-start action. Automatic focus selection for Essentials, expression recovery, full songs, Electric/Bass/drums/Ensemble, local AI and the required real-device acceptance remain open.
