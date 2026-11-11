"""Fixed v1 payload / recovered-r2 authorization upgrade to the r3 runner."""
import argparse
import ast
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('frozen_stage5_repair_builder', Path(__file__).with_name('build_hrrn_stage5_repair.py'))
previous = importlib.util.module_from_spec(spec)
spec.loader.exec_module(previous)
CURRENT_PACKAGE = ROOT / 'results/hrrn-k8s-stage5-20261027/execution-live01-r2/hrrn-stage5-preflight-fix.pyz'
CURRENT_PACKAGE_SHA = '2a24cd12dc0c9137e2972aa1424df6ee7f3ee54998cd6d89731b748d0b73a644'
CURRENT_RUNNER_SHA = 'b6cb8d145b583a66a1451816269cc51299445a3e561bbaf8dc69754f2595fac9'
CURRENT_ROOT = '/home/zbs/hrrn-stage5-20261027-live01-r2'
NEW_ROOT = '/home/zbs/hrrn-stage5-20261027-live01-r3'

RECOVERY_CHECK = r'''def validate_previous_recovered(current):
    state = json.loads(safe_path(current, 'evidence/state.json').read_text())
    if (state.get('phase') != 'restored' or state.get('restored') is not True
            or any(state.get(name) != [] for name in ('created', 'planned', 'cases'))
            or state.get('old_scale_intent') is not False or state.get('experiment_started') is not False):
        raise RuntimeError('r2 is not a restored attempt with an empty object/case ledger')
    confirmation = json.loads(safe_path(current, 'evidence/restore-confirmation.json').read_text())
    expected = {'original', 'rf', 'dqn', 'quota', 'original_config', 'rf-config', 'geometry', 'dqn_available', 'model_sha'}
    checks = confirmation.get('checks', {})
    if (set(checks) != expected or not all(value is True for value in checks.values())
            or confirmation.get('original_process_sha') != BASE_MANIFEST['frozen_shas']['production_scheduler']
            or confirmation.get('cluster_audit_retained') is not True
            or confirmation.get('foreign_workloads_deleted') is not False):
        raise RuntimeError('r2 restoration confirmation is incomplete or not entirely true')
'''

INSTALL = r'''def install(archive):
    import fcntl
    import pwd
    user = pwd.getpwuid(os.getuid())
    if user.pw_name != 'zbs' or user.pw_uid == 0 or user.pw_dir != '/home/zbs':
        raise PermissionError('Run this fixed repair as zbs, never root')
    os.umask(0o077)
    base, current, destination = Path(OLD_ROOT), Path(CURRENT_ROOT), Path(NEW_ROOT)
    if any(path.stat().st_uid != user.pw_uid for path in (base, current)):
        raise PermissionError('A prior experiment directory has another owner')
    updated, runner = read_repair(archive)
    verify_directory(base, BASE_MANIFEST)
    verify_directory(current, CURRENT_MANIFEST)
    # Both prior directories are read-only sources. r2's existing exclusive lock
    # must be free, and its state/confirmation are checked while holding it.
    with safe_path(current, 'run.lock').open('rb') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        validate_previous_recovered(current)
        gate_source = safe_path(base, 'hrrn_stage5_gate.py').read_bytes()
        namespace = {'__name__': 'repair_gate_validation', '__file__': str(base / 'hrrn_stage5_gate.py')}
        exec(compile(gate_source, str(base / 'hrrn_stage5_gate.py'), 'exec'), namespace)
        launch = safe_path(current, 'evidence/launch.json')
        record = json.loads(launch.read_text()) if launch.exists() else None
        if namespace['process_status'](current, record)['alive']:
            raise RuntimeError('The previous runner is still alive')
        copy_repaired_attempt(base, destination, BASE_MANIFEST, updated, runner)
        namespace['verify_bundle'](destination)
        ssh = Path(user.pw_dir) / '.ssh'
        auth = ssh / 'authorized_keys'
        if ssh.is_symlink() or auth.is_symlink():
            raise ValueError('Symlinked SSH authorization path')
        original = auth.read_text()
        key = updated['execution_public_key']
        blob = key.split()[1]
        matching = [line for line in original.splitlines() if not line.lstrip().startswith('#') and blob in line.split()]
        old_command = 'command="/usr/bin/python3 -I ' + str(current / 'hrrn_stage5_gate.py') + '"'
        if len(matching) != 1 or old_command not in matching[0] or not matching[0].startswith('restrict,'):
            raise RuntimeError('The prior r2 scoped execution key is absent or changed')
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup = ssh / ('authorized_keys.before-hrrn-stage5-r3-' + stamp)
        with backup.open('x') as stream:
            stream.write(original)
        backup.chmod(0o600)
        expiry = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=4)).strftime('%Y%m%d%H%M%SZ')
        replacement = authorization_text(original, key, destination, expiry)
        with tempfile.NamedTemporaryFile(mode='w', dir=ssh, prefix='.hrrn-stage5-r3-auth-', delete=False) as stream:
            stream.write(replacement)
            temporary = Path(stream.name)
        temporary.chmod(0o600)
        if auth.read_text() != original:
            temporary.unlink()
            raise RuntimeError('SSH authorization changed concurrently; entry was not replaced')
        os.replace(temporary, auth)
        (destination / 'installation.json').write_text(json.dumps(dict(
            base_root=str(base), previous_root=str(current), base_preserved=True, previous_preserved=True,
            replacement_only='hrrn_stage5_live.py', installed_utc=stamp, key_expiry_utc=expiry,
            authorized_keys_backup=str(backup), archive_sha256=hashlib.sha256(Path(archive).read_bytes()).hexdigest(),
            cluster_changed=False), indent=2) + '\n')
    print('HRRN_STAGE5_IMAGE_REPAIR_READY')
    print('New root:', destination)
    print('Prior attempts and evidence preserved:', base, current)
    print('Temporary key expires UTC:', expiry)
    print('Installed only; no Kubernetes action or experiment was started.')
'''


