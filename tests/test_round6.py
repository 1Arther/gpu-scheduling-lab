"""Frozen-model HTTP integration tests; no cluster client or GPU workloads."""
from copy import deepcopy
import json
import threading
from urllib.request import Request,urlopen
from urllib.error import HTTPError

import pytest

from gpu_lab.runtime.http_server import make_server
from gpu_lab.runtime.service import SuitabilityService
from gpu_lab.runtime.volcano import VolcanoRFAdapter,RF_SCHEMA


@pytest.fixture(scope='module')
def service6():return SuitabilityService()


def payload(service,task='training'):
    return {'schema_version':RF_SCHEMA,'model_version':service.version,'request_id':'test-request','snapshot_token':'native-ledger-token','memory_unit_mib':1,
            'pod':{'apiVersion':'v1','kind':'Pod','metadata':{'name':'work','uid':'uid-work','labels':{'gpu-lab/workload-type':task}},
                   'spec':{'containers':[{'name':'cuda','resources':{'limits':{'volcano.sh/vgpu-number':1,'volcano.sh/vgpu-memory':4096,'volcano.sh/vgpu-cores':30}}}]}},
            'gpus':[{'uuid':'SIM-'+model,'node':'same-node','model':name,'registered_memory_mib':memory,'allocated_memory_mib':0,
                     'allocated_compute_quota':0,'allocated_vgpu_count':0,'training_vgpu_count':0,'inference_vgpu_count':0,
                     'training_pod_count':0,'inference_pod_count':0,'max_instances':10,'healthy':True,'sharing_mode':'hami-core'}
                    for model,name,memory in [('4090','NVIDIA-NVIDIA GeForce RTX 4090',24564),('5090','NVIDIA-NVIDIA GeForce RTX 5090',32607),('l40','NVIDIA-NVIDIA L40',46068)]]}


def test_frozen_model_and_static_hardware_not_replaced_by_logical_total(service6):
    adapter=VolcanoRFAdapter(service6);r=payload(service6);g=adapter.snapshot(r['gpus'][0])
    assert g['hardware']['memory_capacity_mib']==22888
    assert g['logical_memory_capacity_mib']==24564
    assert g['dynamic']['free_memory_mib']==24564
    assert service6.model_error is None
    c=service6.classify_gpu(g)
    assert c['model_feature_mapping']['model_reference_free_memory_mib']==22888
    assert c['model_feature_mapping']['logical_free_memory_mib']==24564


def test_http_adapter_returns_rf_scores_uuid_and_both_targets(service6):
    result=VolcanoRFAdapter(service6).rank(payload(service6))
    assert result['status']=='ranked' and result['model_ready']
    assert result['assignments'][0]['gpu_uuid'].startswith('SIM-')
    assert result['model_version']==service6.version
    assert all(set(r['request_specific_scores'])=={'training','inference'} for r in result['classifications'])
    assert all(r['classification']['raw_rf'] is not None for r in result['classifications'])
    assert result['logical_allocation_only'] and not result['real_cuda_success_guarantee']


def test_logical_percentage_uses_registered_total(service6):
    r=payload(service6);limits=r['pod']['spec']['containers'][0]['resources']['limits']
    limits.pop('volcano.sh/vgpu-memory');limits['volcano.sh/vgpu-memory-percentage']=50
    result=VolcanoRFAdapter(service6).rank(r)
    by_uuid={g['uuid']:g for g in r['gpus']}
    assert result['status']=='ranked'
    for a in result['assignments']:assert a['memory_mib']==by_uuid[a['gpu_uuid']]['registered_memory_mib']//2


def test_no_fabricated_unknown_hardware(service6):
    r=payload(service6);r['gpus'][0]['model']='NVIDIA-unknown-device';r['gpus']=r['gpus'][:1]
    result=VolcanoRFAdapter(service6).rank(r)
    g=result['classifications'][0]
    assert g['snapshot']['hardware']['fp32_tflops'] is None
    assert g['classification']['prediction_status']=='pending_verification'
    assert result['details']['eligible_gpus'][0]['ranking_basis']=='deterministic_conservative_proxy'


@pytest.mark.parametrize('change',[lambda r:r.update(model_version='wrong'),lambda r:r.update(memory_unit_mib=2),
    lambda r:r['gpus'][0].update(allocated_memory_mib=999999),
    lambda r:r['pod']['metadata']['labels'].update({'gpu-lab/workload-type':'auto'})])
def test_invalid_protocol_or_state_is_explicit(service6,change):
    r=payload(service6);change(r)
    assert VolcanoRFAdapter(service6).rank(r)['status']=='invalid_input'


def test_dynamic_state_changes_predictions_without_mutating_request(service6):
    adapter=VolcanoRFAdapter(service6);r=payload(service6);r['gpus']=r['gpus'][2:]
    original=deepcopy(r);before=adapter.rank(r)
    r['gpus'][0].update(allocated_memory_mib=40000,allocated_compute_quota=65,allocated_vgpu_count=2,
                       training_vgpu_count=1,inference_vgpu_count=1,training_pod_count=1,inference_pod_count=1)
    after=adapter.rank(r)
    assert before['classifications'][0]['classification']['input_sha256']!=after['classifications'][0]['classification']['input_sha256']
    restored=adapter.rank(original)
    assert before['assignments']==restored['assignments']
    assert before['classifications'][0]['classification']==restored['classifications'][0]['classification']


def test_real_http_request_response_and_single_service_instance(service6):
    server=make_server(service6,port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        base='http://127.0.0.1:'+str(server.server_port)
        health=json.load(urlopen(base+'/health',timeout=3));assert health['model_ready']
        for task in ('training','inference'):
            req=Request(base+'/v1/rank',data=json.dumps(payload(service6,task)).encode(),headers={'Content-Type':'application/json'})
            response=json.load(urlopen(req,timeout=5))
            assert response['status']=='ranked' and response['model_version']==service6.version
        with pytest.raises(HTTPError) as caught:urlopen(Request(base+'/v1/rank',data=b'{bad'),timeout=3)
        assert caught.value.code==400
    finally:server.shutdown();server.server_close();thread.join(timeout=3)


def test_service_model_load_failure_is_reported(service6,tmp_path):
    broken=SuitabilityService(tmp_path)
    assert broken.model is None
    r=payload(service6);result=VolcanoRFAdapter(broken).rank(r)
    assert result['status']=='invalid_input' and 'model_version_mismatch' in result['errors']


def test_unknown_existing_task_mix_uses_conservative_ranking(service6):
    r=payload(service6);r['gpus']=r['gpus'][2:]
    r['gpus'][0].update(allocated_vgpu_count=1,allocated_compute_quota=10,allocated_memory_mib=1000)
    result=VolcanoRFAdapter(service6).rank(r)
    assert result['classifications'][0]['classification']['prediction_status']=='pending_verification'
    assert result['details']['eligible_gpus'][0]['ranking_basis']=='deterministic_conservative_proxy'


def test_capacity_mapping_does_not_admit_unregistered_memory(service6):
    r=payload(service6);r['gpus']=r['gpus'][0:1]
    r['gpus'][0]['registered_memory_mib']=99999
    result=VolcanoRFAdapter(service6).rank(r)
    assert result['status']=='invalid_input'
    assert 'registered_capacity_not_compatible_with_catalog_variant' in result['errors']
