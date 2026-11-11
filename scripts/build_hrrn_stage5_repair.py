"""Build a fixed-base runner-only repair zipapp; never edits the first attempt."""
import argparse
import ast
import copy
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BASE_PACKAGE = ROOT / 'results/hrrn-k8s-stage5-20261027/execution-live01/hrrn-stage5-live01.pyz'
BASE_PACKAGE_SHA = '7cffccd4aff4c097632ce4e090211652a904c611a91cd79c3d3709c5bdef2143'
BASE_RUNNER_SHA = 'bad1a468a2221a562bdd52664dba68c3b1d9fdfeaf9cdb4ebf8a2d1e94bedb29'
OLD_ROOT = '/home/zbs/hrrn-stage5-20261027-live01'
NEW_ROOT = '/home/zbs/hrrn-stage5-20261027-live01-r2'

REPAIR_BOOTSTRAP = r'''import base64
import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import tempfile
import zipfile

OLD_ROOT = '/home/zbs/hrrn-stage5-20261027-live01'
NEW_ROOT = '/home/zbs/hrrn-stage5-20261027-live01-r2'
# Frozen first-package manifest and previously tested authorization function are
# inserted by the builder. Neither command-line arguments nor server files select
# executable payload paths or change the allowed replacement.
__FROZEN_BASE_MANIFEST__
__FROZEN_AUTHORIZATION_FUNCTION__

def safe_path(root, name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or '\\' in name or ':' in name or any(p in ('', '.', '..') for p in name.split('/')):
        raise ValueError('Unsafe fixed payload path')
    current = root
    if current.is_symlink():
        raise ValueError('Symlinked experiment root')
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('Symlinked payload component')
    return current

def validate_change(base, updated, runner):
    if base.get('install_root') != OLD_ROOT or updated.get('install_root') != NEW_ROOT:
        raise ValueError('Unexpected fixed installation paths')
    wanted = json.loads(json.dumps(base))
    wanted['install_root'] = NEW_ROOT
    wanted['files']['hrrn_stage5_live.py'] = hashlib.sha256(runner).hexdigest()
    if updated != wanted:
        raise ValueError('Only the runner hash and fixed installation root may change')
    if wanted['files']['hrrn_stage5_live.py'] == base['files']['hrrn_stage5_live.py']:
        raise ValueError('Runner repair is empty')

def verify_directory(root, manifest):
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Expected a regular experiment directory')
    recorded = json.loads(safe_path(root, 'bundle-manifest.json').read_text())
    if recorded != manifest:
        raise ValueError('Installed manifest differs from the fixed approved baseline')
    for name, expected in manifest['files'].items():
        path = safe_path(root, name)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('Payload hash mismatch: ' + name)

def copy_repaired_attempt(base, destination, old_manifest, updated, runner):
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('Preserve the existing repaired attempt: ' + str(destination))
    validate_change(old_manifest, updated, runner)
    verify_directory(base, old_manifest)
    destination.mkdir(mode=0o700)
    for name, expected in old_manifest['files'].items():
        data = safe_path(base, name).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('Base changed while copying: ' + name)
        if name == 'hrrn_stage5_live.py':
            data = runner
        target = safe_path(destination, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as stream:
            stream.write(data)
        target.chmod(0o500 if name.endswith('.py') else 0o400)
    with (destination / 'bundle-manifest.json').open('x') as stream:
        json.dump(updated, stream, indent=2)
    verify_directory(destination, updated)

def validate_not_started(base):
    if safe_path(base, 'evidence/launch.json').exists():
        raise RuntimeError('Original execution was attempted; no repair upgrade permitted')
    state = json.loads(safe_path(base, 'evidence/state.json').read_text())
    if (state.get('phase') != 'new' or state.get('created') or state.get('planned')
            or state.get('old_scale_intent') or state.get('experiment_started') or state.get('started_epoch')):
        raise RuntimeError('Only a failed read-only preflight may be repaired')

def read_repair(archive):
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        expected = {'__main__.py', 'repair-manifest.json', 'payload/hrrn_stage5_live.py'}
        if len(names) != len(set(names)) or set(names) != expected:
            raise ValueError('Unexpected repair members; runner is the only replacement')
        for entry in z.infolist():
            if entry.is_dir() or stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG):
                raise ValueError('Nonregular repair member')
        if sum(entry.file_size for entry in z.infolist()) > 2 * 1024 * 1024:
            raise ValueError('Repair exceeds its bounded size')
        updated = json.loads(z.read('repair-manifest.json'))
        runner = z.read('payload/hrrn_stage5_live.py')
    validate_change(BASE_MANIFEST, updated, runner)
    return updated, runner

def install(archive):
    import fcntl
    import pwd
    user = pwd.getpwuid(os.getuid())
    if user.pw_name != 'zbs' or user.pw_uid == 0 or user.pw_dir != '/home/zbs':
        raise PermissionError('Run the fixed repair as zbs, never root')
    os.umask(0o077)
    base, destination = Path(OLD_ROOT), Path(NEW_ROOT)
    if base.stat().st_uid != user.pw_uid:
        raise PermissionError('Base directory has another owner')
    updated, runner = read_repair(archive)
    verify_directory(base, BASE_MANIFEST)
    # The existing lock is opened read-only: the original directory and evidence
    # are never rewritten. No active preflight may race the SSH entry change.
    lock_path = safe_path(base, 'run.lock')
    with lock_path.open('rb') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        validate_not_started(base)
        copy_repaired_attempt(base, destination, BASE_MANIFEST, updated, runner)
        # Invoke only the hash-verified gate's local integrity checker. This does
        # not call its command dispatcher or start any Kubernetes operation.
        gate_source = (destination / 'hrrn_stage5_gate.py').read_bytes()
        namespace = {'__name__': 'repair_gate_validation', '__file__': str(destination / 'hrrn_stage5_gate.py')}
        exec(compile(gate_source, str(destination / 'hrrn_stage5_gate.py'), 'exec'), namespace)
        namespace['verify_bundle'](destination)
        ssh = Path(user.pw_dir) / '.ssh'
        auth = ssh / 'authorized_keys'
        if ssh.is_symlink() or auth.is_symlink():
            raise ValueError('Symlinked SSH authorization path')
        original = auth.read_text()
        key = updated['execution_public_key']
        blob = key.split()[1]
        matching = [line for line in original.splitlines() if not line.lstrip().startswith('#') and blob in line.split()]
        old_command = 'command="/usr/bin/python3 -I ' + str(base / 'hrrn_stage5_gate.py') + '"'
        if len(matching) != 1 or old_command not in matching[0] or not matching[0].startswith('restrict,'):
            raise RuntimeError('The one prior scoped execution key is absent or changed')
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup = ssh / ('authorized_keys.before-hrrn-stage5-r2-' + stamp)
        with backup.open('x') as stream:
            stream.write(original)
        backup.chmod(0o600)
        expiry = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=4)).strftime('%Y%m%d%H%M%SZ')
        replacement = authorization_text(original, key, destination, expiry)
        with tempfile.NamedTemporaryFile(mode='w', dir=ssh, prefix='.hrrn-stage5-r2-auth-', delete=False) as stream:
            stream.write(replacement)
            temporary = Path(stream.name)
        temporary.chmod(0o600)
        if auth.read_text() != original:
            temporary.unlink()
            raise RuntimeError('SSH authorization changed concurrently; entry was not replaced')
        os.replace(temporary, auth)
        (destination / 'installation.json').write_text(json.dumps(dict(
            base_root=str(base), base_preserved=True, replacement_only='hrrn_stage5_live.py',
            installed_utc=stamp, key_expiry_utc=expiry, authorized_keys_backup=str(backup),
            archive_sha256=hashlib.sha256(Path(archive).read_bytes()).hexdigest(), cluster_changed=False), indent=2) + '\n')
    print('HRRN_STAGE5_REPAIR_READY')
    print('New root:', destination)
    print('Old attempt and evidence preserved:', base)
    print('Temporary key expires UTC:', expiry)
    print('Installed only; no Kubernetes action or experiment was started.')

if __name__ == '__main__':
    if sys.argv[1:] != ['--install']:
        raise SystemExit('Usage: python3 hrrn-stage5-live01-r2.pyz --install (as zbs)')
    install(Path(sys.argv[0]).resolve())
'''