def frozen_current():
    if hashlib.sha256(CURRENT_PACKAGE.read_bytes()).hexdigest() != CURRENT_PACKAGE_SHA:
        raise ValueError('Frozen r2 package changed')
    with zipfile.ZipFile(CURRENT_PACKAGE) as archive:
        manifest = json.loads(archive.read('repair-manifest.json'))
    if manifest['install_root'] != CURRENT_ROOT or manifest['files']['hrrn_stage5_live.py'] != CURRENT_RUNNER_SHA:
        raise ValueError('Unexpected r2 baseline')
    base, _ = previous.frozen_base()
    expected = copy.deepcopy(base)
    expected['install_root'] = CURRENT_ROOT
    expected['files']['hrrn_stage5_live.py'] = CURRENT_RUNNER_SHA
    if manifest != expected:
        raise ValueError('r2 contains an unexpected change')
    return manifest


def replace_function(source, name, replacement):
    matches = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == name]
    if len(matches) != 1:
        raise ValueError('Expected one fixed bootstrap function: ' + name)
    node = matches[0]
    lines = source.splitlines(keepends=True)
    return ''.join(lines[:node.lineno - 1]) + replacement + '\n' + ''.join(lines[node.end_lineno:])


def bootstrap(base, authorization, current):
    source = previous.bootstrap(base, authorization)
    old_definition = "NEW_ROOT = '" + previous.NEW_ROOT + "'"
    if source.count(old_definition) != 1:
        raise ValueError('Unexpected fixed bootstrap layout')
    source = source.replace(old_definition, "NEW_ROOT = '" + NEW_ROOT + "'\nCURRENT_ROOT = '" + CURRENT_ROOT
                            + "'\nCURRENT_MANIFEST = json.loads(" + repr(json.dumps(current, sort_keys=True)) + ')')
    source = replace_function(source, 'validate_not_started', RECOVERY_CHECK)
    source = replace_function(source, 'install', INSTALL)
    return source.replace('Usage: python3 hrrn-stage5-live01-r2.pyz', 'Usage: python3 hrrn-stage5-image-fix.pyz')


def build(output):
    base, authorization = previous.frozen_base()
    current = frozen_current()
    runner = (ROOT / 'scripts/hrrn_stage5_live.py').read_bytes()
    updated = copy.deepcopy(base)
    updated['install_root'] = NEW_ROOT
    updated['files']['hrrn_stage5_live.py'] = hashlib.sha256(runner).hexdigest()
    source = bootstrap(base, authorization, current)
    scope = {'__name__': 'image_repair_build_validation'}
    exec(compile(source, 'image-repair-bootstrap', 'exec'), scope)
    scope['validate_change'](base, updated, runner)
    if updated['files']['hrrn_stage5_live.py'] == CURRENT_RUNNER_SHA:
        raise ValueError('Image repair must contain a changed runner')
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('xb') as raw:
        with zipfile.ZipFile(raw, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('__main__.py', source)
            archive.writestr('repair-manifest.json', json.dumps(updated, sort_keys=True, indent=2))
            archive.writestr('payload/hrrn_stage5_live.py', runner)
    scope['read_repair'](output)
    report = dict(package=str(output), package_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
                  package_bytes=output.stat().st_size, base_package_sha256=previous.BASE_PACKAGE_SHA,
                  required_previous_package_sha256=CURRENT_PACKAGE_SHA, required_previous_state='restored_empty_ledger',
                  new_runner_sha256=updated['files']['hrrn_stage5_live.py'], old_root=previous.OLD_ROOT,
                  previous_root=CURRENT_ROOT, new_root=NEW_ROOT, replacement_only='hrrn_stage5_live.py',
                  prior_attempts_modified=False, cluster_executed=False, manifest=updated)
    with output.with_suffix('.manifest.json').open('x') as stream:
        json.dump(report, stream, indent=2)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = build(args.output)
    print(json.dumps({k: v for k, v in result.items() if k != 'manifest'}, indent=2))
