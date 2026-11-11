"""Checkpoint receipts are evidence, not a live allocation ledger.

All cluster/runtime reads are synthetic; no remote command or process executes.
"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import types
from unittest.mock import Mock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
from gpu_lab import staging

imports = {'staging': staging}
try:
    import fcntl
except ImportError:
    imports['fcntl'] = types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8, flock=Mock())
with patch.dict(sys.modules, imports):
    spec = importlib.util.spec_from_file_location('checkpoint_test_live', ROOT / 'scripts/hrrn_stage5_live.py')
    live = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(live)


OWN = '00000000-0000-4000-8000-000000000001'
HISTORY = {
    'PodUID': '161c2cb4-796b-4427-a3d8-13e410029061',
    'ContainerName': 'sleep', 'ResourceName': 'volcano.sh/vgpu-number',
    'DeviceIDs': ['GPU-fb6aaeaf-9ca4-3c9a-7b3b-f56714c1f7cd-6'],
    'device_id_count': 1, 'device_ids_truncated': False,
    'allocation_response_sha256': 'b6cea25653b526a5ef86695286938d1df163544d3aa1099d6abe2e2351de12a8',
    'allocate_envs': {'CUDA_DEVICE_MEMORY_LIMIT_0': '64m',
                      'NVIDIA_VISIBLE_DEVICES': 'GPU-7b94f673-ce00-719b-2c94-706baa032dae',
                      'CUDA_DEVICE_SM_LIMIT': '100'},
    'consumed_uuid_source': 'kubelet_checkpoint_allocate_response',
}


def classify(entries=(), own=(), **views):
    return live.classify_checkpoint_receipts(list(entries), views.get('api', ()), views.get('kubelet', ()),
                                            views.get('cri', ()), views.get('sandboxes', ()), own)


def receipt(uid=OWN):
    return {'PodUID': uid, 'ContainerName': 'sleep', 'ResourceName': 'volcano.sh/vgpu-number',
            'DeviceIDs': {'0': ['synthetic-slot']}, 'AllocResp': ''}


def pod(uid):
    return {'metadata': {'uid': uid, 'name': 'synthetic', 'namespace': live.TEST_NS}}


def sandbox(uid, state='SANDBOX_NOTREADY'):
    return {'id': 'sandbox-' + uid, 'metadata': {'uid': uid}, 'state': state}


def container(uid, state='CONTAINER_EXITED'):
    return {'id': 'container-' + uid, 'podSandboxId': 'sandbox-' + uid, 'state': state,
            'labels': {'io.kubernetes.pod.uid': uid}}


class Reads:
    def __init__(self, tmp_path):
        self.run = live.Run.__new__(live.Run)
        self.run.out, self.run.state, self.run.counter = tmp_path, {}, 0
        self.run.cmd = self.command
        self.raw = json.dumps({'Data': {'PodDeviceEntries': [], 'RegisteredDevices': {}}})
        self.api, self.kubelet, self.all_cri, self.current, self.sandboxes = [], [], [], [], []
        self.calls, self.fail, self.stat_change, self.bad_stat_size = [], None, False, False
        self.stat_count = 0

    def command(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        if args[:4] == ['sudo', '-n', 'stat', '-c']:
            key = 'stat'
            self.stat_count += 1
            size = len(self.raw.encode()) + int(self.bad_stat_size)
            ino = 2 if self.stat_change and self.stat_count % 2 == 0 else 1
            text = f'{size} 1790000000 2026-09-21 00:00:00.000000000 +0000 {ino}'
        elif args == ['sudo', '-n', 'cat', '/var/lib/kubelet/device-plugins/kubelet_internal_checkpoint']:
            key, text = 'checkpoint', self.raw
        elif args == live.K + ['get', 'pods', '-A', '-o', 'json', '--request-timeout=15s']:
            key, text = 'api', json.dumps({'items': self.api})
        elif args == live.K + ['get', '--raw=/api/v1/nodes/t3dgq/proxy/pods', '--request-timeout=15s']:
            key, text = 'kubelet', json.dumps({'items': self.kubelet})
        elif args == ['sudo', '-n', 'crictl', 'ps', '-a', '-o', 'json']:
            key, text = 'cri_all', json.dumps({'containers': self.all_cri})
        elif args == ['sudo', '-n', 'crictl', 'ps', '-o', 'json']:
            key, text = 'cri_current', json.dumps({'containers': self.current})
        elif args == ['sudo', '-n', 'crictl', 'pods', '-o', 'json']:
            key, text = 'sandboxes', json.dumps({'items': self.sandboxes})
        elif args == ['sudo', '-n', 'journalctl', '-u', 'kubelet', '--since', '30 minutes ago',
                      '-n', '400', '--no-pager', '-o', 'short-iso']:
            key, text = 'journal', 'synthetic kubelet history; never edited\n'
        else:
            raise AssertionError('Unexpected or mutating command: ' + repr(args))
        if key == self.fail:
            raise RuntimeError('Synthetic read failure: ' + key)
        return types.SimpleNamespace(stdout=text, stderr='', returncode=0)

    def own_checkpoint(self):
        self.raw = json.dumps({'Data': {'PodDeviceEntries': [receipt()], 'RegisteredDevices': {}}})


@pytest.fixture
def reads(tmp_path, monkeypatch):
    monkeypatch.setattr(live.subprocess, 'run', Mock(side_effect=AssertionError('No real subprocess')))
    monkeypatch.setattr(live.subprocess, 'Popen', Mock(side_effect=AssertionError('No real subprocess')))
    return Reads(tmp_path)


def test_exact_saved_history_requires_no_live_observation():
    row = classify([HISTORY])
    assert row['accepted']
    assert row['entries'][0]['classification'] == 'verified_round8_saved_summary_history'
    assert row['entries'][0]['parsed_record_sha256'] == '837585444e96733982d3fcede71a3ac4c09fbb53b1817631dbbefc0840eb1030'
    assert not row['kubelet_device_manager_memory_directly_observed']
    assert not row['full_historical_raw_device_id_sets_proven_equal']
    assert not row['disk_checkpoint_is_live_allocation_table']


@pytest.mark.parametrize('view', ['api', 'kubelet', 'cri', 'sandboxes'])
def test_history_active_in_any_view_is_rejected_even_without_disk_entry(view):
    for entries in ([HISTORY], []):
        row = classify(entries, **{view: [HISTORY['PodUID']]})
        assert not row['accepted']
        assert row['active_relevant_uids'] == [HISTORY['PodUID']]


@pytest.mark.parametrize('change', ['PodUID', 'DeviceIDs', 'device_id_count', 'allocation_response_sha256'])
def test_changed_history_or_unknown_uid_rejected(change):
    changed = copy.deepcopy(HISTORY)
    changed[change] = 'changed'
    row = classify([changed])
    assert not row['accepted']
    assert row['entries'][0]['classification'] == 'unknown_or_changed_receipt'


def test_duplicates_and_missing_identity_rejected():
    assert not classify([HISTORY, HISTORY])['accepted']
    assert not classify([{'PodUID': OWN}], own=[OWN])['accepted']


def test_own_persistent_receipt_after_confirmed_removal_is_accepted():
    row = classify([receipt()], own=[OWN])
    assert row['accepted']
    assert row['entries'][0]['classification'] == 'experiment_receipt_after_confirmed_workload_removal'


@pytest.mark.parametrize('view', ['api', 'kubelet', 'cri', 'sandboxes'])
def test_own_active_in_any_view_is_rejected_even_without_disk_entry(view):
    for entries in ([receipt()], []):
        assert not classify(entries, own=[OWN], **{view: [OWN]})['accepted']


def test_reconciliation_preserves_raw_stat_journal_and_stopped_runtime_records(reads):
    reads.own_checkpoint()
    reads.all_cri = [container(OWN)]
    reads.sandboxes = [sandbox(OWN)]
    report = reads.run.reconcile_checkpoint('removed', [OWN])
    assert report['accepted']
    raw = reads.run.out / report['checkpoint']['raw_evidence_file']
    assert raw.read_bytes() == reads.raw.encode()
    assert report['checkpoint']['raw_sha256'] == hashlib.sha256(raw.read_bytes()).hexdigest()
    assert report['checkpoint']['stat_before'] == report['checkpoint']['stat_after']
    views = json.loads((reads.run.out / 'checkpoint-reconciliation/removed-views.json').read_text())
    assert views['cri_all']['containers'] == reads.all_cri
    assert views['cri_sandboxes']['items'] == reads.sandboxes
    assert 'never edited' in views['kubelet_recent_journal']
    assert all(options['capture_full'] for _, options in reads.calls)
    assert len(reads.calls) == 9


@pytest.mark.parametrize('read_name', ['stat', 'checkpoint', 'api', 'kubelet', 'cri_all', 'cri_current', 'sandboxes', 'journal'])
def test_every_required_read_failure_is_terminal(reads, read_name):
    reads.fail = read_name
    with pytest.raises(RuntimeError, match='Synthetic read failure'):
        reads.run.reconcile_checkpoint('failed')
    assert not (reads.run.out / 'checkpoint-reconciliation/failed.json').exists()


@pytest.mark.parametrize('field', ['PodDeviceEntries', 'RegisteredDevices'])
def test_go_nil_collections_are_valid_empty_checkpoint(reads, field):
    data = {'PodDeviceEntries': [], 'RegisteredDevices': {}}
    data[field] = None  # Go's nil slice/map JSON representation.
    reads.raw = json.dumps({'Data': data})
    assert reads.run.reconcile_checkpoint('nil')['accepted']


@pytest.mark.parametrize('data', [{}, {'PodDeviceEntries': []}, {'PodDeviceEntries': 'bad', 'RegisteredDevices': {}},
                                  {'PodDeviceEntries': [], 'RegisteredDevices': []}])
def test_missing_or_wrong_checkpoint_fields_block(reads, data):
    reads.raw = json.dumps({'Data': data})
    with pytest.raises(RuntimeError, match='Malformed'):
        reads.run.reconcile_checkpoint('bad')
    assert len(list((reads.run.out / 'checkpoints').glob('*-raw-*.json'))) == 1


def test_changed_stat_or_byte_size_blocks_and_keeps_evidence(reads):
    reads.stat_change = True
    with pytest.raises(RuntimeError, match='changed during capture'):
        reads.run.checkpoint('changed')
    reads.stat_change, reads.bad_stat_size = False, True
    with pytest.raises(RuntimeError, match='stat size'):
        reads.run.checkpoint('size')
    assert len(list((reads.run.out / 'checkpoints').glob('*-raw-*.json'))) == 2


@pytest.mark.parametrize('view', ['kubelet', 'cri', 'sandbox'])
def test_only_api_deleted_own_uid_is_retryable(reads, view):
    reads.own_checkpoint()
    if view == 'kubelet':
        reads.kubelet = [pod(OWN)]
    elif view == 'cri':
        reads.all_cri = [container(OWN, 'CONTAINER_RUNNING')]
    else:
        reads.sandboxes = [sandbox(OWN, 'SANDBOX_READY')]
    with pytest.raises(live.OwnWorkloadTerminating):
        reads.run.reconcile_checkpoint('terminating', [OWN])
    report = json.loads((reads.run.out / 'checkpoint-reconciliation/terminating.json').read_text())
    assert not report['accepted'] and report['retryable_own_termination']
    reads.api = [pod(OWN)]
    with pytest.raises(RuntimeError) as exc:
        reads.run.reconcile_checkpoint('still-api', [OWN])
    assert not isinstance(exc.value, live.OwnWorkloadTerminating)


def test_unknown_receipt_is_not_retried_alongside_own_termination(reads):
    reads.raw = json.dumps({'Data': {'PodDeviceEntries': [receipt('unknown')], 'RegisteredDevices': {}}})
    reads.kubelet = [pod(OWN)]
    with pytest.raises(RuntimeError) as exc:
        reads.run.wait_checkpoint_reconciliation('unknown', [OWN])
    assert not isinstance(exc.value, live.OwnWorkloadTerminating)
    assert len(list((reads.run.out / 'checkpoint-reconciliation').glob('*-views.json'))) == 1


def test_history_live_is_not_retried_in_recovery(reads):
    reads.kubelet = [pod(HISTORY['PodUID'])]
    with pytest.raises(RuntimeError) as exc:
        reads.run.wait_checkpoint_reconciliation('history-active', [OWN])
    assert not isinstance(exc.value, live.OwnWorkloadTerminating)


def test_unidentifiable_cri_container_blocks(reads):
    reads.all_cri = [{'id': 'unknown-stopped', 'state': 'CONTAINER_EXITED'}]
    with pytest.raises(RuntimeError, match='Unresolved'):
        reads.run.reconcile_checkpoint('unknown-cri')


def test_container_uid_can_be_resolved_from_its_sandbox(reads):
    reads.own_checkpoint()
    reads.all_cri = [container(OWN)]
    reads.all_cri[0].pop('labels')
    reads.sandboxes = [sandbox(OWN)]
    assert reads.run.reconcile_checkpoint('sandbox-identity', [OWN])['accepted']


def test_retry_preserves_every_observation_then_accepts_without_disk_edit(reads, monkeypatch):
    reads.own_checkpoint()
    reads.kubelet = [pod(OWN)]
    clock = [0.0]
    def finish(seconds):
        clock[0] += seconds
        reads.kubelet = []
    monkeypatch.setattr(live.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(live.time, 'sleep', finish)
    original = reads.raw
    report = reads.run.wait_checkpoint_reconciliation('async', [OWN])
    assert report['accepted'] and reads.raw == original
    assert clock[0] == 2
    assert len(list((reads.run.out / 'checkpoint-reconciliation').glob('*-views.json'))) == 2


def test_retry_deadline_is_70_seconds_and_does_not_swallow_read_failure(reads, monkeypatch):
    reads.kubelet = [pod(OWN)]
    clock = [0.0]
    monkeypatch.setattr(live.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(live.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    with pytest.raises(RuntimeError, match='Timed out'):
        reads.run.wait_checkpoint_reconciliation('bounded', [OWN])
    assert clock[0] == 70
    assert max(options['timeout'] for _, options in reads.calls) <= 35
    reads.calls.clear()
    reads.fail = 'kubelet'
    with pytest.raises(RuntimeError, match='Synthetic read failure'):
        reads.run.wait_checkpoint_reconciliation('read-failed', [OWN])
    assert clock[0] == 70
    assert len(reads.calls) == 5


def r3_recorded_checkpoint():
    # Preserved real evidence fixture, never fetched from or written to a server.
    folder = ROOT / 'results/live-r3-a/evidence/checkpoints'
    summary = json.loads((folder / 'restore-607-1.json').read_text(encoding='utf-8'))
    raw = json.loads((folder.parent / summary['raw_evidence_file']).read_text(encoding='utf-8'))
    inner = raw['Data']
    return raw, json.loads(inner) if isinstance(inner, str) else inner


def test_r3_all_twelve_complete_records_and_reviewed_manifest_match(reads):
    raw, inner = r3_recorded_checkpoint()
    review = json.loads((ROOT / 'results/hrrn-k8s-stage5-20261027/execution-live01-r3/'
                        'r3-released-a-checkpoint-history.json').read_text(encoding='utf-8'))
    assert live.R3_CHECKPOINT_HISTORY == {uid: set(rows) for uid, rows in review['allowlist'].items()}
    assert len(inner['PodDeviceEntries']) == 12
    reads.raw = json.dumps(raw)
    report = reads.run.reconcile_checkpoint('r3-recorded')
    assert report['accepted']
    assert len(report['entries']) == 12
    assert set(report['r3_full_raw_records_verified_uids']) == set(live.R3_CHECKPOINT_HISTORY)
    assert {row['classification'] for row in report['entries']} == {'verified_r3_saved_full_record_history'}
    assert not report['kubelet_device_manager_memory_directly_observed']


@pytest.mark.parametrize('view', ['api', 'kubelet', 'cri', 'sandbox'])
def test_r3_active_in_any_view_blocks_even_without_a_disk_receipt(reads, view):
    uid = next(iter(live.R3_CHECKPOINT_HISTORY))
    if view == 'api':
        reads.api = [pod(uid)]
    elif view == 'kubelet':
        reads.kubelet = [pod(uid)]
    elif view == 'cri':
        reads.all_cri = [container(uid, 'CONTAINER_RUNNING')]
    else:
        reads.sandboxes = [sandbox(uid, 'SANDBOX_READY')]
    with pytest.raises(RuntimeError, match='Unresolved'):
        reads.run.reconcile_checkpoint('r3-active')


@pytest.mark.parametrize('mutation', ['tail', 'response', 'duplicate', 'container'])
def test_r3_full_record_changes_fail_closed_and_preserve_raw(reads, mutation):
    _, inner = r3_recorded_checkpoint()
    rows = inner['PodDeviceEntries']
    row = next(x for x in rows if x['ResourceName'] == 'volcano.sh/vgpu-memory')
    if mutation == 'tail':
        ids = next(iter(row['DeviceIDs'].values()))
        assert len(ids) > 8
        ids[-1] = 'changed-beyond-parsed-prefix'
    elif mutation == 'response':
        row['AllocResp'] = 'changed'
    elif mutation == 'duplicate':
        rows.append(copy.deepcopy(row))
    else:
        row['ContainerName'] = 'changed'
    reads.raw = json.dumps({'Data': inner})
    with pytest.raises(RuntimeError, match='full r3 historical'):
        reads.run.reconcile_checkpoint('r3-changed')
    files = list((reads.run.out / 'checkpoints').glob('*-raw-*.json'))
    assert len(files) == 1 and files[0].read_text() == reads.raw


def test_r3_parsed_summary_alone_never_authorizes_receipt():
    raw, _ = r3_recorded_checkpoint()
    entries = live.checkpoint(raw)['entries']
    assert not classify(entries)['accepted']
