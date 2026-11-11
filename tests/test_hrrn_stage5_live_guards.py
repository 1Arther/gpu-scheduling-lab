"""Mocked Stage5 runner safety boundaries; these are not real-cluster tests.

No subprocess or Kubernetes client is allowed to execute.  The Linux-only
fcntl import is stubbed on Windows only while loading the runner module.
"""
import importlib.util
import json
import copy
from pathlib import Path
import sys
import types
from unittest.mock import Mock, patch

import pytest

from gpu_lab import staging


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
_imports = {'staging': staging}
try:
    import fcntl  # noqa: F401
except ImportError:
    _imports['fcntl'] = types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8,
                                             flock=Mock())
with patch.dict(sys.modules, _imports):
    _spec = importlib.util.spec_from_file_location(
        'stage5_live_mocked_guards', ROOT / 'scripts/hrrn_stage5_live.py')
    live = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(live)


def make_pod(uid='own-pod', name='candidate', *, owner='run-test', phase='Pending',
             gpu=True, bound=False, annotations=None):
    limits = {'volcano.sh/vgpu-number': '1', 'volcano.sh/vgpu-cores': '100',
              'volcano.sh/vgpu-memory': '1024'} if gpu else {}
    return {'apiVersion': 'v1', 'kind': 'Pod',
            'metadata': {'name': name, 'namespace': live.TEST_NS, 'uid': uid,
                         'labels': {'gpu-lab/run-id': owner},
                         'annotations': annotations or {}},
            'spec': {'containers': [{'name': 'sleep', 'resources': {'limits': limits}}],
                     **({'nodeName': 't3dgq'} if bound else {})},
            'status': {'phase': phase}}


@pytest.fixture
def run(tmp_path, monkeypatch):
    runner = live.Run.__new__(live.Run)
    runner.root = tmp_path
    runner.out = tmp_path / 'evidence'
    runner.out.mkdir()
    runner.run_id = 'run-test'
    runner.recovering = False
    runner.state = {'created': [], 'planned': [], 'cases': [], 'observations': {},
                    'phase': 'prepared'}
    runner.protocol = {'cases': []}
    runner.save = Mock()
    runner.event = Mock()
    runner.observe = Mock()
    runner.cmd = Mock(side_effect=AssertionError('No real command permitted in mocked tests'))
    runner.get = Mock(side_effect=AssertionError('Unexpected read in mocked test'))
    monkeypatch.setattr(live.subprocess, 'run', Mock(side_effect=AssertionError('No subprocess')))
    monkeypatch.setattr(live.subprocess, 'Popen', Mock(side_effect=AssertionError('No subprocess')))
    return runner


def test_delete_refuses_unknown_uid_even_when_run_label_matches(run):
    pod = make_pod()
    run.get.return_value = pod
    run.get.side_effect = None
    with pytest.raises(RuntimeError):
        run.delete(live.pod_ref(pod))
    run.cmd.assert_not_called()


def test_delete_refuses_replaced_object_uid(run):
    original = make_pod(uid='original')
    run.state['created'] = [live.pod_ref(original)]
    run.get.side_effect = None
    run.get.return_value = make_pod(uid='replacement')
    with pytest.raises(RuntimeError):
        run.delete(live.pod_ref(original))
    run.cmd.assert_not_called()


def test_delete_refuses_foreign_label_even_when_uid_is_recorded(run):
    pod = make_pod(owner='another-run')
    run.state['created'] = [live.pod_ref(pod)]
    run.get.side_effect = None
    run.get.return_value = pod
    with pytest.raises(RuntimeError):
        run.delete(live.pod_ref(pod))
    run.cmd.assert_not_called()


def test_delete_uses_server_side_uid_precondition(run):
    pod = make_pod()
    row = live.pod_ref(pod)
    run.state['created'] = [row]
    run.get.side_effect = [pod, None]
    run.cmd.side_effect = None
    run.wait = lambda condition, *_args, **_kwargs: condition()
    run.delete(row)
    argv, body = run.cmd.call_args.args
    assert body['preconditions'] == {'uid': 'own-pod'}
    assert '--raw=/api/v1/namespaces/gpu-rf-tests/pods/candidate' in argv
    assert body['propagationPolicy'] == 'Foreground'


