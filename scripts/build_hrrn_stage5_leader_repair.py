"""Fixed, evidence-gated r3 Lease-handoff repair into an independent r4 attempt."""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('stage5_image_repair_predecessor', Path(__file__).with_name('build_hrrn_stage5_image_repair.py'))
previous = importlib.util.module_from_spec(spec)
spec.loader.exec_module(previous)
CURRENT_PACKAGE = ROOT / 'results/hrrn-k8s-stage5-20261027/execution-live01-r3/hrrn-stage5-image-fix.pyz'
CURRENT_PACKAGE_SHA = '858485a2d3183050c1d99abe73258dce468b00b0d51c54a284d988455f010951'
CURRENT_RUNNER_SHA = 'e0c1c9181ee00a4c405c8aa4bfb51181b84e2b774c01f19b3fc7b3bf3af999c9'
CURRENT_ROOT = '/home/zbs/hrrn-stage5-20261027-live01-r3'
NEW_ROOT = '/home/zbs/hrrn-stage5-20261027-live01-r4'

RECOVERY_CHECK = r'''def validate_previous_recovered(current):
    state = json.loads(safe_path(current, 'evidence/state.json').read_text())
    expected_error = "RuntimeError('Lease is not held by the observed experiment Pod')"
    if (state.get('phase') != 'restored' or state.get('restored') is not True
            or state.get('experiment_success') is not False or state.get('error') != expected_error):
        raise RuntimeError('Only the confirmed restored r3 Lease-handoff failure may be repaired')
    confirmation = json.loads(safe_path(current, 'evidence/restore-confirmation.json').read_text())
    expected_checks = {'original', 'rf', 'dqn', 'quota', 'original_config', 'rf-config', 'geometry', 'dqn_available', 'model_sha'}
    checks = confirmation.get('checks', {})
    if (set(checks) != expected_checks or not all(value is True for value in checks.values())
            or confirmation.get('original_process_sha') != BASE_MANIFEST['frozen_shas']['production_scheduler']
            or confirmation.get('cluster_audit_retained') is not True
            or confirmation.get('foreign_workloads_deleted') is not False):
        raise RuntimeError('r3 restoration confirmation is incomplete or not entirely true')
    protocol = json.loads(safe_path(current, 'protocol.json').read_text())
    if protocol.get('live_scope') != ['order-a', 'order-b'] or state.get('run_id') != protocol.get('run_id'):
        raise RuntimeError('Unexpected prior protocol or run identity')
    native = [case for case in protocol['cases'] if case['id'] == 'order-a']
    if len(native) != 1 or native[0]['mode'] != 'A' or native[0]['expected_initial_bind'] != 'long':
        raise RuntimeError('Expected one frozen native-A long-first case')
    native = native[0]
    long_pods = [obj['metadata']['name'] for obj in native['jobs']['long'] if obj['kind'] == 'Pod']
    cases = state.get('cases')
    if len(long_pods) != 1 or not isinstance(cases, list) or len(cases) != 1:
        raise RuntimeError('Prior result must contain exactly one completed native-A case')
    case = cases[0]
    if (case.get('case') != 'order-a' or case.get('mode') != 'A' or case.get('ordering_matched') is not True
            or case.get('first_pod') != long_pods[0] or case.get('expected_first_pod') != long_pods[0]):
        raise RuntimeError('Prior native-A result does not prove the frozen long-first order')
    def identity(row, uid=True):
        fields = ('kind', 'namespace', 'name', 'uid') if uid else ('kind', 'namespace', 'name')
        if any(not isinstance(row.get(key), str) or not row[key] for key in fields):
            raise RuntimeError('Incomplete object identity in the prior ledger')
        return tuple(row[key] for key in fields)
    def manifest_identity(obj):
        return obj['kind'], obj['metadata']['namespace'], obj['metadata']['name']
    allowed = {manifest_identity(obj) for obj in protocol['setup_objects']}
    allowed.add(('Lease', 'gpu-rf-system', 'volcano-hrrn-stage5'))
    allowed |= {manifest_identity(obj) for docs in native['jobs'].values() for obj in docs}
    created = state.get('created')
    planned = state.get('planned')
    if not isinstance(created, list) or not isinstance(planned, list):
        raise RuntimeError('Prior created/planned ledgers are missing')
    created_uids = [identity(row) for row in created]
    created_names = [identity(row, False) for row in created]
    planned_names = [identity(row, False) for row in planned]
    if (len(set(created_uids)) != len(created_uids) or len(set(created_names)) != len(created_names)
            or len(set(planned_names)) != len(planned_names) or set(created_names) != allowed
            or set(planned_names) != allowed or len({row['uid'] for row in created}) != len(created)):
        raise RuntimeError('Prior objects differ from setup plus native-A inputs; B/foreign submissions are forbidden')
    events_path = safe_path(current, 'evidence/events.jsonl')
    if events_path.stat().st_size > 64 * 1024 * 1024:
        raise RuntimeError('Prior event log exceeds the bounded verification size')
    events = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
    deleted = {identity(event) for event in events if event.get('event') == 'deleted'}
    if deleted != set(created_uids):
        raise RuntimeError('Every created object must have an exact UID/kind/namespace/name deleted event')
    failures = [event.get('error') for event in events if event.get('event') == 'experiment_error']
    if failures != [expected_error]:
        raise RuntimeError('Prior events do not identify the single confirmed Lease-handoff failure')
'''


