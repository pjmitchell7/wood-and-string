"""Cheap Electric preview availability; the renderer must still verify assets.

No engine is loaded, waveform hashed or synthesis performed here. Availability
means that the pinned local metadata and expected files are present, not that
their current contents or musical output have passed verification.
"""
from contextlib import ExitStack
from pathlib import Path

from .emilyguitar import ROOT, PROFILE_VERSION, _configuration, _plain_path


def bank_status(root=ROOT):
    try:
        with ExitStack() as stack:
            config = _configuration(stack, root, lambda: False)
            _plain_path(config['bank'])
            for member in config['members']:
                path = _plain_path(config['bank'] / member['path'])
                if not path.is_file() or path.stat().st_size != member['bytes']:
                    raise ValueError('The Electric sample bank is missing or incomplete.')
            binaries = {Path(row['path']).name.casefold(): row for row in config['native']['binaries']}
            imports = {Path(name).name.casefold(): values for name, values in config['native']['imports'].items()}
            pending, seen = ['sfizz.dll'], set()
            while pending:
                name = pending.pop().casefold()
                if name in seen or name.startswith(('api-ms-', 'ext-ms-')):
                    continue
                seen.add(name)
                entry = binaries[name]
                path = config['engine'] if name == 'sfizz.dll' else Path(entry['path'])
                _plain_path(path)
                if not path.is_file() or path.stat().st_size != entry['bytes']:
                    raise ValueError('The local Electric sample engine is missing or incomplete.')
                pending.extend(imports[name])
        return {'ready': True, 'reason': None, 'profile_version': PROFILE_VERSION,
                'integrity_checked': False, 'musical_acceptance': False}
    except (OSError, ValueError, KeyError, TypeError):
        return {'ready': False,
                'reason': 'The experimental Electric bank or local sample engine needs verification.',
                'profile_version': PROFILE_VERSION, 'integrity_checked': False,
                'musical_acceptance': False}