@pytest.mark.parametrize('variant', ['ordinary_gpu', 'vgpu', 'dra', 'init_container'])
def test_guard_rejects_foreign_gpu_or_dra_request_even_with_copied_run_label(run, variant):
    pod = make_pod(uid='foreign', gpu=False)
    if variant == 'ordinary_gpu':
        pod['spec']['containers'][0]['resources']['requests'] = {'nvidia.com/gpu': '1'}
    elif variant == 'vgpu':
        pod['spec']['containers'][0]['resources']['limits'] = {'volcano.sh/vgpu-number': '1'}
    elif variant == 'dra':
        pod['spec']['resourceClaims'] = [{'name': 'gpu-claim'}]
    else:
        pod['spec']['initContainers'] = [{'name': 'init', 'resources': {'limits': {'nvidia.com/mig-1g.5gb': '1'}}}]
    run.get.side_effect = None
    run.get.return_value = {'items': [pod]}
    with pytest.raises(RuntimeError):
        run.guard()
    run.cmd.assert_not_called()


def test_guard_allows_exact_recorded_pod_uid_and_cpu_workload(run):
    own = make_pod()
    foreign_cpu = make_pod(uid='foreign-cpu', owner='other', gpu=False)
    run.state['created'] = [live.pod_ref(own)]
    run.get.side_effect = None
    run.get.return_value = {'items': [own, foreign_cpu]}
    assert run.guard() == [own, foreign_cpu]
    run.cmd.assert_not_called()


def test_guard_rejects_consumable_foreign_allocation_on_terminal_pod(run):
    stale = make_pod(uid='stale', owner='other', phase='Succeeded', annotations={
        'volcano.sh/vgpu-node': 't3dgq', 'volcano.sh/vgpu-time': '12345',
        'volcano.sh/bind-phase': 'allocating',
        'volcano.sh/devices-to-allocate': 'GPU-x,NVIDIA,1024,100:'})
    run.get.side_effect = None
    run.get.return_value = {'items': [stale]}
    with pytest.raises(RuntimeError):
        run.guard()
    run.cmd.assert_not_called()


def test_guard_allows_terminal_historical_annotations_with_no_unconsumed_allocation(run):
    old = make_pod(uid='old', owner='other', phase='Succeeded', annotations={
        'volcano.sh/vgpu-node': 't3dgq', 'volcano.sh/vgpu-time': str(2**64-1),
        'volcano.sh/vgpu-ids-new': 'GPU-x,NVIDIA,1024,100:'})
    run.get.side_effect = None
    run.get.return_value = {'items': [old]}
    assert run.guard() == [old]


@pytest.mark.parametrize('changed', ['token', 'owner'])
def test_adopt_ambiguous_create_refuses_identity_mismatch(run, changed):
    pod = make_pod(annotations={'gpu-lab/create-token': 'intent-token'})
    plan = {'kind': 'Pod', 'name': 'candidate', 'namespace': live.TEST_NS,
            'create_token': 'intent-token'}
    run.state['planned'] = [plan]
    if changed == 'token':
        pod['metadata']['annotations']['gpu-lab/create-token'] = 'somebody-else'
    else:
        pod['metadata']['labels']['gpu-lab/run-id'] = 'another-run'
    run.get.side_effect = None
    run.get.return_value = pod
    with pytest.raises(RuntimeError):
        run.adopt_pending_creates()
    assert run.state['created'] == []
    run.cmd.assert_not_called()


def test_adopt_ambiguous_create_accepts_exact_durable_intent_token(run):
    pod = make_pod(annotations={'gpu-lab/create-token': 'intent-token'})
    run.state['planned'] = [{'kind': 'Pod', 'name': 'candidate', 'namespace': live.TEST_NS,
                             'create_token': 'intent-token'}]
    run.get.side_effect = None
    run.get.return_value = pod
    run.adopt_pending_creates()
    assert run.state['created'] == [live.pod_ref(pod)]
    run.save.assert_called_once()
    run.cmd.assert_not_called()


