"""Instrument contracts and explicit local bank verification; no acquisition."""
from dataclasses import dataclass
from pathlib import Path
import hashlib
import re
import zipfile

VERSION = 2
ROOT = Path(__file__).resolve().parent

@dataclass(frozen=True)
class Voice:
    id: str
    instrument: str
    responsibility: str
    max_polyphony: int

@dataclass(frozen=True)
class Mode:
    name: str
    performers: int
    drums: str
    voices: tuple[Voice, ...]


def mode(name, performers, drums='none'):
    """Validate target mode independently of whether its banks are installed."""
    if name not in ('acoustic','electric','bass','ensemble'):
        raise ValueError('Unknown instrument mode')
    if type(performers) is not int or performers not in ((3,) if name=='ensemble' else (1,2)):
        raise ValueError('Choose one/two instruments, or exactly three for ensemble')
    if drums not in ('none','hats','snare','both') or (name=='acoustic' and drums!='none'):
        raise ValueError('Acoustic mode has no drums; other modes allow hats, snare or both')
    if name=='ensemble':
        voices=[Voice('acoustic-1','acoustic','chord-rhythm',6),
                Voice('electric-1','electric','melody',6),Voice('bass-1','jazz-bass','foundation',1)]
    else:
        instrument={'acoustic':'acoustic','electric':'electric','bass':'jazz-bass'}[name]
        roles=('melody','accompaniment') if name!='bass' else ('upper-counterline','foundation')
        if name=='bass' and performers==1: roles=('melody',)
        voices=[Voice(f'{name}-{i+1}',instrument,roles[i],4 if name=='bass' else 6) for i in range(performers)]
    if drums in ('hats','both'): voices.append(Voice('drums-hats','drum-kit','hats',1))
    if drums in ('snare','both'): voices.append(Voice('drums-snare','drum-kit','snare',1))
    return Mode(name,performers,drums,tuple(voices))


@dataclass(frozen=True)
class Bank:
    instrument: str
    bank_id: str | None
    verified: bool
    reason: str
    pitches: frozenset[int] = frozenset()
    articulations: frozenset[str] = frozenset()
    string_pitches: tuple[frozenset[int], ...] = ()


def _hash(stream):
    return hashlib.file_digest(stream,'sha256').hexdigest()


def _verify_archive(root, filename, digest, destination, only=None):
    archive=root/'provenance'/filename
    with archive.open('rb') as stream:
        if _hash(stream)!=digest: raise ValueError('Provenance archive hash mismatch')
    with zipfile.ZipFile(archive) as zipped:
        entries=[i for i in zipped.infolist() if not i.is_dir() and (only is None or i.filename in only)]
        if only is not None and {i.filename for i in entries}!=set(only):
            raise ValueError('Required archive member missing')
        for entry in entries:
            target=(destination/entry.filename).resolve()
            if not target.is_relative_to(destination.resolve()): raise ValueError('Invalid asset path')
            if target.stat().st_size!=entry.file_size: raise ValueError('Installed asset size mismatch')
            with target.open('rb') as current,zipped.open(entry) as expected:
                if _hash(current)!=_hash(expected): raise ValueError('Installed asset hash mismatch')


def installed_banks(root=ROOT):
    """Explicit read-only verification; no imports of native DLLs, no downloads.

    Acoustic availability checks pinned archives against installed sample/map
    bytes and sfizz DLL. Bass verifies the installed experimental moderate map;
    this is operational availability, not musical or physical acceptance.
    """
    root=Path(root)
    result={key:Bank(key,None,False,'No verified instrument bank/mapping installed')
            for key in ('acoustic','electric','jazz-bass','drum-kit')}
    try:
        _verify_archive(root,'meta-open-sound-meta-steel-guitar-ogg.zip',
            'a06baf193497ba36f20ccb67018e9f106155ad5e4f81d8a672530498038f68ed',root/'vendor/Meta-open-sound')
        _verify_archive(root,'sfizz-1.2.3-win64.zip',
            '7431cbe9b7f2e71a9ff4b12fbbe82c766dbf75b1eedecca627e7506cb06a9fcc',root/'vendor/sfizz',{'bin/Release/sfizz.dll'})
        text=(root/'vendor/Meta-open-sound/meta-steel-guitar/meta-steel-guitar.sfz').read_text()
        strings=tuple(frozenset(map(int,re.findall(r'<region> key=(\d+)',
            text.split(f'<master> lochan={i+2} hichan={i+2}')[1].split('<master>')[0]))) for i in range(6))
        if any(not pitches for pitches in strings): raise ValueError('Missing string coverage')
        result['acoustic']=Bank('acoustic','meta-steel-guitar-v1.0.0-ogg',True,
            'Pinned sample/map and engine bytes verified; existing acoustic adapter only',
            frozenset().union(*strings),frozenset({'sampled-pluck'}),strings)
    except (OSError,ValueError,IndexError,zipfile.BadZipFile) as exc:
        result['acoustic']=Bank('acoustic',None,False,'Acoustic verification failed: '+type(exc).__name__)
    try:
        from .growlybass import require_bank, moderate_map, PROFILE_VERSION
        from .bass_performance import OPEN, MAX_FRET
        config=require_bank(root=root)
        moderate_map(config['bank'])
        strings=tuple(frozenset(range(opened,opened+MAX_FRET+1)) for opened in OPEN)
        result['jazz-bass']=Bank('jazz-bass',PROFILE_VERSION,True,
            'Experimental clean RR1 sustain, velocities1..120; assumed fingering, unaccepted musical quality',
            frozenset(range(25,64)),frozenset({'clean-sustain-first-variation'}),strings)
    except (OSError,ValueError,KeyError,StopIteration) as exc:
        result['jazz-bass']=Bank('jazz-bass',None,False,'Bass verification failed: '+type(exc).__name__)
    return result


