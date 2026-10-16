#!/usr/bin/env python
# encoding: utf-8
#
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

"""Pinned upstream event functions, copied verbatim; research only.
Source revision fa5997af0a8210982619003269994a1be25eddf3.
Source file SHA256 83346193f834edfc37aeadbed91a4092b574727dd2b4535e03e5ad89d2e89506.
Full source LICENSE/NOTICE retained in audio/research/basic-pitch/notices.
No frequency filtering is exposed; librosa is unused on this path.
"""
from typing import List, Tuple, Optional
import numpy as np
import scipy.signal
MIDI_OFFSET=21
MAX_FREQ_IDX=87

# Frozen before comparison002 outcomes. Only event decoding, not MIDI writing.
POLICIES = {
    'published_default': dict(min_note_len=11, infer_onsets=True, melodia_trick=True, energy_tol=11),
    'duration_matched': dict(min_note_len=0, infer_onsets=True, melodia_trick=True, energy_tol=11),
    'onset_head_ablation': dict(min_note_len=0, infer_onsets=False, melodia_trick=False, energy_tol=11),
}


def decode_saved(report, policy='published_default'):
    """Wrap verbatim upstream event functions; retain our source anchor mapping.

    Not full upstream MIDI equivalence: no frequency filters or pitch-bend/MIDI
    conversion. Warning behavior (including silent-input division) is recorded.
    Raw inputs copied because upstream frequency constraints mutate arrays.
    """
    import warnings
    from .basic_pitch import frame_samples, official_frame_samples
    if policy not in POLICIES:
        raise ValueError('Unknown frozen policy')
    raw = report['raw_predictions']
    frames = np.asarray(raw['note'],dtype=np.float32)
    onsets = np.asarray(raw['onset'],dtype=np.float32)
    if frames.ndim != 2 or frames.shape != onsets.shape or frames.shape[1] != 88 or len(frames)<3:
        raise ValueError('Expected retained note/onset matrices')
    if not np.isfinite(frames).all() or not np.isfinite(onsets).all():
        raise ValueError('Non-finite activations')
    with warnings.catch_warnings(record=True) as notices:
        warnings.simplefilter('always')
        events = output_to_notes_polyphonic(frames.copy(),onsets.copy(),
            onset_thresh=.5,frame_thresh=.3,max_freq=None,min_freq=None,**POLICIES[policy])
        combined = get_infered_onsets(onsets.copy(),frames.copy()) if POLICIES[policy]['infer_onsets'] else onsets
    rate=report['source_rate']; origin=report['source_origin_sample']
    limit=origin+report['source_length']
    anchors=np.minimum(frame_samples(len(frames)+1,rate,origin),limit)
    official=official_frame_samples(len(frames)+1,rate,origin)
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
    return dict(policy=policy,settings=dict(onset_thresh=.5,frame_thresh=.3,**POLICIES[policy]),
        events=sorted(evidence,key=lambda e:(e['start'],e['pitch'])),
        unassigned_active_frame_pitch_count=int(((frames>=.3)&~coverage).sum()),
        nonfinite_augmented_onset_cells=int((~np.isfinite(combined)).sum()),
        warnings=sorted(set(str(w.message) for w in notices)),
        equivalence='verbatim pinned event functions; source anchors custom; no upstream MIDI/pitch-bend equivalence claim')

def get_infered_onsets(onsets: np.array, frames: np.array, n_diff: int = 2) -> np.array:
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
    frame_diff = np.max(onsets) * frame_diff / np.max(frame_diff)  # rescale to have the same max as onsets

    max_onsets_diff = np.max([onsets, frame_diff], axis=0)  # use the max of the predicted onsets and the differences

    return max_onsets_diff

def constrain_frequency(
    onsets: np.array, frames: np.array, max_freq: Optional[float], min_freq: Optional[float]
) -> Tuple[np.array, np.array]:
    """Zero out activations above or below the max/min frequencies

    Args:
        onsets: Onset activation matrix (n_times, n_freqs)
        frames: Frame activation matrix (n_times, n_freqs)
        max_freq: The maximum frequency to keep.
        min_freq: the minimum frequency to keep.

    Returns:
       The onset and frame activation matrices, with frequencies outside the min and max
       frequency set to 0.
    """
    n_freqs = onsets.shape[1]
    min_freq_idx = 0
    max_freq_idx = n_freqs

    if min_freq is not None:
        min_freq_idx = int(np.round(librosa.hz_to_midi(min_freq) - MIDI_OFFSET))
    if max_freq is not None:
        max_freq_idx = int(np.round(librosa.hz_to_midi(max_freq) - MIDI_OFFSET))

    onsets[:, :min_freq_idx] = 0
    frames[:, :min_freq_idx] = 0
    onsets[:, max_freq_idx:] = 0
    frames[:, max_freq_idx:] = 0

    return onsets, frames

def output_to_notes_polyphonic(
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
        onsets = get_infered_onsets(onsets, frames)

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
        while i < n_frames - 1 and k < energy_tol:
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
            while i < n_frames - 1 and k < energy_tol:
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

            i_end = i - 1 - k  # go back to frame above threshold

            # backward pass
            i = i_mid - 1
            k = 0
            while i > 0 and k < energy_tol:
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
            assert i_end < n_frames

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