def test_pending_unbound_cleanup_does_not_require_native_release_proof(run):
    pod = make_pod()
    run.delete = Mock()
    run.logs = Mock(side_effect=AssertionError('Never allocated Pod needs no release log'))
    run.scheduler_pods = Mock(return_value=[make_pod(uid='scheduler')])
    run.get.side_effect = None
    run.get.return_value = {'metadata': {'annotations': {}}}
    run.wait = lambda condition, *_args, **_kwargs: condition()
    run.delete_pod(pod)
    run.delete.assert_called_once_with(live.pod_ref(pod))
    run.logs.assert_not_called()


def test_foreign_gpu_guard_stops_new_work_but_allows_recovery_reads(run):
    run.recovering = True
    foreign = make_pod(uid='foreign', owner='other')
    run.get.side_effect = None
    run.get.return_value = {'items': [foreign]}
    assert run.guard() == [foreign]
    run.cmd.assert_not_called()


def test_enabled_complete_job_case_rejected_before_any_execution(run):
    run.protocol['cases'] = [{'kind': 'complete', 'mode': 'C', 'execution_enabled': True}]
    with pytest.raises(RuntimeError):
        run.execute()
    assert run.state['phase'] == 'prepared'
    run.cmd.assert_not_called()


def _enabled_order(mode):
    return {'id': 'order-' + mode.lower(), 'mode': mode, 'kind': 'order',
            'execution_enabled': True}


def test_live_scope_accepts_exactly_one_a_and_b_with_documented_disabled_c():
    cases = [_enabled_order('A'), _enabled_order('B'),
             {'id': 'complete-c', 'mode': 'C', 'kind': 'complete', 'execution_enabled': False}]
    assert live.validate_execution_scope(cases) == cases[:2]


@pytest.mark.parametrize('cases', [
    [], [_enabled_order('A')],
    [_enabled_order('A'), _enabled_order('A')],
    [_enabled_order('A'), _enabled_order('B'), _enabled_order('B')],
    [_enabled_order('A'), _enabled_order('C')],
    [_enabled_order('A'), {**_enabled_order('B'), 'kind': 'complete'}],
    [_enabled_order('A'), {**_enabled_order('B'), 'id': 'unreviewed-case'}],
])
def test_live_scope_rejects_missing_duplicate_or_unreviewed_cases(cases):
    with pytest.raises(RuntimeError):
        live.validate_execution_scope(cases)


def _worker():
    return {'pid': 123, 'start_ticks': 150, 'uid': 1001,
            'argv': ['/usr/bin/python3', '/test/hrrn_stage5_live.py', '--execute']}


@pytest.mark.parametrize('change', ['reused_pid', 'owner', 'command', 'arguments'])
def test_watchdog_never_signals_reused_or_unrelated_process(monkeypatch, change):
    expected = _worker()
    actual = {**expected, 'argv': list(expected['argv'])}
    if change == 'reused_pid':
        actual['start_ticks'] += 1
    elif change == 'owner':
        actual['uid'] = 1002
    elif change == 'command':
        actual['argv'][1] = '/somebody-else/task.py'
    else:
        actual['argv'][-1] = '--prepare'
    monkeypatch.setattr(live, 'process_identity', Mock(return_value=actual))
    monkeypatch.setattr(live.os, 'getuid', lambda: 1001, raising=False)
    kill = Mock()
    monkeypatch.setattr(live.os, 'kill', kill)
    with pytest.raises(RuntimeError):
        live.signal_owned_worker(expected, 15, '/test/hrrn_stage5_live.py')
    kill.assert_not_called()


def test_watchdog_signal_requires_current_exact_identity(monkeypatch):
    expected = _worker()
    monkeypatch.setattr(live, 'process_identity', Mock(return_value=expected))
    monkeypatch.setattr(live.os, 'getuid', lambda: 1001, raising=False)
    kill = Mock()
    monkeypatch.setattr(live.os, 'kill', kill)
    # A real Linux deployment uses a PosixPath; avoid Windows path conversion here.
    assert live.signal_owned_worker(expected, 15, '/test/hrrn_stage5_live.py') is True
    kill.assert_called_once_with(123, 15)


def test_watchdog_does_not_signal_a_process_that_has_exited(monkeypatch):
    monkeypatch.setattr(live, 'process_identity', Mock(return_value=None))
    kill = Mock()
    monkeypatch.setattr(live.os, 'kill', kill)
    assert live.signal_owned_worker(_worker(), 15, '/test/hrrn_stage5_live.py') is False
    kill.assert_not_called()