def availability(selection, banks):
    if selection!=mode(selection.name,selection.performers,selection.drums):
        raise ValueError('Mode voice assignments were altered')
    missing=sorted({v.instrument for v in selection.voices
                    if v.instrument not in banks or not banks[v.instrument].verified})
    from .growlybass import PROFILE_VERSION
    bass=banks.get('jazz-bass')
    adapter=selection.drums=='none' and (selection.name=='acoustic' or (
        selection.name=='bass' and bass is not None and bass.bank_id==PROFILE_VERSION))
    return {'playable':not missing and adapter,'missing_instruments':missing,
            'adapter_ready':adapter,'contract_version':VERSION,
            'experimental':selection.name=='bass'}


@dataclass(frozen=True)
class Event:
    id: str
    voice_id: str
    start: int
    end: int
    pitch: int
    velocity: int
    articulation: str


def validate_events(selection, events, banks, *, sample_rate, origin, end_sample):
    """Absolute source-sample clock, sorted starts, bounded passage, no retiming.

    Validates data constraints, not musical fidelity or hand/fret feasibility.
    Guitar string assignment remains the existing planner's responsibility.
    """
    if any(type(x) is not int for x in (sample_rate,origin,end_sample)) or not 8000<=sample_rate<=192000 or not 0<=origin<end_sample or end_sample-origin>30*sample_rate:
        raise ValueError('Invalid bounded source clock')
    if selection!=mode(selection.name,selection.performers,selection.drums): raise ValueError('Invalid mode voices')
    if len(events)>4000: raise ValueError('Too many events')
    voices={v.id:v for v in selection.voices};seen=set();previous=origin;active={}
    for event in events:
        if not event.id or event.id in seen or event.voice_id not in voices: raise ValueError('Invalid event identity')
        seen.add(event.id)
        if any(type(x) is not int for x in (event.start,event.end,event.pitch,event.velocity)) or not origin<=event.start<event.end<=end_sample or event.start<previous or not 1<=event.velocity<=127:
            raise ValueError('Invalid event timing/order or velocity')
        previous=event.start;voice=voices[event.voice_id];bank=banks.get(voice.instrument)
        if bank is None or bank.instrument!=voice.instrument or not bank.verified or event.pitch not in bank.pitches or event.articulation not in bank.articulations:
            raise ValueError('Unverified bank or unsupported pitch/articulation')
        if voice.instrument=='jazz-bass' and event.velocity>120:
            raise ValueError('Experimental bass profile supports velocities1..120')
        if voice.instrument=='drum-kit' and not event.articulation.startswith(voice.responsibility+'-'):
            raise ValueError('Drum articulation routed to the wrong part')
        active[event.voice_id]=[e for e in active.get(event.voice_id,[]) if e.end>event.start]
        if len(active[event.voice_id])>=voice.max_polyphony: raise ValueError('Voice polyphony exceeded')
        active[event.voice_id].append(event)
    # Duos must not be identical doubled event streams.
    pitched=[v for v in selection.voices if v.instrument!='drum-kit']
    if any(not any(e.voice_id==v.id for e in events) for v in pitched):
        raise ValueError('Each pitched performer needs a nonempty part')
    if len(pitched)==2:
        streams=[[(e.start,e.end,e.pitch,e.articulation) for e in events if e.voice_id==v.id] for v in pitched]
        if streams[0] and streams[0]==streams[1]: raise ValueError('Duo parts must have distinct musical content')
    return True
