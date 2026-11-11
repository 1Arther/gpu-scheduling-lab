"""Evidence tests use the frozen Go log contracts, not a simulated scheduler."""
import base64
import copy
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
spec = importlib.util.spec_from_file_location('hrrn_s5_evidence', ROOT/'scripts/hrrn_stage5_evidence.py')
e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e)


def line(second, text):
    return f'2026-10-27T03:00:{second:02d}.250000001Z I1027 03:00:{second:02d}.250000       1 file.go:1] {text}'


def snapshot(job='ns/short', second=20, ready=True, session='s1', score=3, estimated=10, ready_at='2026-10-27T03:00:00Z'):
    return line(second, '"HRRN session snapshot" '+f'session="{session}" job="{job}" ready={str(ready).lower()} readyAt="{ready_at}" waitingSeconds={second} estimatedSeconds={estimated} score={score} reason="complete_observed;podgroup_annotation" completeJob=false')


def pod(name='short', uid='pod-uid', node='t3dgq', uuid='GPU-a', cores=40):
    return {'metadata': {'namespace':'ns','name':name,'uid':uid,'creationTimestamp':'2026-10-27T03:00:00Z',
            'annotations': {'scheduling.k8s.io/group-name':name,'volcano.sh/vgpu-ids-new':f'{uuid},NVIDIA,4096,{cores}:'}},
            'spec': {'nodeName':node, 'containers':[{'name':'sleep','resources':{'limits':{'volcano.sh/vgpu-number':1,'volcano.sh/vgpu-memory':4096,'volcano.sh/vgpu-cores':cores}}}]},
            'status':{'phase':'Running','startTime':'2026-10-27T03:00:21Z',
              'conditions':[{'type':'PodScheduled','status':'True','lastTransitionTime':'2026-10-27T03:00:21Z'}],
              'containerStatuses':[{'name':'sleep','state':{'running':{'startedAt':'2026-10-27T03:00:22Z'}}}]}}


def group(name='short', owners=None):
    return {'metadata':{'namespace':'ns','name':name,'uid':'pg-uid','ownerReferences':owners or []}}


def native(second=20, request='r1', devices='GPU-a,NVIDIA,4096,40:', uid='pod-uid', node='t3dgq', fallback=''):
    return line(second, '"rf-job UUID allocation" '+f'route="rf-job" pod="ns/short" podUID="{uid}" node="{node}" requestID="{request}" devices="{devices}" score=26.6 fallback="{fallback}" logicalBefore=[] logicalAfter=[]')


def release(second, uid='pod-uid'):
    return line(second, f'"rf-job logical release" pod="ns/short" podUID="{uid}" node="t3dgq" logicalBefore=[] logicalAfter=[]')


def advice(request='r1', uid='pod-uid', uuid='GPU-a', cores=40):
    return {'event':'rf_advice','request_id':request,'model_version':e.MODEL,
            'rf_prediction_enabled':True,'model_ready':True,'status':'ranked',
            'pod':{'uid':uid,'namespace':'ns','name':'short'},
            'assignments':[{'container':'sleep','unit':0,'gpu_uuid':uuid,'node':'t3dgq',
                            'memory_mib':4096,'compute_quota':cores,'instances':1}]}


def cp(uuid='GPU-a', uid='pod-uid', container='sleep'):
    return {'entries':[{'PodUID':uid,'ContainerName':container,'ResourceName':'volcano.sh/vgpu-number',
                        'DeviceIDs':['virtual-slot-17'], 'allocate_envs':{'NVIDIA_VISIBLE_DEVICES':uuid}}]}


def test_snapshot_clock_is_direct_and_latest_not_ready_clears_prior_state():
    logs='\n'.join([snapshot(),snapshot(second=23,ready=False,session='s2',score=1),
                    'HRRN session snapshot session="truncated"'])
    snapshots=e.parse_snapshots(logs)
    assert len(snapshots)==2
    assert snapshots[0]['observed_at']=='2026-10-27T03:00:20.250000001Z'
    latest=e.latest_waiting(logs,['ns/short','missing'])
    assert latest['ns/short']['ready'] is False
    assert latest['ns/short']['session']=='s2'
    assert latest['missing'] is None


def test_basename_resolution_never_merges_different_namespaces():
    logs=snapshot('ns/short')+'\n'+snapshot('another/short')
    assert e.latest_waiting(logs,['short'])=={'short':None}
    assert e.latest_waiting(logs,['ns/short'])['ns/short']['job']=='ns/short'