class FakeClock:
    def __init__(self):
        self.elapsed = 0
    def __call__(self):
        return self.elapsed
    def sleep(self, seconds):
        self.elapsed += seconds


def test_recovery_lock_has_finite_deadline_when_worker_never_releases(monkeypatch):
    clock = FakeClock()
    flock = Mock(side_effect=BlockingIOError('still held'))
    monkeypatch.setattr(live.fcntl, 'flock', flock)
    lock = object()
    with pytest.raises(TimeoutError):
        live.acquire_recovery_lock(lock, timeout_seconds=3, clock=clock, sleeper=clock.sleep)
    assert clock.elapsed == 3
    assert flock.call_count == 4
    assert all(call.args == (lock, live.fcntl.LOCK_EX | live.fcntl.LOCK_NB)
               for call in flock.call_args_list)


def test_recovery_lock_retries_then_succeeds_without_blocking_flock(monkeypatch):
    clock = FakeClock()
    flock = Mock(side_effect=[BlockingIOError(), BlockingIOError(), None])
    monkeypatch.setattr(live.fcntl, 'flock', flock)
    live.acquire_recovery_lock(object(), timeout_seconds=3, clock=clock, sleeper=clock.sleep)
    assert clock.elapsed == 2
    assert flock.call_count == 3


def test_guard_rejects_experiment_scheduler_restart(run):
    scheduler = make_pod(uid='scheduler', gpu=False, phase='Running')
    scheduler['status']['containerStatuses'] = [{'name': 'scheduler', 'restartCount': 1}]
    run.state['scheduler_pod_uid'] = 'scheduler'
    run.get.side_effect = None
    run.get.return_value = {'items': [scheduler]}
    with pytest.raises(RuntimeError):
        run.guard()


def test_guard_rejects_controlled_holder_ending_before_release(run):
    holder = make_pod(phase='Succeeded')
    run.state['created'] = [live.pod_ref(holder)]
    run.state['active_holder_uids'] = [holder['metadata']['uid']]
    run.get.side_effect = None
    run.get.return_value = {'items': [holder]}
    with pytest.raises(RuntimeError):
        run.guard()


@pytest.fixture
def decisive_evidence(run):
    """Source-grounded log fixtures; this does not execute a Go scheduler."""
    import hrrn_stage5_evidence as evidence

    pod = make_pod(phase='Running', bound=True, annotations={
        'scheduling.k8s.io/group-name': 'short',
        'volcano.sh/vgpu-ids-new': 'GPU-final,NVIDIA,1024,100:'})
    ns = live.TEST_NS
    def snapshot(job, session, waiting, estimated):
        ready_at = '2026-10-27T02:59:58Z' if job == 'long' else '2026-10-27T03:00:00Z'
        return ('"HRRN session snapshot" '
                f'session="{session}" job="{ns}/{job}" ready=true '
                f'readyAt="{ready_at}" waitingSeconds={waiting} '
                f'estimatedSeconds={estimated} score={1 + waiting / estimated} '
                'reason="complete_observed;podgroup_annotation" completeJob=false')
    def native(request, device):
        return ('"rf-job UUID allocation" route="rf-job" '
                f'pod="{ns}/candidate" podUID="own-pod" node="t3dgq" '
                f'requestID="{request}" devices="{device},NVIDIA,1024,100:" '
                'score=26.6 fallback="" logicalBefore=[] logicalAfter=[]')
    log = '\n'.join([
        snapshot('short', 'rolled-back', 15, 10),
        native('attempt-old', 'GPU-speculative'),
        f'"rf-job logical release" pod="{ns}/candidate" podUID="own-pod" node="t3dgq" logicalBefore=[] logicalAfter=[]',
        snapshot('long', 'actual-allocation', 22, 100),
        snapshot('short', 'actual-allocation', 20, 10),
        f'Priority JobOrderFn: <{ns}/long> priority: 0, <{ns}/short> priority: 0',
        f'Gang JobOrderFn: <{ns}/long> is ready: false, <{ns}/short> is ready: false',
        f'DRF JobOrderFn: <{ns}/long> share state: 0, <{ns}/short> share state: 0',
        f'Try to allocate resource to 1 tasks of Job <{ns}/short>',
        native('attempt-final', 'GPU-final'),
        snapshot('short', 'later', 0, 10),
        f'Gang JobOrderFn: <{ns}/long> is ready: false, <{ns}/short> is ready: true',
    ])
    advice = {'event': 'rf_advice', 'request_id': 'attempt-final',
              'model_version': evidence.MODEL, 'rf_prediction_enabled': True,
              'model_ready': True, 'status': 'ranked',
              'pod': {'uid': 'own-pod', 'namespace': ns, 'name': 'candidate'},
              'assignments': [{'container': 'sleep', 'unit': 0, 'gpu_uuid': 'GPU-final',
                               'node': 't3dgq', 'memory_mib': 1024,
                               'compute_quota': 100, 'instances': 1}]}
    cp = {'entries': [{'PodUID': 'own-pod', 'ContainerName': 'sleep',
                       'ResourceName': 'volcano.sh/vgpu-number', 'DeviceIDs': ['virtual-17'],
                       'allocate_envs': {'NVIDIA_VISIBLE_DEVICES': 'GPU-final'}}]}
    run.logs = Mock(return_value={'scheduler': log, 'rf-model': json.dumps(advice)})
    run.checkpoint = Mock(return_value=cp)
    proof = run.proof(pod, 'mocked-first-bind')
    allocation = proof['selected_native_allocation']
    comparison = evidence.comparison_evidence(
        log, ns + '/long', ns + '/short', session=allocation['session'],
        before_line=allocation['line_number'])
    return dict(proof=proof, comparison=comparison, pod=pod, long=ns + '/long', short=ns + '/short')


