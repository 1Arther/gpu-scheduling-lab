"""Build the reviewed one-shot Stage5 zipapp; contains a public key only.

The resulting .pyz installs as zbs, never root, and performs no cluster action
at installation. The operator explicitly dispatches prepare/start afterward.
"""
import argparse
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import zipfile

ROOT = Path(__file__).resolve().parents[1]
INSTALL_ROOT = '/home/zbs/hrrn-stage5-20261027-live01'
spec = importlib.util.spec_from_file_location('stage5_package_gate', Path(__file__).with_name('hrrn_stage5_gate.py'))
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

# This literal is the zipapp's __main__.py. Keep bootstrap stdlib-only and do
# all member/path/hash checks before creating the immutable install directory.
BOOTSTRAP = r'''import base64
import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import tempfile
import zipfile

FIXED_ROOT = '/home/zbs/hrrn-stage5-20261027-live01'

def require_installer_identity(name, uid, home):
    if name != 'zbs' or uid == 0 or str(Path(home)) != '/home/zbs':
        raise PermissionError('Install as the zbs account, never root or another user')

def safe_member(name):
    p = PurePosixPath(name)
    if not name or p.is_absolute() or '\\' in name or ':' in name or any(v in ('', '.', '..') for v in name.split('/')):
        raise ValueError('Unsafe package member: ' + name)
    return p

def validated_payload(archive):
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        if len(names) != len(set(names)):
            raise ValueError('Duplicate archive member')
        for entry in z.infolist():
            safe_member(entry.filename)
            typ = stat.S_IFMT(entry.external_attr >> 16)
            if entry.is_dir() or typ not in (0, stat.S_IFREG):
                raise ValueError('Non-regular archive member')
        manifest = json.loads(z.read('install-manifest.json'))
        if manifest.get('install_root') != FIXED_ROOT or manifest.get('live_scope') != ['order-a', 'order-b']:
            raise ValueError('Unexpected installation or experiment scope')
        expected = {'__main__.py', 'install-manifest.json'} | {'payload/' + name for name in manifest['files']}
        if set(names) != expected:
            raise ValueError('Unexpected/missing archive members')
        if sum(i.file_size for i in z.infolist()) > 256 * 1024 * 1024:
            raise ValueError('Package exceeds bounded extraction size')
        payload = {}
        for name, sha in manifest['files'].items():
            safe_member(name)
            content = z.read('payload/' + name)
            if hashlib.sha256(content).hexdigest() != sha:
                raise ValueError('Payload integrity mismatch: ' + name)
            payload[name] = content
        return manifest, payload

def authorization_text(original, public, root, expiry):
    if not public.startswith('ssh-ed25519 ') or '\n' in public or '\r' in public:
        raise ValueError('Not a single SSH public key')
    fields = public.split()
    if len(fields) != 3 or fields[2] != 'gpu-lab-hrrn-stage5-live01':
        raise ValueError('Unexpected execution public key format')
    decoded = base64.b64decode(fields[1], validate=True)
    if len(decoded) != 51 or decoded[:19] != b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20':
        raise ValueError('Invalid execution public key encoding')
    # Preserve all other keys and comments, including persistent read-only access.
    retained = ''.join(line for line in original.splitlines(keepends=True)
                       if line.lstrip().startswith('#') or fields[1] not in line.split())
    if retained and not retained.endswith('\n'):
        retained += '\n'
    entry = 'restrict,expiry-time="' + expiry + '",command="/usr/bin/python3 -I ' + str(root / 'hrrn_stage5_gate.py') + '" ' + public + '\n'
    return retained + entry

def install(archive):
    import pwd
    user = pwd.getpwuid(os.getuid())
    require_installer_identity(user.pw_name, user.pw_uid, user.pw_dir)
    manifest, payload = validated_payload(archive)
    root = Path(FIXED_ROOT)
    if root.exists() or root.is_symlink():
        raise FileExistsError('Preserve existing attempt; refusing to overwrite ' + str(root))
    # Read the gate only after its manifest hash has passed. It performs a second
    # exact payload allowlist and A/B scope validation before installing the key.
    os.umask(0o077)
    root.mkdir(mode=0o700)
    for name, content in payload.items():
        path = root.joinpath(*safe_member(name).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o500 if name.endswith('.py') else 0o400)
    (root / 'bundle-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    namespace = {'__name__': 'installed_gate_validation', '__file__': str(root / 'hrrn_stage5_gate.py')}
    exec(compile(payload['hrrn_stage5_gate.py'], str(root / 'hrrn_stage5_gate.py'), 'exec'), namespace)
    namespace['verify_bundle'](root)
    ssh = Path(user.pw_dir) / '.ssh'
    auth = ssh / 'authorized_keys'
    if ssh.is_symlink() or auth.is_symlink():
        raise RuntimeError('Refuse symlinked SSH authorization files')
    ssh.mkdir(mode=0o700, exist_ok=True)
    original = auth.read_text() if auth.exists() else ''
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = ssh / ('authorized_keys.before-hrrn-stage5-' + stamp)
    with backup.open('x') as stream:
        stream.write(original)
    backup.chmod(0o600)
    public = manifest['execution_public_key']
    expiry = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=4)).strftime('%Y%m%d%H%M%SZ')
    replacement = authorization_text(original, public, root, expiry)
    with tempfile.NamedTemporaryFile(mode='w', dir=ssh, prefix='.hrrn-stage5-auth-', delete=False) as stream:
        stream.write(replacement)
        temp = Path(stream.name)
    temp.chmod(0o600)
    if (auth.read_text() if auth.exists() else '') != original:
        temp.unlink()
        raise RuntimeError('SSH authorization changed concurrently; refusing to replace it')
    os.replace(temp, auth)
    ssh.chmod(0o700)
    (root / 'installation.json').write_text(json.dumps(dict(installed_utc=stamp, key_expiry_utc=expiry,
        authorized_keys_backup=str(backup), archive_sha256=hashlib.sha256(Path(archive).read_bytes()).hexdigest(),
        cluster_changed=False), indent=2) + '\n')
    print('HRRN_STAGE5_SCOPED_READY')
    print('Root:', root)
    print('Temporary execution key expires UTC:', expiry)
    print('Installed only; no experiment started and no Kubernetes resources changed.')

if __name__ == '__main__':
    if sys.argv[1:] != ['--install']:
        raise SystemExit('Usage: python3 hrrn-stage5.pyz --install (as zbs)')
    install(Path(sys.argv[0]).resolve())
'''


