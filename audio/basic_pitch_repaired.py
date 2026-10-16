# Copyright 2022 Spotify AB
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Explicit research comparison: finite onset fallback and exclusive-end repair.

Derived from Spotify Basic Pitch commit fa5997af0a8210982619003269994a1be25eddf3.
The pinned upstream module and production behavior remain unchanged. This is
not a sustain, attack-classification, transcription-accuracy or music-quality
repair. It retains existing thresholds, policies and source-anchor timing.
Changes are the zero-difference onset fallback, exclusive Melodia endpoints,
the final forward-scan frame, and frame-zero backward-scan coverage.
"""
from typing import List, Tuple, Optional
import numpy as np
import scipy.signal

from .basic_pitch_upstream import constrain_frequency

VERSION = 'basic-pitch-event-repair-v1'
MAX_FRAMES = 4096
MIDI_OFFSET, MAX_FREQ_IDX = 21, 87
POLICIES = {
    'published_default': dict(min_note_len=11, infer_onsets=True, melodia_trick=True, energy_tol=11),
    'duration_matched': dict(min_note_len=0, infer_onsets=True, melodia_trick=True, energy_tol=11),
    'onset_head_ablation': dict(min_note_len=0, infer_onsets=False, melodia_trick=False, energy_tol=11),
}


def _validate_report(report):
    if type(report) is not dict or type(report.get('raw_predictions')) is not dict:
        raise ValueError('Expected retained Basic Pitch predictions')
    raw = report['raw_predictions']
    shapes = {}
    for key, width in (('note', 88), ('onset', 88), ('contour', 264)):
        value = raw.get(key)
        # Retained JSON rows and ordinary ndarrays are the supported inputs.
        # Inspect every dimension before conversion: a bounded row count alone
        # does not bound allocation for an excessively wide or nested matrix.
        if type(value) is np.ndarray:
            if (value.ndim != 2 or value.shape[1] != width or
                    value.dtype.kind not in 'biuf'):
                raise ValueError('Invalid prediction matrix: ' + key)
            count = value.shape[0]
        elif type(value) in (list, tuple):
            count = len(value)
            if not 3 <= count <= MAX_FRAMES:
                raise ValueError('Prediction frame count exceeds the bounded decoder')
            for row in value:
                if type(row) not in (list, tuple) or len(row) != width:
                    raise ValueError('Invalid prediction matrix row: ' + key)
                if any(type(item) not in (bool, int, float) and not
                        (isinstance(item, np.generic) and item.dtype.kind in 'biuf')
                        for item in row):
                    raise ValueError('Expected numeric prediction scalars: ' + key)
        else:
            raise ValueError('Invalid prediction matrix: ' + key)
        if not 3 <= count <= MAX_FRAMES:
            raise ValueError('Prediction frame count exceeds the bounded decoder')
        shapes[key] = count
    count = shapes['note']
    if any(value != count for value in shapes.values()):
        raise ValueError('Prediction matrices must share their source frames')
    rate, origin, length = (report.get(key) for key in ('source_rate', 'source_origin_sample', 'source_length'))
    if (type(rate) is not int or rate not in (22050, 44100, 48000) or type(origin) is not int
            or type(length) is not int or not 0 < length <= 30 * rate
            or not 0 <= origin <= 2**63 - 1 - length):
        raise ValueError('Expected a bounded integer source clock')
    from .basic_pitch import frame_samples
    if int(frame_samples(count, rate, 0)[-1]) >= length:
        raise ValueError('Prediction frames exceed the declared source passage')
    arrays = {}
    for key in shapes:
        try:
            array = np.asarray(raw[key])
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError('Invalid prediction matrix: ' + key) from error
        if (array.dtype.kind not in 'biuf' or not np.isfinite(array).all() or
                (array < 0).any() or (array > 1).any()):
            raise ValueError('Invalid finite prediction matrix: ' + key)
        arrays[key] = array.astype(np.float32, copy=False)
    return arrays


def decode_saved(report, policy='published_default'):
    """Decode retained matrices using only the explicitly versioned repairs.

    This remains an optional comparison, not the production/default decoder.
    Source-anchor timing is retained; physical attacks and damping are unknown.
    """
    import warnings
    from .basic_pitch import frame_samples, official_frame_samples
    if type(policy) is not str or policy not in POLICIES:
        raise ValueError('Unknown frozen policy')
    arrays = _validate_report(report)
    frames, onsets = arrays['note'], arrays['onset']
    with warnings.catch_warnings(record=True) as notices:
        warnings.simplefilter('always')
        events = _output_to_notes_polyphonic(frames.copy(),onsets.copy(),
            onset_thresh=.5,frame_thresh=.3,max_freq=None,min_freq=None,**POLICIES[policy])
        combined = _infer_onsets(onsets.copy(),frames.copy()) if POLICIES[policy]['infer_onsets'] else onsets
    rate=report['source_rate']; origin=report['source_origin_sample']
    # Clamp local anchors before adding the validated origin. NumPy int64
    # addition could wrap at a valid near-limit clock before a later clamp.
    anchors=[origin+min(int(sample),report['source_length'])
        for sample in frame_samples(len(frames)+1,rate,0)]
    # This un-clipped historical comparison can exceed the declared passage;
    # Python integers preserve its value without int64 wraparound.
    official=[origin+int(sample)
        for sample in official_frame_samples(len(frames)+1,rate,0)]
    evidence=[]
    coverage=np.zeros_like(frames,dtype=bool)
    for index,(start,end,pitch,amplitude) in enumerate(events):
        p=pitch-21
        direct=bool(0<start<len(frames)-1 and onsets[start,p]>=.5 and
                    onsets[start,p]>onsets[start-1,p] and onsets[start,p]>onsets[start+1,p])
        augmented=bool(0<start<len(frames)-1 and combined[start,p]>=.5 and
                      combined[start,p]>combined[start-1,p] and combined[start,p]>combined[start+1,p])
        coverage[start:end,p]=True
        evidence.append(dict(id=f'{policy}-{index}',start_frame=int(start),end_frame_exclusive=int(end),
            pitch=int(pitch),start=int(anchors[start]),end=int(anchors[end]),
            official_start=int(official[start]),official_end=int(official[end]),
            mean_note_activation=float(amplitude),raw_onset_activation=float(onsets[start,p]),
            onset_head_peak_at_start=direct,augmented_peak_at_start=augmented,
            start_evidence='head_peak_present' if direct else 'augmented_peak_present' if augmented else 'no_peak_at_start_Melodia_possible'))
    return dict(version=VERSION, policy=policy,settings=dict(onset_thresh=.5,frame_thresh=.3,**POLICIES[policy]),
        events=sorted(evidence,key=lambda e:(e['start'],e['pitch'])),
        unassigned_active_frame_pitch_count=int(((frames>=.3)&~coverage).sum()),
        nonfinite_augmented_onset_cells=int((~np.isfinite(combined)).sum()),
        warnings=sorted(set(str(w.message) for w in notices)),
        repairs=['preserve direct onsets when no positive frame differences exist', 'retain final source frame during forward scans', 'Melodia endpoints are exclusive', 'retain frame zero during the Melodia backward scan'],
        equivalence='Pinned event policy with only finite-onset and exclusive-end/EOF repairs; source anchors custom; no MIDI equivalence or music-quality claim',
        limitations=['One-frame endpoint repairs do not cure the observed approximately982ms GuitarSet truncation.', 'Repeated attacks, intentional rests, recording dropout and polyphonic contamination remain ambiguous.', 'Source frame anchors are not independently verified physical pluck or damping times.'])

def _infer_onsets(onsets: np.array, frames: np.array, n_diff: int = 2) -> np.array:
    """Infer onsets from large changes in frame amplitudes.

    Args:
        onsets: Array of note onset predictions.
        frames: Audio frames.
        n_diff: Differences used to detect onsets.

    Returns:
        The maximum between the predicted onsets and its differences.
    """
    diffs = []
    for n in range(1, n_diff + 1):
        frames_appended = np.concatenate([np.zeros((n, frames.shape[1])), frames])
        diffs.append(frames_appended[n:, :] - frames_appended[:-n, :])
    frame_diff = np.min(diffs, axis=0)
    frame_diff[frame_diff < 0] = 0
    frame_diff[:n_diff, :] = 0
    if np.max(frame_diff) == 0:
        return onsets.copy()  # Preserve observed peaks; no derivative onset exists.
    frame_diff = np.max(onsets) * frame_diff / np.max(frame_diff)  # rescale to have the same max as onsets

    max_onsets_diff = np.max([onsets, frame_diff], axis=0)  # use the max of the predicted onsets and the differences

    return max_onsets_diff

def _output_to_notes_polyphonic(
    frames: np.array,
    onsets: np.array,
    onset_thresh: float,
    frame_thresh: float,
    min_note_len: int,
    infer_onsets: bool,
    max_freq: Optional[float],
    min_freq: Optional[float],
    melodia_trick: bool = True,
    energy_tol: int = 11,
) -> List[Tuple[int, int, int, float]]:
    """Decode raw model output to polyphonic note events

    Args:
        frames: Frame activation matrix (n_times, n_freqs).
        onsets: Onset activation matrix (n_times, n_freqs).
        onset_thresh: Minimum amplitude of an onset activation to be considered an onset.
        frame_thresh: Minimum amplitude of a frame activation for a note to remain "on".
        min_note_len: Minimum allowed note length in frames.
        infer_onsets: If True, add additional onsets when there are large differences in frame amplitudes.
        max_freq: Maximum allowed output frequency, in Hz.
        min_freq: Minimum allowed output frequency, in Hz.
        melodia_trick : Whether to use the melodia trick to better detect notes.
        energy_tol: Drop notes below this energy.

    Returns:
        list of tuples [(start_time_frames, end_time_frames, pitch_midi, amplitude)]
        representing the note events, where amplitude is a number between 0 and 1
    """

    n_frames = frames.shape[0]

    onsets, frames = constrain_frequency(onsets, frames, max_freq, min_freq)
    # use onsets inferred from frames in addition to the predicted onsets
    if infer_onsets:
        onsets = _infer_onsets(onsets, frames)

    peak_thresh_mat = np.zeros(onsets.shape)
    peaks = scipy.signal.argrelmax(onsets, axis=0)
    peak_thresh_mat[peaks] = onsets[peaks]

    onset_idx = np.where(peak_thresh_mat >= onset_thresh)
    onset_time_idx = onset_idx[0][::-1]  # sort to go backwards in time
    onset_freq_idx = onset_idx[1][::-1]  # sort to go backwards in time

    remaining_energy = np.zeros(frames.shape)
    remaining_energy[:, :] = frames[:, :]

    # loop over onsets
    note_events = []
    for note_start_idx, freq_idx in zip(onset_time_idx, onset_freq_idx):
        # if we're too close to the end of the audio, continue
        if note_start_idx >= n_frames - 1:
            continue

        # find time index at this frequency band where the frames drop below an energy threshold
        i = note_start_idx + 1
        k = 0  # number of frames since energy dropped below threshold
        while i < n_frames and k < energy_tol:
            if remaining_energy[i, freq_idx] < frame_thresh:
                k += 1
            else:
                k = 0
            i += 1

        i -= k  # go back to frame above threshold

        # if the note is too short, skip it
        if i - note_start_idx <= min_note_len:
            continue

        remaining_energy[note_start_idx:i, freq_idx] = 0
        if freq_idx < MAX_FREQ_IDX:
            remaining_energy[note_start_idx:i, freq_idx + 1] = 0
        if freq_idx > 0:
            remaining_energy[note_start_idx:i, freq_idx - 1] = 0

        # add the note
        amplitude = np.mean(frames[note_start_idx:i, freq_idx])
        note_events.append(
            (
                note_start_idx,
                i,
                freq_idx + MIDI_OFFSET,
                amplitude,
            )
        )

    if melodia_trick:
        energy_shape = remaining_energy.shape

        while np.max(remaining_energy) > frame_thresh:
            i_mid, freq_idx = np.unravel_index(np.argmax(remaining_energy), energy_shape)
            remaining_energy[i_mid, freq_idx] = 0

            # forward pass
            i = i_mid + 1
            k = 0
            while i < n_frames and k < energy_tol:
                if remaining_energy[i, freq_idx] < frame_thresh:
                    k += 1
                else:
                    k = 0

                remaining_energy[i, freq_idx] = 0
                if freq_idx < MAX_FREQ_IDX:
                    remaining_energy[i, freq_idx + 1] = 0
                if freq_idx > 0:
                    remaining_energy[i, freq_idx - 1] = 0

                i += 1

            i_end = i - k  # exclusive boundary after the final supported frame

            # backward pass
            i = i_mid - 1
            k = 0
            while i >= 0 and k < energy_tol:
                if remaining_energy[i, freq_idx] < frame_thresh:
                    k += 1
                else:
                    k = 0

                remaining_energy[i, freq_idx] = 0
                if freq_idx < MAX_FREQ_IDX:
                    remaining_energy[i, freq_idx + 1] = 0
                if freq_idx > 0:
                    remaining_energy[i, freq_idx - 1] = 0

                i -= 1

            i_start = i + 1 + k  # go back to frame above threshold
            assert i_start >= 0, "{}".format(i_start)
            assert i_end <= n_frames

            if i_end - i_start <= min_note_len:
                # note is too short, skip it
                continue

            # add the note
            amplitude = np.mean(frames[i_start:i_end, freq_idx])
            note_events.append(
                (
                    i_start,
                    i_end,
                    freq_idx + MIDI_OFFSET,
                    amplitude,
                )
            )

    return note_events