def test_runner_proof_integrates_final_receipt_and_comparator_scope(run, decisive_evidence):
    proof, comparison = decisive_evidence['proof'], decisive_evidence['comparison']
    assert proof['status'] == 'confirmed'
    assert proof['selected_request_id'] == 'attempt-final'
    assert proof['native_allocation_is_bind_commit'] is False
    assert proof['rollback_release_count_before_final_allocation'] == 1
    assert run.state['verified_pod_uids'] == ['own-pod']
    assert (run.out / 'proofs' / 'mocked-first-bind.json').exists()
    allocation = proof['selected_native_allocation']
    assert allocation['session'] == 'actual-allocation'
    assert proof['selected_job_selection']['job'] == decisive_evidence['short']
    assert proof['hrrn_snapshot_at_allocation']['waiting_seconds'] == 20
    assert comparison['hrrn_result'] == 1
    assert comparison['all_three_preceding_plugins_have_tie_evidence']
    assert not comparison['any_preceding_non_tie']
    assert comparison['hrrn_direct_comparator_log_available'] is False


def validate_decisive(value, **kwargs):
    return live.validate_decisive_hrrn(value['comparison'], value['proof'], value['pod'],
                                     value['long'], value['short'], **kwargs)


def test_decisive_hrrn_uses_actual_consumed_session_and_explicit_clock_inference(decisive_evidence):
    result = validate_decisive(decisive_evidence)
    assert result['validated'] is True
    assert result['session'] == 'actual-allocation'
    assert result['native_request_id'] == 'attempt-final'
    assert result['consumed_pod_uid'] == 'own-pod'
    assert result['consumed_uuids'] == ['GPU-final']
    assert result['reconstructed_session_skew_ns'] == '0'
    assert result['session_time_source'] == 'derived_ready_at_plus_waiting_seconds_not_direct_p_now_log'
    assert result['hrrn_comparator_source'].endswith('no_direct_HRRN_compare_log')


