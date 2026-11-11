"""Offline safeguards for staged real-cluster inputs, with no Kubernetes calls."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('stage5_manifests', ROOT / 'scripts/hrrn_stage5_manifests.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


def plugin(config, name):
    return next(p for tier in config['tiers'] for p in tier['plugins'] if p['name'] == name)


def test_all_modes_preserve_stage4_frozen_placement_and_fairness():
    for mode, filename in [('A', 'A-native-rf.yaml'), ('B', 'B-hrrn-rf.yaml'), ('C', 'C-hrrn-complete-rf.yaml')]:
        old = yaml.safe_load((ROOT / 'k8s/hrrn-stage4/configs' / filename).read_text())
        d = plugin(old, 'deviceshare')['arguments']
        d['deviceshare.KnownGeometriesCMName'] = m.DEVICE_CONFIG
        d['deviceshare.KnownGeometriesCMNamespace'] = m.SYSTEM_NS
        assert m.configs()[mode] == old
        assert plugin(m.configs()[mode], 'deviceshare')['arguments']['deviceshare.NodeLockEnable'] is True


def test_only_two_hrrn_flags_differ_between_modes():
    normalized = []
    observed = []
    for config in m.configs().values():
        args = plugin(config, 'hrrn')['arguments']
        observed.append((args.pop('enabled'), args.pop('complete-job')))
        normalized.append(config)
    assert observed == [(False, False), (True, False), (True, True)]
    assert normalized[0] == normalized[1] == normalized[2]


def test_pending_requests_determine_quota_not_only_running_capacity():
    p = m.protocol('unit-001')
    cases = {c['id']: c for c in p['cases']}
    assert cases['order-a']['admission_peak'] == {
        'pods': 6, 'requests.volcano.sh/vgpu-number': 6,
        'requests.volcano.sh/vgpu-memory': 24576, 'requests.volcano.sh/vgpu-cores': 600,
    }
    assert cases['complete-c']['admission_peak'] == {
        'pods': 8, 'requests.volcano.sh/vgpu-number': 8,
        'requests.volcano.sh/vgpu-memory': 32768, 'requests.volcano.sh/vgpu-cores': 540,
    }
    for key, value in p['peak_quota'].items():
        assert int(value) == max(c['admission_peak'][key] for c in p['cases'])


def test_complete_case_is_structurally_feasible_but_temporarily_blocked():
    jobs = m.scenario_jobs('unit-001', 'complete-c')
    cores = lambda role: [int(p['spec']['containers'][0]['resources']['requests']['volcano.sh/vgpu-cores'])
                          for p in jobs[role] if p['kind'] == 'Pod']
    assert cores('large') == [40, 40]
    assert sum(cores('large')) <= 100
    assert sum(cores('large') + cores('background')) > 100
    assert sum(cores('small') + cores('background')) <= 100
    assert sum(cores('background') + cores('hold')) == 100
    assert jobs['large'][0]['spec']['minMember'] == 1
    assert jobs['large'][0]['metadata']['annotations'][m.TOTAL_PODS] == '2'


def test_lock_incompatibility_blocks_c_without_silently_disabling_lock():
    p = m.protocol('unit-001')
    blocked = [c for c in p['cases'] if not c['execution_enabled']]
    assert [c['id'] for c in blocked] == ['complete-a', 'complete-c']
    assert all(c['requires_compatibility_proof'] == 'full-job-node-lock' for c in blocked)
    assert 'native partial case is prepared but not executed' in blocked[0]['blocked_reason']
    for config in m.configs().values():
        assert plugin(config, 'deviceshare')['arguments']['deviceshare.NodeLockEnable'] is True


def test_first_live_scope_is_only_the_two_ordering_cases():
    p = m.protocol('unit-001')
    assert p['live_scope'] == ['order-a', 'order-b']
    assert [c['id'] for c in p['cases'] if c['execution_enabled']] == p['live_scope']
    assert all(c['kind'] == 'order' for c in p['cases'] if c['execution_enabled'])
    assert p['propagation']['execution_enabled'] is False


def test_order_comparison_uses_identical_inputs_and_real_readiness():
    a, b = m.scenario_jobs('unit-001', 'order-a'), m.scenario_jobs('unit-001', 'order-b')
    for role in a:
        assert a[role][0]['spec'] == b[role][0]['spec']
        assert a[role][0]['metadata']['annotations'] == b[role][0]['metadata']['annotations']
        assert a[role][1]['spec'] == b[role][1]['spec']
        assert 'minResources' not in a[role][0]['spec']
        assert 'schedulingGates' not in a[role][1]['spec']
        assert not any('ready' in k.lower() for o in a[role] for k in o['metadata'].get('annotations', {}))
    assert a['long'][0]['metadata']['annotations'][m.ESTIMATE] == '100'
    assert a['short'][0]['metadata']['annotations'][m.ESTIMATE] == '10'
    assert a['long'][1]['spec'] == a['short'][1]['spec']


def test_run_identity_and_full_membership_are_unambiguous():
    all_names = []
    for case in m.protocol('unit-001')['cases']:
        for role, docs in case['jobs'].items():
            group, *pods = docs
            assert int(group['metadata']['annotations'][m.TOTAL_PODS]) == len(pods)
            for pod in pods:
                assert pod['metadata']['annotations']['scheduling.k8s.io/group-name'] == group['metadata']['name']
                assert pod['metadata']['labels'][m.RUN_LABEL] == 'unit-001'
                assert pod['metadata']['labels']['gpu-lab/role'] == role
                assert pod['metadata']['namespace'] == m.TEST_NS
                assert 'uid' not in pod['metadata']
                all_names.append(pod['metadata']['name'])
    assert len(all_names) == len(set(all_names))


def test_scheduler_default_off_and_frozen_digests():
    obj = m.scheduler_deployment('unit-001')
    assert obj['metadata']['name'] != 'volcano-scheduler'
    assert obj['spec']['replicas'] == 0
    spec = obj['spec']['template']['spec']
    assert spec['serviceAccountName'] == 'volcano-rf-test'
    assert spec['nodeSelector'] == {'kubernetes.io/hostname': 't3dgq'}
    containers = {c['name']: c for c in spec['containers']}
    assert containers['scheduler']['image'] == m.SCHEDULER_IMAGE
    assert containers['rf-model']['image'] == m.RF_IMAGE
    assert containers['scheduler']['command'] == ['/vc-scheduler-hrrn']
    assert '--scheduler-name=volcano-hrrn-stage5' in containers['scheduler']['args']
    assert next(v['configMap']['name'] for v in spec['volumes'] if v['name'] == 'config') == m.configmap_name('unit-001', 'A')


def test_metadata_propagation_is_real_volcano_job_but_gpu_free():
    job = m.propagation_job('unit-001')
    assert job['apiVersion'] == 'batch.volcano.sh/v1alpha1'
    assert job['metadata']['annotations'] == {m.ESTIMATE: '37', m.TOTAL_PODS: '1'}
    spec = job['spec']['tasks'][0]['template']['spec']
    assert 'runtimeClassName' not in spec
    assert spec['containers'][0]['command'] == ['/bin/sleep', '5']
    assert not any('gpu' in k for k in spec['containers'][0]['resources']['requests'])


@pytest.mark.parametrize('run_id', ['../escape', 'UPPER', 'bad_name', '', '-prefix', 'suffix-', 'a' * 21])
def test_invalid_run_identity_refused(run_id):
    with pytest.raises(ValueError):
        m.protocol(run_id)


@pytest.mark.parametrize('change', [
    lambda p: p['spec'].update(nodeName='t3dgq'),
    lambda p: p['spec'].update(schedulerName='volcano'),
    lambda p: p['spec'].update(schedulingGates=[{'name': 'gpu-lab/ready'}]),
    lambda p: p['spec'].update(hostPID=True),
    lambda p: p['spec'].update(volumes=[{'name': 'host', 'hostPath': {'path': '/'}}]),
    lambda p: p['metadata'].update(namespace='default'),
    lambda p: p['metadata']['annotations'].update({'volcano.sh/vgpu-ids-new': 'FORGED'}),
    lambda p: p['spec']['containers'][0].update(command=['/bin/sh', '-c', 'sleep 30']),
    lambda p: p['spec']['containers'][0].update(image='pytorch/pytorch:latest'),
    lambda p: p['spec']['containers'][0].update(env=[{'name': 'NVIDIA_VISIBLE_DEVICES', 'value': 'all'}]),
    lambda p: p['spec']['containers'][0]['securityContext'].update(privileged=True),
    lambda p: p['spec']['containers'][0]['resources']['limits'].update({'volcano.sh/vgpu-cores': '99'}),
])
def test_dangerous_or_invalid_workload_mutation_refused(change):
    pod = m.standalone_job('unit-001', 'order-a', 'long', 100, 100, 30)[1]
    change(pod)
    with pytest.raises(ValueError):
        m.validate_manifest(pod)


def test_finite_service_and_membership_inputs():
    for kwargs in [dict(sleep_seconds=0), dict(sleep_seconds=301), dict(cores=101), dict(estimate=True), dict(pods=0), dict(pods=2, min_available=3)]:
        values = dict(run_id='unit-001', case='order-a', name='long', cores=100, estimate=100, sleep_seconds=30)
        values.update(kwargs)
        with pytest.raises(ValueError):
            m.standalone_job(**values)


def test_render_preserves_existing_attempt_and_round_trips_json(tmp_path):
    folder = tmp_path / 'rendered'
    plan = m.render('unit-001', folder)
    saved = json.loads((folder / 'protocol.json').read_text())
    assert saved == plan
    cms = [obj for obj in saved['setup_objects'] if obj['kind'] == 'ConfigMap']
    assert [yaml.safe_load(c['data']['volcano-scheduler.conf']) for c in cms] == list(m.configs().values())
    with pytest.raises(FileExistsError):
        m.render('unit-001', folder)