def frozen_current():
    if hashlib.sha256(CURRENT_PACKAGE.read_bytes()).hexdigest() != CURRENT_PACKAGE_SHA:
        raise ValueError('Frozen r3 package changed')
    with zipfile.ZipFile(CURRENT_PACKAGE) as archive:
        manifest = json.loads(archive.read('repair-manifest.json'))
    base, _ = previous.previous.frozen_base()
    expected = copy.deepcopy(base)
    expected['install_root'] = CURRENT_ROOT
    expected['files']['hrrn_stage5_live.py'] = CURRENT_RUNNER_SHA
    if manifest != expected:
        raise ValueError('r3 contains an unexpected change')
    return manifest


def bootstrap(base, authorization, current):
    source = previous.bootstrap(base, authorization, current)
    changes = {
        "CURRENT_ROOT = '" + previous.CURRENT_ROOT + "'": "CURRENT_ROOT = '" + CURRENT_ROOT + "'",
        "NEW_ROOT = '" + previous.NEW_ROOT + "'": "NEW_ROOT = '" + NEW_ROOT + "'",
    }
    for old, new in changes.items():
        if source.count(old) != 1:
            raise ValueError('Unexpected fixed predecessor bootstrap layout')
        source = source.replace(old, new)
    source = previous.replace_function(source, 'validate_previous_recovered', RECOVERY_CHECK)
    install = previous.INSTALL.replace('prior r2 scoped', 'prior r3 scoped').replace("'authorized_keys.before-hrrn-stage5-r3-'", "'authorized_keys.before-hrrn-stage5-r4-'").replace("prefix='.hrrn-stage5-r3-auth-'", "prefix='.hrrn-stage5-r4-auth-'").replace('HRRN_STAGE5_IMAGE_REPAIR_READY', 'HRRN_STAGE5_LEADER_REPAIR_READY')
    source = previous.replace_function(source, 'install', install)
    return source.replace('Usage: python3 hrrn-stage5-image-fix.pyz', 'Usage: python3 hrrn-stage5-leader-fix.pyz')


def build(output, frozen_runner_sha):
    if not re.fullmatch('[0-9a-f]{64}', frozen_runner_sha):
        raise ValueError('A reviewed frozen runner SHA is required')
    base, authorization = previous.previous.frozen_base()
    current = frozen_current()
    runner = (ROOT / 'scripts/hrrn_stage5_live.py').read_bytes()
    runner_sha = hashlib.sha256(runner).hexdigest()
    if runner_sha != frozen_runner_sha or runner_sha == CURRENT_RUNNER_SHA:
        raise ValueError('Current runner does not match a new explicitly frozen revision')
    updated = copy.deepcopy(base)
    updated['install_root'] = NEW_ROOT
    updated['files']['hrrn_stage5_live.py'] = runner_sha
    source = bootstrap(base, authorization, current)
    scope = {'__name__': 'leader_repair_build_validation'}
    exec(compile(source, 'leader-repair-bootstrap', 'exec'), scope)
    scope['validate_change'](base, updated, runner)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('xb') as raw:
        with zipfile.ZipFile(raw, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('__main__.py', source)
            archive.writestr('repair-manifest.json', json.dumps(updated, sort_keys=True, indent=2))
            archive.writestr('payload/hrrn_stage5_live.py', runner)
    scope['read_repair'](output)
    report = dict(package=str(output), package_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
                  package_bytes=output.stat().st_size, base_package_sha256=previous.previous.BASE_PACKAGE_SHA,
                  required_previous_package_sha256=CURRENT_PACKAGE_SHA,
                  required_previous_state='restored_native_A_passed_B_not_submitted_confirmed_Lease_failure_all_objects_deleted',
                  new_runner_sha256=runner_sha, old_root=previous.previous.OLD_ROOT,
                  previous_root=CURRENT_ROOT, new_root=NEW_ROOT, replacement_only='hrrn_stage5_live.py',
                  prior_attempts_modified=False, cluster_executed=False, manifest=updated)
    with output.with_suffix('.manifest.json').open('x') as stream:
        json.dump(report, stream, indent=2)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--frozen-runner-sha', required=True)
    args = parser.parse_args()
    result = build(args.output, args.frozen_runner_sha)
    print(json.dumps({k: v for k, v in result.items() if k != 'manifest'}, indent=2))