@pytest.mark.parametrize('change', [
    'comparison_prior_session', 'snapshot_prior_session', 'missing_snapshot', 'not_ready',
    'zero_wait', 'insufficient_wait', 'nan_wait', 'estimate_wrong', 'fallback_estimate',
    'wrong_formula', 'missing_ready_at', 'clock_mismatch', 'snapshot_after_selection',
    'actual_long_pod', 'not_running', 'missing_uid', 'wrong_consumed_uuid', 'request_mismatch',
    'ambiguous_final_allocation', 'rf_uuid_mismatch', 'selection_wrong_job',
    'allocation_snapshot_old', 'short_score_not_higher', 'comparison_wrong_direction',
    'prior_plugin_non_tie',
])
def test_decisive_hrrn_fails_closed_on_incomplete_or_mismatched_evidence(decisive_evidence, change):
    value = copy.deepcopy(decisive_evidence)
    proof, comparison, pod = value['proof'], value['comparison'], value['pod']
    short = comparison['snapshots'][value['short']]
    long = comparison['snapshots'][value['long']]
    if change == 'comparison_prior_session':
        comparison['session'] = 'rolled-back'
    elif change == 'snapshot_prior_session':
        short['session'] = 'rolled-back'
    elif change == 'missing_snapshot':
        del comparison['snapshots'][value['long']]
    elif change == 'not_ready':
        short['ready'] = False
    elif change in ('zero_wait', 'insufficient_wait', 'nan_wait'):
        short['waiting_seconds'] = {'zero_wait': 0, 'insufficient_wait': 14.9999, 'nan_wait': float('nan')}[change]
    elif change == 'estimate_wrong':
        long['estimated_seconds'] = 60
    elif change == 'fallback_estimate':
        short['reason'] = 'complete_observed;estimate_missing_default'
    elif change == 'wrong_formula':
        short['score'] = 100
    elif change == 'missing_ready_at':
        short['ready_at'] = None
    elif change == 'clock_mismatch':
        short['ready_at'] = '2026-10-27T03:00:00.000002Z'
    elif change == 'snapshot_after_selection':
        short['line_number'] = proof['selected_job_selection']['line_number'] + 1
    elif change == 'actual_long_pod':
        pod['metadata']['annotations']['scheduling.k8s.io/group-name'] = 'long'
    elif change == 'not_running':
        pod['status']['phase'] = 'Succeeded'
    elif change == 'missing_uid':
        del pod['metadata']['uid']
    elif change == 'wrong_consumed_uuid':
        proof['device_plugin_consumed_uuids'] = ['GPU-other']
    elif change == 'request_mismatch':
        proof['selected_request_id'] = 'attempt-old'
    elif change == 'ambiguous_final_allocation':
        proof['final_segment_receipts'].append(copy.deepcopy(proof['selected_native_allocation']))
    elif change == 'rf_uuid_mismatch':
        proof['rf_proofs'][0]['recommended_uuids'] = ['GPU-other']
    elif change == 'selection_wrong_job':
        proof['selected_job_selection']['job'] = value['long']
    elif change == 'allocation_snapshot_old':
        proof['hrrn_snapshot_at_allocation']['session'] = 'rolled-back'
    elif change == 'short_score_not_higher':
        long.update(waiting_seconds=220, score=3.2, ready_at='2026-10-27T02:56:40Z')
        comparison['hrrn_result'] = -1
    elif change == 'comparison_wrong_direction':
        comparison['hrrn_result'] = -1
    else:
        comparison['any_preceding_non_tie'] = True
    with pytest.raises(RuntimeError, match='Decisive HRRN evidence invalid:'):
        validate_decisive(value)


def test_decisive_clock_allows_only_declared_sub_microsecond_serialization_tolerance(decisive_evidence):
    value = copy.deepcopy(decisive_evidence)
    short = value['comparison']['snapshots'][value['short']]
    short['ready_at'] = '2026-10-27T03:00:00.000000500Z'
    value['proof']['hrrn_snapshot_at_allocation'] = copy.deepcopy(short)
    result = validate_decisive(value)
    assert result['reconstructed_session_skew_ns'] == '500'
    assert result['reconstructed_session_tolerance_ns'] == 1000


def test_decisive_wait_threshold_is_frozen_at_least_fifteen_seconds(decisive_evidence):
    with pytest.raises(RuntimeError, match='threshold must be at least 15'):
        validate_decisive(decisive_evidence, minimum_wait_seconds=14)
    value = copy.deepcopy(decisive_evidence)
    value['comparison']['snapshots'][value['long']].update(waiting_seconds=17, score=1.17)
    short = value['comparison']['snapshots'][value['short']]
    short.update(waiting_seconds=15, score=2.5)
    value['proof']['hrrn_snapshot_at_allocation'] = copy.deepcopy(short)
    assert validate_decisive(value)['validated'] is True
