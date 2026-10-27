"""Stage-1 safety/evidence tests. Fixtures are synthetic, not cluster results."""
from copy import deepcopy
import base64
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import threading
from urllib.request import Request,urlopen

import pytest
import yaml

from gpu_lab.profiles import ROOT
from gpu_lab.runtime.audit_server import audit_record
from gpu_lab.runtime.http_server import make_server
from gpu_lab.runtime.service import SuitabilityService
from gpu_lab.staging import allocations,inventory,fragmentation,uuid_consistency,physical_rows
from test_round6 import payload


def module(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/f'{name}.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


@pytest.fixture(scope='module')
def service():return SuitabilityService()


def fixture_pod(phase='Running',deleting=False):
    return {'metadata':{'name':'fixture','namespace':'gpu-rf-tests','uid':'fixture','labels':{'gpu-lab/workload-type':'training'},
                       'deletionTimestamp':'now' if deleting else None,'annotations':{'volcano.sh/vgpu-ids-new':'GPU-fixture,NVIDIA,4096,20:',
                       'volcano.sh/devices-to-allocate':''}},'spec':{'nodeName':'fixture-node','containers':[{'resources':{'limits':{'volcano.sh/vgpu-number':1}}}]},'status':{'phase':phase}}


def fixture_node():
    return {'metadata':{'name':'fixture-node','annotations':{'volcano.sh/node-vgpu-register':'GPU-fixture,10,24564,NVIDIA-NVIDIA GeForce RTX 4090,true,hami-core:'}},
            'status':{'conditions':[{'type':'Ready','status':'True'}]}}


@pytest.mark.parametrize('phase,count',[('Running',1),('Pending',1),('Succeeded',0),('Failed',0)])
def test_reconstruction_excludes_terminal_not_empty_transient(phase,count):
    g,w=inventory([fixture_node()],[fixture_pod(phase)])
    assert g[0]['allocated_vgpu_count']==count and g[0]['allocated_memory_mib']==4096*count
    assert g[0]['training_pod_count']==count and not w
    assert g[0]['source'].startswith('reconstructed')


@pytest.mark.parametrize('field',['limits','requests'])
def test_terminating_pod_remains_reserved_and_unknown_ordinary_gpu_is_not_invented(field):
    p=fixture_pod(deleting=True);g,_=inventory([fixture_node()],[p]);assert g[0]['allocated_memory_mib']==4096
    p['metadata']['annotations']={};p['spec']['containers'][0]['resources']={field:{'nvidia.com/gpu':1}}
    g,w=inventory([fixture_node()],[p]);assert w and not g[0]['pods']


def test_bad_allocation_and_unknown_uuid_are_explicit():
    with pytest.raises(ValueError):allocations('broken')
    with pytest.raises(ValueError):allocations('GPU-x,NVIDIA,4096,101:')
    p=fixture_pod();p['metadata']['annotations']['volcano.sh/vgpu-ids-new']='GPU-other,NVIDIA,100,20:'
    _,w=inventory([fixture_node()],[p]);assert w[0]['reason']=='annotation UUID not registered'


def test_running_or_consumed_annotation_alone_does_not_prove_three_way_match():
    annotation='GPU-fixture,NVIDIA,4096,20:'
    assert uuid_consistency(['GPU-fixture'],annotation,None,transient_annotation='')['status']=='insufficient_evidence'
    assert uuid_consistency(['GPU-fixture'],annotation,['GPU-fixture'],consumed_source='Pod_Running')['status']=='insufficient_evidence'
    assert uuid_consistency(['GPU-fixture'],annotation,['GPU-other'],consumed_source='cri_runtime_NVIDIA_VISIBLE_DEVICES')['status']=='mismatch'
    assert uuid_consistency(['GPU-fixture'],annotation,['GPU-fixture'],consumed_source='kubelet_checkpoint_allocate_response')['status']=='confirmed'


def test_fragmentation_zero_denominator_and_stranded_quota():
    assert fragmentation([])['fragmentation_ratio'] is None
    g=[{'free_memory_mib':8192,'free_compute_quota':0,'free_instances':2},
       {'free_memory_mib':0,'free_compute_quota':40,'free_instances':2}]
    r=fragmentation(g);assert r['fragmentation_ratio']==1 and r['stranded_memory_mib']==8192 and r['stranded_compute_quota']==40


def test_physical_query_eight_fields_is_independent_from_logical_memory():
    p=physical_rows('0, GPU-fixture, NVIDIA fixture, 24564, 6695, 17516, 100, 550.163.01\n')[0]
    assert p['physical_free_mib']==17516 and p['physical_utilization_pct']==100
    assert 'free_memory_mib' not in p
    with pytest.raises(ValueError):physical_rows('unsupported-format')


def test_checkpoint_env_decoder_does_not_use_virtual_ids_as_physical_uuid():
    collect=module('round7_collect')
    def field(n,b):
        assert len(b)<128;return bytes([n*8+2,len(b)])+b
    raw=field(1,field(1,b'NVIDIA_VISIBLE_DEVICES')+field(2,b'GPU-fixture'))
    cp=collect.checkpoint({'Data':{'PodDeviceEntries':[{'PodUID':'fixture','ResourceName':'volcano.sh/vgpu-number',
        'DeviceIDs':{'0':['unrelated-virtual-ID']},'AllocResp':base64.b64encode(raw).decode()}],'RegisteredDevices':{'volcano.sh/vgpu-memory':[str(i) for i in range(100)]}}})
    assert cp['entries'][0]['allocate_envs']['NVIDIA_VISIBLE_DEVICES']=='GPU-fixture'
    assert cp['registered_devices']['volcano.sh/vgpu-memory']['count']==100
    assert len(cp['registered_devices']['volcano.sh/vgpu-memory']['sample'])==8
    assert collect.checkpoint({'Data':{'PodDeviceEntries':None}})['entries']==[]


def test_draft_manifests_fail_closed_and_never_start_workloads(tmp_path):
    prep=module('round7_prepare');prep.generate(tmp_path)
    gate=json.loads((tmp_path/'isolation-gate.json').read_text())
    assert not gate['deployment_allowed'] and not gate['explicit_phase2_user_approval']
    deployment=list(yaml.safe_load_all((tmp_path/'04-scheduler-DRAFT.yaml').read_text()))[0]
    assert deployment['spec']['replicas']==0 and deployment['spec']['strategy']['type']=='Recreate'
    ps=deployment['spec']['template']['spec'];assert not ps['automountServiceAccountToken']
    rf=next(c for c in ps['containers'] if c['name']=='rf-model')
    assert all(v['name']!='api-token' for v in rf['volumeMounts'])
    assert 'hostNetwork' not in ps and not any('hostPath' in v for v in ps['volumes'])
    for f in tmp_path.glob('test-*.yaml'):
        pg,pod=list(yaml.safe_load_all(f.read_text()));s=pod['spec'];c=s['containers'][0]
        assert s['schedulerName']=='volcano-rf-test' and c['command']==['/bin/sleep','600']
        assert c['imagePullPolicy']=='Never' and '@sha256:' in c['image']
        assert 'nvidia.com/gpu' not in c['resources']['limits']
        assert c['resources']['limits']['volcano.sh/vgpu-memory']==4096
        assert pod['metadata']['annotations']['scheduling.k8s.io/group-name']==pg['metadata']['name']
    maps=list(yaml.safe_load_all((tmp_path/'03-configmaps.yaml').read_text()))
    on,off=(yaml.safe_load(m['data']['volcano-scheduler.conf']) for m in maps[:2])
    aa=on['tiers'][1]['plugins'][0]['arguments'];bb=off['tiers'][1]['plugins'][0]['arguments']
    assert aa['deviceshare.RFPredictionEnabled'] is True and bb['deviceshare.RFPredictionEnabled'] is False
    aa.pop('deviceshare.RFPredictionEnabled');bb.pop('deviceshare.RFPredictionEnabled');assert aa==bb
    assert on['actions']=='enqueue, allocate' and all(p['name'] not in ('proportion','reclaim','preempt') for tier in on['tiers'] for p in tier['plugins'])


def test_rollback_refuses_foreign_objects_and_requires_actual_uid():
    rollback=module('round7_rollback')
    for row in ({'kind':'node','name':'t3dgq','uid':'a'},{'kind':'deployment','namespace':'volcano-system','name':'volcano-scheduler','uid':'a'},
                {'kind':'queue','name':'root','uid':'a'},{'kind':'pod','name':'rf-v7-a','namespace':'gpu-rf-tests'}):
        with pytest.raises(ValueError):rollback.validate(row)
    assert 'delete' in rollback.validate({'kind':'pod','name':'rf-v7-a','namespace':'gpu-rf-tests','uid':'fixture'})


def test_rollback_is_server_uid_conditional_and_path_safe():
    r=module('round7_rollback');row={'kind':'pod','name':'rf-v7-a','namespace':'gpu-rf-tests','uid':'fixture'}
    assert r.delete_options(row)['preconditions']=={'uid':'fixture'}
    assert r.validate(row)==['kubectl','delete','--raw=/api/v1/namespaces/gpu-rf-tests/pods/rf-v7-a','-f','-']
    with pytest.raises(ValueError):r.validate({**row,'name':'rf-v7-../../nodes'})


def test_audit_records_real_response_and_same_snapshot(service):
    from gpu_lab.runtime.volcano import VolcanoRFAdapter
    p=payload(service);r=VolcanoRFAdapter(service).rank(p);a=audit_record(p,r)
    assert a['model_version']==service.version and a['assignments']==r['assignments']
    assert a['gpus']==p['gpus'] and a['pod']['uid']==p['pod']['metadata']['uid']
    assert all('score_decomposition' in c['request_specific_scores']['training'] for c in a['classifications'])


def test_http_observer_reuses_model_and_does_not_change_advice(service):
    records=[];server=make_server(service,port=0,observer=lambda p,r:records.append(audit_record(p,r)))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        p=payload(service)
        with urlopen(Request('http://127.0.0.1:'+str(server.server_port)+'/v1/rank',data=json.dumps(p).encode(),headers={'Content-Type':'application/json'}),timeout=3) as r:
            data=json.load(r)
        assert len(records)==1 and records[0]['assignments']==data['assignments']
    finally:server.shutdown();server.server_close();thread.join(3)


def test_audit_server_fails_before_listen_on_bad_model(tmp_path):
    r=subprocess.run([sys.executable,'-m','gpu_lab.runtime.audit_server','--bundle',str(tmp_path/'missing')],cwd=ROOT,capture_output=True,text=True,timeout=15)
    assert r.returncode==2 and json.loads(r.stdout)['event']=='model_load_failed'
