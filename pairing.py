"""Device pairing state machine. No listener or certificate installation.

Gateway integration is intentionally gated until its HTTP boundary is reviewed.
Owner approval is a separate local-only capability, never a phone endpoint.
"""
import hashlib
import secrets
import time
import uuid

def token_hash(token): return hashlib.sha256(token.encode('utf-8')).hexdigest()

class Pairing:
    def __init__(self,library,clock=time.time):
        self.library=library;self.clock=clock
        with library.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS pairing_requests (
              id TEXT PRIMARY KEY, label TEXT NOT NULL, code TEXT NOT NULL,
              secret_hash TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
              state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS devices (
              id TEXT PRIMARY KEY,label TEXT NOT NULL,token_hash TEXT UNIQUE NOT NULL,
              created REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0);
            ''')

    def request(self,label):
        if not isinstance(label,str) or not 1<=len(label.strip())<=80: raise ValueError('Use a short device name.')
        now=self.clock();identity=str(uuid.uuid4());secret=secrets.token_urlsafe(32)
        code=f'{secrets.randbelow(1000000):06d}'
        with self.library.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("DELETE FROM pairing_requests WHERE expires<?",(now,))
            if db.execute("SELECT COUNT(*) FROM pairing_requests WHERE state IN ('pending','approved')").fetchone()[0]>=5:
                raise ValueError('Too many waiting devices. Review them on the laptop first.')
            latest=db.execute('SELECT MAX(created) FROM pairing_requests').fetchone()[0]
            if latest is not None and now-latest<5: raise ValueError('Wait a few seconds before trying again.')
            db.execute('INSERT INTO pairing_requests VALUES (?,?,?,?,?,?,?)',(identity,label.strip(),code,token_hash(secret),now,now+300,'pending'))
        return {'id':identity,'secret':secret,'code':code,'expires':now+300}

    def pending(self):
        with self.library.connect() as db:
            return [dict(r) for r in db.execute("SELECT id,label,code,expires,state FROM pairing_requests WHERE expires>? AND state IN ('pending','approved')",(self.clock(),))]

    def approve_local(self,identity,approve=True):
        """Caller must already be authenticated as the local owner."""
        with self.library.connect() as db:
            result=db.execute("UPDATE pairing_requests SET state=? WHERE id=? AND state='pending' AND expires>?",('approved' if approve else 'denied',identity,self.clock()))
            if result.rowcount!=1: raise ValueError('Pairing request expired or was already handled.')

    def claim(self,identity,secret):
        if not isinstance(secret,str) or len(secret)>128: raise PermissionError('Invalid pairing request.')
        with self.library.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM pairing_requests WHERE id=?',(identity,)).fetchone()
            if row is None or row['expires']<=self.clock() or not secrets.compare_digest(row['secret_hash'],token_hash(secret)):
                raise PermissionError('Pairing request expired or is invalid.')
            if row['state']=='pending': return {'state':'pending'}
            # The server-generated 256-bit request secret becomes this device's
            # credential after local approval. Retrying a lost response within
            # the pairing window returns the same cookie, never a second device.
            token=secret;device_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'wood-string-device:'+identity))
            if row['state']=='claimed':
                device=db.execute('SELECT * FROM devices WHERE id=? AND revoked=0',(device_id,)).fetchone()
                if device and secrets.compare_digest(device['token_hash'],token_hash(token)):
                    return {'state':'paired','device_id':device_id,'token':token}
                raise PermissionError('Device was revoked. Request pairing again.')
            if row['state']!='approved': raise PermissionError('Pairing request is not approved.')
            db.execute('INSERT INTO devices VALUES (?,?,?,?,0)',(device_id,row['label'],token_hash(token),self.clock()))
            db.execute("UPDATE pairing_requests SET state='claimed' WHERE id=?",(identity,))
        return {'state':'paired','device_id':device_id,'token':token}

    def authenticate(self,token):
        if not isinstance(token,str) or not 20<=len(token)<=128:return None
        with self.library.connect() as db:
            row=db.execute('SELECT id,label FROM devices WHERE token_hash=? AND revoked=0',(token_hash(token),)).fetchone()
        return dict(row) if row else None

    def devices(self):
        with self.library.connect() as db:
            return [dict(r) for r in db.execute('SELECT id,label,created,revoked FROM devices')]

    def revoke_local(self,identity):
        with self.library.connect() as db:
            result=db.execute('UPDATE devices SET revoked=1 WHERE id=?',(identity,))
            if result.rowcount!=1: raise KeyError('Device not found.')
