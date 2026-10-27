"""Preparation tests only. Image hashes below are fixtures, never built images."""
from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/'scripts'/f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def clear_observation():
    return dict(no_prior_gpu_demand=True, no_legacy_consumable_annotations=True, node_lock=None)


def test_raw_api_pod_without_optional_metadata_does_not_break_inventory():
    from gpu_lab.staging import inventory
    node={'metadata':{'name':'test','annotations':{}},'status':{'conditions':[]}}
    pod={'metadata':{'name':'cpu','namespace':'default','uid':'fixture'},
         'spec':{'containers':[{'name':'cpu','resources':{}}]},'status':{'phase':'Running'}}
    assert inventory([node],[pod])==([],[])
    pod['metadata'].update(annotations=None,labels=None)
    assert inventory([node],[pod])==([],[])


@pytest.mark.parametrize('reason',['','Post "http://127.0.0.1:1/v1/rank": connection refused'])
def test_native_fallback_is_a_reason_string_not_boolean(reason):
    import json
    from gpu_lab.staging import native_commit_receipt
    fields=dict(pod='gpu-rf-tests/fixture',podUID='fixture',node='test',requestID='request',
                devices='GPU-fixture,NVIDIA,4096,20:',fallback=reason)
    line='I fixture "rf-job UUID allocation" '+' '.join(k+'='+json.dumps(v) for k,v in fields.items())
    value=native_commit_receipt(line)
    assert value['fallback_used'] is bool(reason)
    assert value['fallback']==reason
    assert value['allocations'][0]['uuid']=='GPU-fixture'
    with pytest.raises(ValueError):native_commit_receipt(line.replace('fallback='+json.dumps(reason),'fallback=true'))


def test_native_json_snapshots_parse_independently_of_rf_advice():
    from gpu_lab.staging import native_ledger_snapshots
    line='native logicalBefore=[{"uuid":"fixture","allocated_memory_mib":4096}] logicalAfter=[{"uuid":"fixture","allocated_memory_mib":0}]'
    row=native_ledger_snapshots(line)
    assert row['logicalBefore'][0]['allocated_memory_mib']==4096
    assert row['logicalAfter'][0]['allocated_memory_mib']==0
    with pytest.raises(ValueError):native_ledger_snapshots('logicalAfter={}')


def test_absent_admission_is_not_automatically_mandatory_or_a_go():
    result = load('round7_window_preflight').assessment(clear_observation() | dict(policy_present=False))
    assert result['observed_api_clear']
    assert not result['admission_policy_mandatory']
    assert not result['execution_authorized']
    assert result['private_scheduler_reservations'] == 'not_directly_observed'
    assert not result['technical_exclusion_without_admission']


def test_present_admission_does_not_approve_or_prove_quiescence():
    result = load('round7_window_preflight').assessment(clear_observation() | dict(policy_present=True, binding_present=True))
    assert not result['execution_authorized']
    assert result['private_scheduler_reservations'] == 'not_directly_observed'


@pytest.mark.parametrize('field,value', [('node_lock', 'busy'), ('error', 'query failed'),
    ('no_prior_gpu_demand', False), ('no_legacy_consumable_annotations', False)])
def test_window_blocks_bad_or_incomplete_observations(field, value):
    m = load('round7_window_preflight')
    assert not m.assessment(clear_observation() | {field: value})['observed_api_clear']
    assert not m.assessment({})['observed_api_clear']


def test_ordinary_mig_alias_also_blocks_observation():
    m = load('round7_window_preflight')
    p = dict(metadata=dict(name='fixture', uid='fixture', namespace='default'),
             spec=dict(containers=[dict(resources=dict(requests={'nvidia.com/mig-1g.10gb': '1'}))]),
             status=dict(phase='Pending'))
    assert m.blockers([p], 't3dgq')['active_gpu_or_DRA_demand']


@pytest.mark.parametrize('bad', ['gpu-lab/rf:latest', 'placeholder', 'x@sha256:'+'z'*64, '', 'x@sha256:abc'])
def test_renderer_rejects_unresolved_or_mutable_images(bad):
    with pytest.raises(ValueError):
        load('round7_render_minimal').render(bad, 'fixture/rf@sha256:'+'1'*64)


def test_default_bundle_excludes_guard_and_production_mutations():
    m = load('round7_render_minimal')
    before = {f: (m.SOURCE/f).read_bytes() for f in m.FILES}
    objects = m.render('fixture/scheduler@sha256:'+'0'*64, 'fixture/rf@sha256:'+'1'*64)
    assert all(o['kind'] not in ['ValidatingAdmissionPolicy', 'ValidatingAdmissionPolicyBinding'] for o in objects)
    assert all(o['metadata'].get('namespace') != 'volcano-system' for o in objects)
    dep = next(o for o in objects if o['kind']=='Deployment')
    assert dep['spec']['replicas'] == 0
    assert dep['spec']['template']['spec']['nodeSelector']=={'kubernetes.io/hostname': 't3dgq'}
    assert all(c['imagePullPolicy']=='Never' for c in dep['spec']['template']['spec']['containers'])
    assert before == {f: (m.SOURCE/f).read_bytes() for f in m.FILES}