def test_comparator_ties_are_separate_from_derived_hrrn_and_speculative_planning():
    logs='\n'.join([snapshot('ns/long',score=1.2,estimated=100),snapshot(),
        line(20,'Priority JobOrderFn: <ns/long> priority: 0, <ns/short> priority: 0'),
        line(20,'Gang JobOrderFn: <ns/long> is ready: false, <ns/short> is ready: false'),
        line(20,'DRF JobOrderFn: <ns/long> share state: 0, <ns/short> share state: 0'),
        line(20,'Try to allocate resource to 1 tasks of Job <ns/short>'),
        line(20,'Binding Task <ns/short> to node <t3dgq>')])
    out=e.comparison_evidence(logs,'ns/long','ns/short')
    assert out['all_three_preceding_plugins_have_tie_evidence']
    assert out['hrrn_result']==1
    assert out['hrrn_direct_comparator_log_available'] is False
    plans=e.parse_decisions(logs)
    assert plans['job_selections'][0]['job']=='ns/short'
    assert plans['task_plans'][0]['evidence_source']=='speculative_plan_not_API_binding'


def test_native_session_markers_identify_allocating_cycle_when_hrrn_disabled():
    # framework/session.go V3 logs occur in both A and B configurations.
    log='\n'.join([
        line(20, 'Open Session native-a with <2> Job and <1> Queues'),
        line(20, 'Priority JobOrderFn: <ns/long> priority: 0, <ns/short> priority: 0'),
        line(20, 'Gang JobOrderFn: <ns/long> is ready: false, <ns/short> is ready: false'),
        line(20, 'DRF JobOrderFn: <ns/long> share state: 0, <ns/short> share state: 0'),
        line(20, 'Try to allocate resource to 1 tasks of Job <ns/short>'), native(),
        line(20, 'Close Session native-a')])
    result=e.uuid_evidence(pod(),log,json.dumps(advice()),cp())
    assert result['status']=='confirmed'
    assert result['selected_native_allocation']['session']=='native-a'
    assert result['selected_job_selection']['session']=='native-a'
    assert result['hrrn_snapshot_at_allocation'] is None
    comparison=e.comparison_evidence(log,'ns/long','ns/short',session='native-a',
        before_line=result['selected_native_allocation']['line_number'])
    assert comparison['all_three_preceding_plugins_have_tie_evidence']
    assert comparison['hrrn_result'] is None
    assert comparison['snapshots']=={}


def test_closed_session_is_not_reused_for_later_async_log():
    log='\n'.join([snapshot(),line(21,'Close Session s1'),release(22)])
    assert e.parse_decisions(log)['releases'][0]['session'] is None


def test_a_preceding_non_tie_is_not_hidden_by_hrrn_scores():
    logs='\n'.join([snapshot('ns/long',score=1.2),snapshot(),
        line(20,'Priority JobOrderFn: <ns/long> priority: 5, <ns/short> priority: 0'),
        line(20,'Gang JobOrderFn: <ns/long> is ready: true, <ns/short> is ready: false'),
        line(20,'DRF JobOrderFn: <ns/long> share state: 0.1, <ns/short> share state: 0')])
    out=e.comparison_evidence(logs,'ns/long','ns/short')
    assert out['any_preceding_non_tie']
    assert not out['all_three_preceding_plugins_have_tie_evidence']
    assert [r['result'] for r in out['direct_preceding_comparator_inputs']]==[-1,1,1]


def test_standalone_pg_has_no_invented_job_uid_or_exact_binding_timestamp():
    p=pod(); logs=snapshot()+'\n'+snapshot(second=30,session='later',score=4)
    row=e.extract_timing([p],[group()],logs,{'pods':{'pod-uid':{'first_bound_at':'2026-10-27T03:00:21.5Z'}}})[0]
    assert row['volcano_job_uid'] is None
    assert row['job_info_id']=='ns/short' and row['podgroup_uid']=='pg-uid'
    assert row['hrrn_score']==3 and row['waiting_seconds']==20
    assert row['queue_wait_seconds']==21
    assert row['binding_api_at'] is None
    assert row['first_bound_observed_at']=='2026-10-27T03:00:21.5Z'
    assert row['scheduling_decision_seconds'] is None


def test_actual_job_owner_audit_and_completion_are_separate_from_sleep_estimate():
    p=pod(); p['status']['phase']='Succeeded'
    p['status']['containerStatuses'][0]['state']={'terminated':{'startedAt':'2026-10-27T03:00:22Z','finishedAt':'2026-10-27T03:00:42Z'}}
    obs={'pods':{'pod-uid':{'binding_api_at':'2026-10-27T03:00:20.9Z','binding_api_source':'audit'}}}
    logs='\n'.join([snapshot(estimated=100),release(10),release(43,uid='different-uid'),release(44)])
    owner={'kind':'Job','apiVersion':'batch.volcano.sh/v1alpha1','uid':'real-vc-job'}
    row=e.extract_timing([p],[group(owners=[owner])],logs,obs)[0]
    assert row['volcano_job_uid']=='real-vc-job'
    assert row['binding_api_at']=='2026-10-27T03:00:20.9Z'
    assert row['container_runtime_seconds']==20
    assert row['estimated_service_seconds']==100
    assert row['logical_release_at']=='2026-10-27T03:00:44.250000001Z'


