"""Stable library origin. Loss of an established identity requires recovery."""
import json
import os
from pathlib import Path
import uuid
from library import Library

RECOVERY = 'Saved library identity is missing or inconsistent. Restore the protected identity; do not change the phone address.'


def validate_identity(value):
    try:
        parsed = uuid.UUID(value['library_id'])
        if (value.get('version') != 1 or value['library_id'] != str(parsed)
                or value.get('hostname') != 'guitar-' + parsed.hex[:16] + '.local'
                or value.get('https_port') != 8443):
            raise ValueError(RECOVERY)
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError(RECOVERY) from error
    return value

def library_identity(root):
    library = Library(root)
    root = Path(root)
    path = root / 'identity.json'
    # Keep the lock outside the temporary file. Concurrent startup cannot read
    # an unfinished JSON file or replace a competing identity.
    with library.exclusive('identity.lock'), library.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS library_identity (singleton INTEGER PRIMARY KEY CHECK(singleton=1), value TEXT NOT NULL)')
        row = db.execute('SELECT value FROM library_identity WHERE singleton=1').fetchone()
        saved = validate_identity(json.loads(row['value'])) if row else None
        if path.exists():
            value = validate_identity(json.loads(path.read_text(encoding='utf-8')))
            if saved is not None and value != saved:
                raise ValueError(RECOVERY)
        else:
            has_devices_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='devices'").fetchone()
            has_devices = has_devices_table and db.execute('SELECT 1 FROM devices LIMIT 1').fetchone()
            has_private_state = (root / 'private-pki').exists()
            if saved is not None or has_devices or has_private_state:
                raise ValueError(RECOVERY)
            identity = str(uuid.uuid4())
            value = {'version': 1, 'library_id': identity,
                     'hostname': 'guitar-' + identity.replace('-', '')[:16] + '.local',
                     'https_port': 8443}
            temporary = root / ('identity-' + str(uuid.uuid4()) + '.tmp')
            try:
                with temporary.open('x', encoding='utf-8') as stream:
                    json.dump(value, stream, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        if saved is None:
            db.execute('INSERT INTO library_identity VALUES (1,?)', (json.dumps(value, sort_keys=True),))
        return value