def frozen_base():
    if hashlib.sha256(BASE_PACKAGE.read_bytes()).hexdigest() != BASE_PACKAGE_SHA:
        raise ValueError('Frozen first package has changed')
    with zipfile.ZipFile(BASE_PACKAGE) as archive:
        base = json.loads(archive.read('install-manifest.json'))
        original_bootstrap = archive.read('__main__.py').decode()
    if base['install_root'] != OLD_ROOT or base['files']['hrrn_stage5_live.py'] != BASE_RUNNER_SHA or len(base['files']) != 11:
        raise ValueError('Unexpected first-package baseline')
    tree = ast.parse(original_bootstrap)
    authorization = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'authorization_text')
    function_source = ast.get_source_segment(original_bootstrap, authorization)
    return base, function_source


def bootstrap(base, authorization):
    return REPAIR_BOOTSTRAP.replace('__FROZEN_BASE_MANIFEST__', 'BASE_MANIFEST = json.loads(' + repr(json.dumps(base, sort_keys=True)) + ')').replace('__FROZEN_AUTHORIZATION_FUNCTION__', authorization)


def build(output):
    base, authorization = frozen_base()
    runner = (ROOT / 'scripts/hrrn_stage5_live.py').read_bytes()
    updated = copy.deepcopy(base)
    updated['install_root'] = NEW_ROOT
    updated['files']['hrrn_stage5_live.py'] = hashlib.sha256(runner).hexdigest()
    source = bootstrap(base, authorization)
    scope = {'__name__': 'repair_build_validation'}
    exec(compile(source, 'repair-bootstrap', 'exec'), scope)
    scope['validate_change'](base, updated, runner)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('xb') as raw:
        with zipfile.ZipFile(raw, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('__main__.py', source)
            archive.writestr('repair-manifest.json', json.dumps(updated, sort_keys=True, indent=2))
            archive.writestr('payload/hrrn_stage5_live.py', runner)
    scope['read_repair'](output)
    report = dict(package=str(output), package_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
                  package_bytes=output.stat().st_size, base_package_sha256=BASE_PACKAGE_SHA,
                  old_runner_sha256=BASE_RUNNER_SHA, new_runner_sha256=updated['files']['hrrn_stage5_live.py'],
                  old_root=OLD_ROOT, new_root=NEW_ROOT, replacement_only='hrrn_stage5_live.py',
                  old_attempt_modified=False, cluster_executed=False, manifest=updated)
    with output.with_suffix('.manifest.json').open('x') as stream:
        json.dump(report, stream, indent=2)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = build(args.output)
    print(json.dumps({k: v for k, v in result.items() if k != 'manifest'}, indent=2))