def test_external_readiness_never_becomes_an_internal_tracker_timestamp():
    p=pod(); obs={'jobs':{'ns/short':{'complete_ready_observed_at':'2026-10-27T03:00:01Z'}},
                'pods':{'pod-uid':{'binding_api_at':'2026-10-27T03:00:21Z','binding_api_source':'PodScheduled'}}}
    row=e.extract_timing([p],[group()],'',obs)[0]
    assert row['complete_ready_at'] is None and row['hrrn_score'] is None
    assert row['queue_wait_seconds'] is None
    assert row['external_queue_wait_seconds']==20
    assert row['binding_api_at'] is None


def test_rollback_requires_unbound_pending_and_all_assignment_dimensions_clean():
    p=pod(node='');p['metadata']['annotations']={'scheduling.k8s.io/group-name':'short'};p['status']={'phase':'Pending'}
    assert e.rollback_annotations([p])['clean']
    for key in e.ASSIGNMENT_KEYS:
        dirty=copy.deepcopy(p);dirty['metadata']['annotations'][key]='leftover'
        assert not e.rollback_annotations([dirty])['clean']
    p['spec']['nodeName']='t3dgq'
    assert not e.rollback_annotations([p])['clean']
    assert not e.rollback_annotations([])['clean']


def test_unique_bound_receipt_correlates_rf_request_and_plugin_uuid():
    result=e.uuid_evidence(pod(),native(),json.dumps(advice()),cp())
    assert result['status']=='confirmed' and result['uuid_consistent']
    assert result['selected_request_id']=='r1'
    assert result['native_allocation_is_bind_commit'] is False


def test_rolled_back_attempt_is_not_mislabelled_as_final_allocation():
    logs='\n'.join([native(second=10,request='rolled-back',devices='GPU-wrong,NVIDIA,4096,40:'),
                    release(11),native(request='final')])
    result=e.uuid_evidence(pod(),logs,json.dumps(advice(request='final')),cp())
    assert result['status']=='confirmed' and result['selected_request_id']=='final'
    assert result['native_attempt_count']==2 and result['rollback_release_count_before_final_allocation']==1


def test_repeated_final_segment_receipts_preserve_request_ambiguity():
    logs=native(second=19,request='r0')+'\n'+native(request='r1')
    rf='\n'.join(map(json.dumps,[advice(request='r0'),advice(request='r1')]))
    result=e.uuid_evidence(pod(),logs,rf,cp())
    assert result['uuid_consistent'] and result['status']=='uuid_consistent_request_ambiguous'
    assert result['selected_request_id'] is None


def test_wrong_uid_or_virtual_device_id_cannot_prove_consumption():
    assert e.uuid_evidence(pod(),native(),json.dumps(advice(uid='wrong')),cp())['status']=='insufficient_evidence'
    assert e.uuid_evidence(pod(),native(),json.dumps(advice()),cp(uid='wrong'))['status']=='insufficient_evidence'
    fake=cp();fake['entries'][0].pop('allocate_envs');fake['entries'][0]['DeviceIDs']=['GPU-a']
    assert e.uuid_evidence(pod(),native(),json.dumps(advice()),fake)['status']=='insufficient_evidence'


def test_uuid_or_quota_mismatch_and_unbound_pod_are_rejected():
    assert e.uuid_evidence(pod(),native(),json.dumps(advice()),cp(uuid='GPU-b'))['status']=='mismatch'
    assert e.uuid_evidence(pod(),native(),json.dumps(advice(cores=60)),cp())['status']=='mismatch'
    assert e.uuid_evidence(pod(),native(node='other-node'),json.dumps(advice()),cp())['status']=='mismatch'
    result=e.uuid_evidence(pod(node=''),native(),json.dumps(advice()),cp())
    assert not result['uuid_consistent']


def test_raw_checkpoint_protobuf_is_decoded_instead_of_device_ids():
    key=b'NVIDIA_VISIBLE_DEVICES';value=b'GPU-a'
    entry=b'\x0a'+bytes([len(key)])+key+b'\x12'+bytes([len(value)])+value
    payload=b'\x0a'+bytes([len(entry)])+entry
    raw={'Data':json.dumps({'PodDeviceEntries':[{'PodUID':'pod-uid','ContainerName':'sleep',
            'ResourceName':'volcano.sh/vgpu-number','DeviceIDs':{'0':['virtual-index']},
            'AllocResp':base64.b64encode(payload).decode()}]})}
    result=e.uuid_evidence(pod(),native(),json.dumps(advice()),raw)
    assert result['status']=='confirmed'


