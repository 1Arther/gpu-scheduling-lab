"""Ready is not leader: exercise real Lease observations with a synthetic clock.

No Kubernetes, CRI, image, scheduler binary or remote clock is modified.
"""
import copy
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
    spec = importlib.util.spec_from_file_location('leader_wait_test_live', ROOT / 'scripts/hrrn_stage5_live.py')
    live = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(live)

OLD_NAME = live.DEPLOYMENT + '-old'
NEW_NAME = live.DEPLOYMENT + '-new'
OLD_HOLDER = OLD_NAME + '_11111111-1111-4111-8111-111111111111'
NEW_HOLDER = NEW_NAME + '_22222222-2222-4222-8222-222222222222'


def pod(name=NEW_NAME, uid='new-pod-uid', run_id='own-run'):
    return {'kind': 'Pod', 'metadata': {'name': name, 'uid': uid, 'namespace': live.SYSTEM_NS,
                                       'labels': {'gpu-lab/run-id': run_id, 'app': live.DEPLOYMENT}},
            'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}


def lease(holder=NEW_HOLDER, uid='own-lease-uid', duration=15):
    return {'kind': 'Lease', 'metadata': {'name': live.DEPLOYMENT, 'uid': uid, 'namespace': live.SYSTEM_NS},
            'spec': {'holderIdentity': holder, 'leaseDurationSeconds': duration,
                     'renewTime': '2026-10-27T00:00:00Z', 'acquireTime': '2026-10-27T00:00:00Z'}}


class Harness:
    def __init__(self, tmp_path, monkeypatch):
        self.clock, self.commands, self.reads = [0.0], [], 0
        self.leases = [lease()]
        self.pods, self.running = [pod()], []
        self.run = live.Run.__new__(live.Run)
        self.run.out, self.run.root, self.run.run_id = tmp_path, tmp_path, 'own-run'
        self.run.state = {'lease_uid': 'own-lease-uid', 'created': [
            {'kind': 'Deployment', 'uid': 'own-deployment-uid'},
            {'kind': 'Lease', 'uid': 'own-lease-uid'}]}
        self.run.save, self.run.event = Mock(), Mock()
        self.run.guard, self.run.process_sha = Mock(), Mock()
        self.run.patch_replicas = Mock(side_effect=self.scale)
        self.run.scheduler_pods = lambda: copy.deepcopy(self.pods)
        self.run.cmd = self.cmd
        monkeypatch.setattr(live.time, 'monotonic', lambda: self.clock[0])
        monkeypatch.setattr(live.time, 'sleep', lambda seconds: self.clock.__setitem__(0, self.clock[0] + seconds))
        monkeypatch.setattr(live.subprocess, 'run', Mock(side_effect=AssertionError('No subprocess')))
        monkeypatch.setattr(live.subprocess, 'Popen', Mock(side_effect=AssertionError('No subprocess')))

    def scale(self, name, namespace, replicas, uid):
        assert (name, namespace, uid) == (live.DEPLOYMENT, live.SYSTEM_NS, 'own-deployment-uid')
        if replicas == 0:
            self.pods = []

    def cmd(self, args, **kwargs):
        self.commands.append((list(args), kwargs))
        if args == live.K + ['rollout', 'status', 'deployment/' + live.DEPLOYMENT,
                              '-n', live.SYSTEM_NS, '--timeout=90s']:
            return types.SimpleNamespace(stdout='deployment successfully rolled out', returncode=0)
        if args == live.K + ['get', 'lease', live.DEPLOYMENT, '-n', live.SYSTEM_NS,
                              '-o', 'json', '--request-timeout=15s']:
            value = self.leases[min(self.reads, len(self.leases) - 1)]
            self.reads += 1
            if isinstance(value, Exception):
                raise value
            return types.SimpleNamespace(stdout=json.dumps(value), returncode=0)
        if args == ['sudo', '-n', 'crictl', 'ps', '-o', 'json']:
            return types.SimpleNamespace(stdout=json.dumps({'containers': self.running}), returncode=0)
        raise AssertionError('Unexpected or mutating command: ' + repr(args))

    def previous_leader_stopped(self):
        self.run.state['leader_holder_identities'] = {OLD_NAME: OLD_HOLDER}
        self.run.state['leader_pod_uids'] = {OLD_NAME: 'old-pod-uid'}
        self.run.state['stopped_scheduler_pods'] = {OLD_NAME: {
            'uid': 'old-pod-uid', 'api_absent': True, 'current_cri_absent': True}}

    def summaries(self):
        return [json.loads(p.read_text()) for p in (self.run.out / 'leader-acquisition').glob('*/summary.json')]


@pytest.fixture
def harness(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


def test_ready_pod_waits_for_empty_lease_to_be_acquired(harness):
    harness.leases = [lease('', duration=None), lease('', duration=None), lease()]
    harness.run.scale_experiment(1)
    assert harness.reads == 3 and harness.clock[0] == 2
    assert harness.run.guard.call_count == 3
    summary = harness.summaries()[0]
    assert summary['status'] == 'acquired'
    assert summary['holder_identity'] == NEW_HOLDER
    assert summary['elapsed_seconds'] == 2
    assert not summary['readiness_is_leadership'] and not summary['lease_or_clock_modified']
    assert 'case_deadline_epoch' not in harness.run.state


def test_observed_old_leader_requires_scale_zero_exit_proof_then_transfers(harness):
    harness.run.state['leader_holder_identities'] = {OLD_NAME: OLD_HOLDER}
    harness.run.state['leader_pod_uids'] = {OLD_NAME: 'old-pod-uid'}
    harness.pods = [pod(OLD_NAME, 'old-pod-uid')]
    harness.run.scale_experiment(0)
    stopped = harness.run.state['stopped_scheduler_pods'][OLD_NAME]
    assert stopped['api_absent'] and stopped['current_cri_absent']
    harness.pods = [pod()]
    harness.leases = [lease(OLD_HOLDER), lease(OLD_HOLDER), lease()]
    harness.run.scale_experiment(1)
    assert harness.reads == 3 and harness.clock[0] == 2
    assert harness.run.state['leader_pod_uids'][NEW_NAME] == 'new-pod-uid'
    assert harness.summaries()[0]['wait_budget_seconds'] == 25


@pytest.mark.parametrize('holder', ['foreign_123', OLD_NAME + '_different-process'])
def test_unknown_holder_is_immediate_failure(harness, holder):
    harness.previous_leader_stopped()
    harness.leases = [lease(holder)]
    with pytest.raises(RuntimeError, match='unknown or unverified'):
        harness.run.scale_experiment(1)
    assert harness.reads == 1 and harness.clock[0] == 0
    assert harness.summaries()[0]['status'] == 'failed'


@pytest.mark.parametrize('missing', ['api_absent', 'current_cri_absent', 'uid'])
def test_old_holder_without_complete_stop_proof_is_rejected(harness, missing):
    harness.previous_leader_stopped()
    harness.run.state['stopped_scheduler_pods'][OLD_NAME].pop(missing)
    harness.leases = [lease(OLD_HOLDER)]
    with pytest.raises(RuntimeError, match='unknown or unverified'):
        harness.run.scale_experiment(1)
    assert harness.reads == 1


def test_reused_pod_name_with_different_stopped_uid_is_not_proof(harness):
    harness.previous_leader_stopped()
    harness.run.state['stopped_scheduler_pods'][OLD_NAME]['uid'] = 'recreated-other-pod'
    harness.leases = [lease(OLD_HOLDER)]
    with pytest.raises(RuntimeError, match='unknown or unverified'):
        harness.run.scale_experiment(1)


def test_lease_uid_change_even_for_current_holder_is_rejected(harness):
    harness.leases = [lease(uid='replaced-lease')]
    with pytest.raises(RuntimeError, match='UID changed'):
        harness.run.scale_experiment(1)
    assert harness.reads == 1
    observation = next((harness.run.out / 'leader-acquisition').glob('*/0001.json'))
    assert json.loads(observation.read_text())['lease']['metadata']['uid'] == 'replaced-lease'


def test_lease_uid_must_be_recorded_before_any_scale(harness):
    harness.run.state.pop('lease_uid')
    with pytest.raises(RuntimeError, match='pre-created'):
        harness.run.scale_experiment(1)
    harness.run.patch_replicas.assert_not_called()


@pytest.mark.parametrize('duration,expected', [(15, 25), (60, 75), (600, 120)])
def test_expiry_wait_is_based_on_observed_duration_and_capped(harness, duration, expected):
    harness.previous_leader_stopped()
    harness.leases = [lease(OLD_HOLDER, duration=duration)]
    with pytest.raises(RuntimeError, match='Timed out'):
        harness.run.scale_experiment(1)
    assert harness.clock[0] == expected
    assert harness.summaries()[0]['wait_budget_seconds'] == expected
    assert harness.summaries()[0]['status'] == 'failed'


def test_empty_lease_never_acquiring_has_120_second_deadline(harness):
    harness.leases = [lease('', duration=None)]
    with pytest.raises(RuntimeError, match='Timed out'):
        harness.run.scale_experiment(1)
    assert harness.clock[0] == 120


@pytest.mark.parametrize('duration', [None, -1, 0, True, '15'])
def test_held_lease_with_invalid_duration_fails_immediately(harness, duration):
    harness.leases = [lease(duration=duration)]
    with pytest.raises(RuntimeError, match='valid duration'):
        harness.run.scale_experiment(1)
    assert harness.clock[0] == 0


def test_guard_expiration_or_foreign_request_interrupts_leader_wait(harness):
    harness.previous_leader_stopped()
    harness.leases = [lease(OLD_HOLDER)]
    harness.run.guard.side_effect = [None, RuntimeError('Bounded maintenance window exceeded')]
    with pytest.raises(RuntimeError, match='maintenance window'):
        harness.run.scale_experiment(1)
    assert harness.reads == 1 and harness.clock[0] == 1


def test_lease_read_failure_is_not_swallowed(harness):
    harness.leases = [RuntimeError('API unavailable')]
    with pytest.raises(RuntimeError, match='API unavailable'):
        harness.run.scale_experiment(1)
    assert harness.clock[0] == 0


def test_running_old_process_prevents_stop_proof(harness):
    harness.pods = [pod(OLD_NAME, 'old-pod-uid')]
    harness.running = [{'labels': {'io.kubernetes.pod.namespace': live.SYSTEM_NS,
                                   'io.kubernetes.pod.name': OLD_NAME}}]
    with pytest.raises(RuntimeError, match='process still running'):
        harness.run.scale_experiment(0)
    assert not harness.run.state.get('stopped_scheduler_pods')


def test_foreign_labeled_scheduler_is_not_adopted_as_stopped(harness):
    harness.pods = [pod(OLD_NAME, 'old-pod-uid', 'not-this-run')]
    with pytest.raises(RuntimeError, match='identity before scale-down'):
        harness.run.scale_experiment(0)
    harness.run.patch_replicas.assert_not_called()


def test_r3_observed_old_holder_identity_is_waited_for_not_declared_leader(harness):
    # Identity comes from r3 failure-before-stop-scheduler.log:58; Pod UIDs from
    # state.observations (A) and commands/000614.json CRI sandbox metadata (B).
    # 15 seconds is a logged runtime flag, NOT an observed Lease.spec field.
    # This Lease response and both exit-proof booleans are synthetic test data.
    old_name = 'volcano-hrrn-stage5-57ddcc579-562bl'
    old_uid = '6538e54e-db2f-405e-9550-bd678a47723e'
    old_holder = old_name + '_ff5b9216-45eb-4e65-8d97-9f517cd1b641'
    new_name = 'volcano-hrrn-stage5-6946c96f4b-2msll'
    new_uid = 'f5127a12-68de-4a8f-88ba-31125129c03a'
    lease_uid = 'd0db38d8-ead0-40e3-94d4-b4e29a281faf'
    harness.pods = [pod(new_name, new_uid)]
    harness.run.state.update(
        lease_uid=lease_uid,
        leader_holder_identities={old_name: old_holder},
        leader_pod_uids={old_name: old_uid},
        stopped_scheduler_pods={old_name: dict(uid=old_uid, api_absent=True, current_cri_absent=True)})
    harness.leases = [lease(old_holder, uid=lease_uid, duration=15)]
    with pytest.raises(RuntimeError, match='Timed out'):
        harness.run.scale_experiment(1)
    assert harness.clock[0] == 25
    assert harness.reads > 1
    assert new_name not in harness.run.state['leader_holder_identities']
    assert harness.summaries()[0]['status'] == 'failed'
