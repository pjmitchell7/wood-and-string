"""Prepare and inspect loopback-only Caddy config; never start or install trust.

This adapts syntax only. A separate reviewed validation step may provision the
private CA after the data/private-pki directory has restricted Windows ACLs.
No LAN listener or phone trust setup is included.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from identity import library_identity

EXE_HASH = '5f93a9bd555f3aac9d33af2756ba14b3519d8cca4ffd55256bdaeab444c9db2a'


def inspect_config(value, hostname, pki):
    if value.get('admin', {}).get('disabled') is not True:
        raise ValueError('Admin listener must be disabled')
    if value.get('admin', {}).get('config', {}).get('persist') is not False:
        raise ValueError('Automatic config persistence must be disabled')
    if value.get('storage') != {'module': 'file_system', 'root': pki}:
        raise ValueError('Private certificate storage must be explicit')
    apps = value['apps']
    authorities = apps['pki']['certificate_authorities']
    if set(authorities) != {'local'} or authorities['local'].get('install_trust') is not False:
        raise ValueError('Automatic root trust must be explicitly false')
    servers = apps['http']['servers']
    if len(servers) != 1:
        raise ValueError('Expected exactly one loopback HTTPS server')
    server = next(iter(servers.values()))
    if (server.get('listen') != ['127.0.0.1:8443'] or server.get('protocols') != ['h1', 'h2']
            or server.get('strict_sni_host') is not True
            or server.get('automatic_https', {}).get('disable_redirects') is not True):
        raise ValueError('Unexpected listener, protocol, SNI or redirect configuration')
    policies = apps['tls']['automation']['policies']
    if any(policy.get('issuers') != [{'module': 'internal'}] for policy in policies):
        raise ValueError('Only internal certificate issuance is allowed')
    text = json.dumps(server)
    if hostname not in text or '127.0.0.1:8766' not in text:
        raise ValueError('Saved library hostname or paired gateway is missing')


def prepare():
    exe = ROOT / 'network/vendor/caddy-2.11.7/caddy.exe'
    if hashlib.sha256(exe.read_bytes()).hexdigest() != EXE_HASH:
        raise ValueError('Portable Caddy does not match the pinned executable')
    identity = library_identity(ROOT / 'data')
    pki = (ROOT / 'data/private-pki').as_posix()
    config = (ROOT / 'network/Caddyfile.local.template').read_text()
    config = config.replace('__LIBRARY_HOST__', identity['hostname']).replace('__PKI_DIRECTORY__', json.dumps(pki))
    folder = ROOT / 'data/network'
    folder.mkdir(exist_ok=True)
    source = folder / 'Caddyfile.local'
    source.write_text(config, encoding='utf-8')
    result = subprocess.run([str(exe), 'adapt', '--config', str(source), '--adapter', 'caddyfile', '--pretty'], capture_output=True, text=True, check=True, timeout=30)
    value = json.loads(result.stdout)
    inspect_config(value, identity['hostname'], pki)
    output = folder / 'caddy-local.json'
    output.write_text(json.dumps(value, indent=2), encoding='utf-8')
    print(json.dumps({'config': str(output), 'hostname': identity['hostname'],
                      'listen': '127.0.0.1:8443', 'automatic_trust': False,
                      'status': 'adapted and inspected only; no listeners or trust changes'}, indent=2))


if __name__ == '__main__':
    prepare()