def public_key(path):
    lines = path.read_text(encoding='utf-8').strip().splitlines()
    if len(lines) != 1:
        raise ValueError('Exactly one public key line required; never provide a private key')
    parts = lines[0].split()
    if len(parts) < 2 or parts[0] != 'ssh-ed25519':
        raise ValueError('Expected an ed25519 public key, not key options or a private key')
    decoded = base64.b64decode(parts[1], validate=True)
    if len(decoded) != 51 or decoded[:15] != struct.pack('>I', 11) + b'ssh-ed25519' or decoded[15:19] != struct.pack('>I', 32):
        raise ValueError('Invalid ed25519 public key encoding')
    return parts[0] + ' ' + parts[1] + ' gpu-lab-hrrn-stage5-live01'


def build(key_path, protocol_path, output):
    key = public_key(key_path)
    protocol = json.loads(protocol_path.read_text(encoding='utf-8-sig'))
    gate.validate_protocol(protocol)
    # Revalidate generated workloads rather than accepting arbitrary passed-in
    # Kubernetes objects merely because the caller supplied a protocol filename.
    spec = importlib.util.spec_from_file_location('package_manifests', ROOT / 'scripts/hrrn_stage5_manifests.py')
    manifests = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(manifests)
    if protocol != manifests.protocol(protocol['run_id']):
        raise ValueError('Protocol differs from the reviewed deterministic generator')
    image_dir = ROOT / 'results/hrrn-k8s-stage5-20261027/image'
    sources = {
        'hrrn_stage5_gate.py': ROOT / 'scripts/hrrn_stage5_gate.py',
        'hrrn_stage5_live.py': ROOT / 'scripts/hrrn_stage5_live.py',
        'hrrn_stage5_evidence.py': ROOT / 'scripts/hrrn_stage5_evidence.py',
        'protocol.json': protocol_path,
        'scheduler.oci.tar': image_dir / 'scheduler.oci.tar', 'image.json': image_dir / 'image.json',
        'helpers/staging.py': ROOT / 'gpu_lab/staging.py',
        'helpers/round7_collect.py': ROOT / 'scripts/round7_collect.py',
        'helpers/round7_window_preflight.py': ROOT / 'scripts/round7_window_preflight.py',
        'gpu_lab/__init__.py': ROOT / 'gpu_lab/__init__.py', 'gpu_lab/staging.py': ROOT / 'gpu_lab/staging.py',
    }
    payload = {name: path.read_bytes() for name, path in sources.items()}
    if set(payload) != gate.FILES:
        raise ValueError('Builder/gate payload scope differs')
    image = json.loads(payload['image.json'])
    if (image['binary_sha256'] != gate.HASHES['hrrn_scheduler'] or image['manifest_digest'] != gate.IMAGE_MANIFEST_DIGEST
            or image['archive_sha256'] != gate.IMAGE_ARCHIVE_SHA
            or hashlib.sha256(payload['scheduler.oci.tar']).hexdigest() != gate.IMAGE_ARCHIVE_SHA):
        raise ValueError('Frozen scheduler image archive identity mismatch')
    manifest = {'schema': 1, 'install_root': INSTALL_ROOT, 'live_scope': ['order-a', 'order-b'],
                'complete_job_enabled': False, 'execution_public_key': key, 'frozen_shas': gate.HASHES,
                'files': {name: hashlib.sha256(content).hexdigest() for name, content in sorted(payload.items())}}
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation preserves earlier reviewed packages and their hashes.
    with output.open('xb') as raw:
        with zipfile.ZipFile(raw, 'w', compression=zipfile.ZIP_STORED) as archive:
            archive.writestr('__main__.py', BOOTSTRAP)
            archive.writestr('install-manifest.json', json.dumps(manifest, sort_keys=True, indent=2))
            for name, content in sorted(payload.items()):
                archive.writestr('payload/' + name, content)
    metadata = {'package': str(output), 'package_sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
                'package_bytes': output.stat().st_size, 'install_root': INSTALL_ROOT,
                'live_scope': ['order-a', 'order-b'], 'contains_private_key': False,
                'temporary_key_hours': 4, 'cluster_executed': False, 'manifest': manifest}
    with output.with_suffix('.manifest.json').open('x', encoding='utf-8') as stream:
        json.dump(metadata, stream, ensure_ascii=False, indent=2)
    return metadata


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public-key', type=Path, required=True)
    parser.add_argument('--protocol', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = build(args.public_key, args.protocol, args.output)
    print(json.dumps({k: v for k, v in result.items() if k != 'manifest'}, ensure_ascii=False))
