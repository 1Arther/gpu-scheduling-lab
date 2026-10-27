"""Logical-window/canary preparation tests; fixtures are NOT live Pod results."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import pytest
import yaml

ROOT=Path(__file__).resolve().parents[1]


def module(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/f'{name}.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


def pod(phase='Running'):
    return {'metadata':{'name':'fixture','namespace':'default','uid':'test','annotations':{}},
      'status':{'phase':phase},'spec':{'containers':[{'name':'work','resources':{'limits':{'volcano.sh/vgpu-number':'1'}}}]}}


def test_window_detects_both_resource_names_and_requests():
    m=module('round7_window_preflight')
    for key in ['volcano.sh/vgpu-number','volcano.sh/vgpu-memory','nvidia.com/gpu']:
        for typ in ['limits','requests']:
            p=pod();p['spec']['containers'][0]['resources']={typ:{key:'1'}}
            assert m.gpu_demand(p)
            assert m.blockers([p],'t3dgq')['active_gpu_or_DRA_demand']


def test_terminal_annotation_not_current_logical_demand():
    m=module('round7_window_preflight');p=pod('Succeeded')
    p['metadata']['annotations']={'volcano.sh/vgpu-node':'t3dgq','volcano.sh/vgpu-time':str(m.MAX_TIME),'volcano.sh/devices-to-allocate':''}
    b=m.blockers([p],'t3dgq')
    assert not b['active_gpu_or_DRA_demand'] and not b['finite_plugin_candidates']
    assert len(b['historical_terminal_annotations'])==1


def test_unconsumed_old_timestamp_requires_inspection_even_if_terminal():
    m=module('round7_window_preflight');p=pod('Succeeded')
    p['metadata']['annotations']={'volcano.sh/vgpu-node':'t3dgq','volcano.sh/vgpu-time':'1234',
      'volcano.sh/devices-to-allocate':'GPU-fixture,NVIDIA,4096,20:'}
    assert m.blockers([p],'t3dgq')['finite_plugin_candidates']


def test_dra_and_init_container_demand_are_not_missed():
    m=module('round7_window_preflight');p=pod();p['spec']['initContainers']=p['spec'].pop('containers');p['spec']['containers']=[]
    assert m.gpu_demand(p)
    p['spec']['initContainers']=[];p['spec']['resourceClaims']=[{'name':'claim'}]
    assert m.gpu_demand(p)


def test_guard_removal_requires_scheduler_and_test_pods_stopped():
    m=module('round7_window_rollback')
    assert not m.may_remove_guard([pod()],None,[])
    assert not m.may_remove_guard([],{'spec':{'replicas':1}},[])
    assert not m.may_remove_guard([],{'spec':{'replicas':0},'status':{'replicas':1}},[])
    assert not m.may_remove_guard([],None,[pod()])
    assert m.may_remove_guard([],None,[])
    command,body=m.operation({'kind':'ValidatingAdmissionPolicyBinding','name':'rf-v7-gpu-window','uid':'actual-created-uid'})
    assert command[2].endswith('/validatingadmissionpolicybindings/rf-v7-gpu-window')
    assert body['preconditions']['uid']=='actual-created-uid'


def test_window_drafts_fail_closed_and_sleep_only():
    folder=ROOT/'k8s/round7-logical-window'
    gate=json.loads((folder/'execution-gate.json').read_text())
    assert not gate['deployment_allowed'] and gate['physical_occupancy_alone_is_not_a_blocker']
    dep=yaml.safe_load((folder/'04-scheduler-DRAFT.yaml').read_text())
    assert dep['spec']['replicas']==0
    assert '--node-selector=kubernetes.io/hostname:t3dgq' in dep['spec']['template']['spec']['containers'][0]['args']
    for path in folder.glob('test-*.yaml'):
        p=next(o for o in yaml.safe_load_all(path.read_text()) if o['kind']=='Pod')
        assert p['spec']['schedulerName']=='volcano-rf-test'
        assert p['spec']['containers'][0]['command']==['/bin/sleep','600']
        assert p['spec']['containers'][0]['resources']['limits']['volcano.sh/vgpu-memory']=='4096'
    policy=yaml.safe_load((folder/'05-window-policy-DRAFT.yaml').read_text())
    binding=yaml.safe_load((folder/'06-window-binding-DRAFT.yaml').read_text())
    assert policy['spec']['failurePolicy']=='Fail'
    assert policy['spec']['matchConstraints']['resourceRules'][0]['operations']==['CREATE']
    assert 'namespaceSelector' not in binding['spec'].get('matchResources',{}) # old schedulers anywhere must be covered
    assert binding['spec']['validationActions']==['Deny']


def test_canary_config_retains_original_dqn_plugins():
    folder=ROOT/'k8s/round7-logical-window'
    cm=yaml.safe_load((folder/'A-canary-config-DRAFT.yaml').read_text())
    config=yaml.safe_load(cm['data']['volcano-scheduler.conf'])
    args=next(p['arguments'] for t in config['tiers'] for p in t['plugins'] if p['name']=='deviceshare')
    assert args['deviceshare.GPUSelectPolicy']=='dqn-job'
    assert args['deviceshare.RFCanaryEnable']
    assert args['deviceshare.DQNGRPCEndpoint']=='dqn-scheduler-grpc.volcano-system.svc.cluster.local:50051'
    assert 'deviceshare.ScheduleWeight' not in args
    assert [p['name'] for t in config['tiers'] for p in t['plugins']]==['priority','gang','conformance','overcommit','drf','deviceshare','predicates','proportion','nodeorder','binpack']
    patch=yaml.safe_load((folder/'A-scheduler-patch-DRAFT.yaml').read_text())
    mounts=patch['spec']['template']['spec']['containers'][0]['volumeMounts']
    assert next(m for m in mounts if m.get('$patch')=='delete')['mountPath']=='/vc-scheduler' # strategic merge key is mountPath


def test_actual_uid_export_preserves_native_and_guard_kinds():
    m=module('round7_record_ledger');p=pod()
    p['kind']='Pod';p['metadata']['labels']={'gpu-lab/experiment':'round7-rf'}
    assert m.records([p])[0]['uid']==p['metadata']['uid']
    assert m.records([p])[0]['kind']=='pod'
    p['kind']='ValidatingAdmissionPolicyBinding'
    assert m.records([p],guard=True)[0]['kind']=='ValidatingAdmissionPolicyBinding'
    p['metadata'].pop('uid')
    with pytest.raises(ValueError):m.records([p])


def test_optional_three_node_scope_excludes_A40_and_does_not_start():
    f=ROOT/'k8s/round7-logical-window/expand-three-nodes'
    dep=yaml.safe_load((f/'04-scheduler-DRAFT.yaml').read_text())
    assert dep['spec']['replicas']==0
    flags=[s for s in dep['spec']['template']['spec']['containers'][0]['args'] if s.startswith('--node-selector=')]
    assert flags==['--node-selector=kubernetes.io/hostname:'+n for n in ['t3dgq','t3dmg','t3dgq-2']]
    role=yaml.safe_load((f/'01-node-rbac-DRAFT.yaml').read_text())
    assert next(r['resourceNames'] for r in role['rules'] if r.get('resourceNames'))==['t3dgq','t3dmg','t3dgq-2']