def test_multi_container_gpu_proof_requires_all_containers():
    p=pod();p['spec']['containers'].append({'name':'second','resources':{'limits':{'volcano.sh/vgpu-number':1,'volcano.sh/vgpu-memory':2048,'volcano.sh/vgpu-cores':20}}})
    devices='GPU-a,NVIDIA,4096,40:;GPU-b,NVIDIA,2048,20:'
    p['metadata']['annotations']['volcano.sh/vgpu-ids-new']=devices
    a=advice();a['assignments'].append({'container':'second','unit':0,'gpu_uuid':'GPU-b','node':'t3dgq',
                                      'memory_mib':2048,'compute_quota':20,'instances':1})
    checkpoint=cp();checkpoint['entries'].extend(cp(uuid='GPU-b',container='second')['entries'])
    assert e.uuid_evidence(p,native(devices=devices),json.dumps(a),checkpoint)['status']=='confirmed'
    checkpoint['entries'].pop()
    assert e.uuid_evidence(p,native(devices=devices),json.dumps(a),checkpoint)['status']=='insufficient_evidence'


def test_duplicate_checkpoint_or_conflicting_rf_record_is_not_silently_selected():
    duplicated=cp();duplicated['entries']*=2
    assert e.uuid_evidence(pod(),native(),json.dumps(advice()),duplicated)['status']=='insufficient_evidence'
    rf=json.dumps(advice())+'\n'+json.dumps(advice(cores=99))
    assert e.uuid_evidence(pod(),native(),rf,cp())['status']=='insufficient_evidence'


def test_timestamp_without_timezone_and_native_klog_only_have_no_invented_date():
    assert e.timestamp('2026-10-27T03:00:00') is None
    log=snapshot().split(' ',1)[1]
    assert e.parse_snapshots(log)[0]['observed_at'] is None


def test_score_tie_uses_real_nanosecond_ready_order_before_uid():
    logs=snapshot('ns/a',score=3,ready_at='2026-10-27T03:00:00.000000002Z')+'\n'+snapshot('ns/z',score=3,ready_at='2026-10-27T03:00:00.000000001Z')
    assert e.comparison_evidence(logs,'ns/a','ns/z')['hrrn_result']==1
    logs=snapshot('ns/a',score=3,ready_at='2026-10-27T03:00:00.1Z')+'\n'+snapshot('ns/z',score=3,ready_at='2026-10-27T03:00:00Z')
    assert e.comparison_evidence(logs,'ns/a','ns/z')['hrrn_result']==1


def test_identical_request_hash_may_have_multiple_equivalent_audit_timestamps():
    a=advice();b=advice();a['utc']='2026-10-27T03:00:18Z';b['utc']='2026-10-27T03:00:20Z'
    result=e.uuid_evidence(pod(),native(),json.dumps(a)+'\n'+json.dumps(b),cp())
    assert result['status']=='confirmed'
    assert result['rf_proofs'][0]['equivalent_audit_occurrences']==2


def test_actual_pod_quota_not_only_three_matching_uuid_strings_is_verified():
    p=pod();p['spec']['containers'][0]['resources']['limits']['volcano.sh/vgpu-cores']=60
    assert e.uuid_evidence(p,native(),json.dumps(advice()),cp())['status']=='mismatch'
    p=pod();p['spec']['containers'][0]['resources']['limits']['volcano.sh/vgpu-number']=2
    assert e.uuid_evidence(p,native(),json.dumps(advice()),cp())['status']=='mismatch'
    p=pod();p['spec']['containers'][0]['resources']['limits']['volcano.sh/vgpu-memory']='4k'
    p['metadata']['annotations']['volcano.sh/vgpu-ids-new']='GPU-a,NVIDIA,4000,40:'
    a=advice();a['assignments'][0]['memory_mib']=4000
    assert e.uuid_evidence(p,native(devices='GPU-a,NVIDIA,4000,40:'),json.dumps(a),cp())['status']=='confirmed'


def test_bound_uuid_proof_joins_the_allocating_session_not_later_running_snapshots():
    logs='\n'.join([snapshot(score=3),line(20,'Try to allocate resource to 1 tasks of Job <ns/short>'),
                    native(),snapshot(second=30,session='later',score=4)])
    result=e.uuid_evidence(pod(),logs,json.dumps(advice()),cp())
    assert result['hrrn_snapshot_at_allocation']['session']=='s1'
    assert result['hrrn_snapshot_at_allocation']['score']==3
    assert result['selected_job_selection']['job']=='ns/short'
    assert result['selected_native_allocation']['line_number']==2
