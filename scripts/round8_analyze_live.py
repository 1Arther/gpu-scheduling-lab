"""Verify saved real round-8 receipts. Never contacts Kubernetes or predicts labels."""
import argparse
import csv
import hashlib
import importlib.util
import json
import re
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def write_csv(path,rows):
    fields=list(dict.fromkeys(k for r in rows for k in r))
    with Path(path).open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        w.writerows({k:json.dumps(v) if isinstance(v,(dict,list)) else v for k,v in r.items()} for r in rows)


def write_json(path,obj):
    Path(path).write_text(json.dumps(obj,indent=2),encoding='utf-8')


def resource_integer(value):
    """Audit integer DecimalSI API quantities (e.g. 1k = 1000 resource units).

    A vgpu-memory unit remains MiB. This is NOT a YAML/model parser change.
    Reject formats not needed by these preserved integer requests.
    """
    m=re.fullmatch(r'([0-9]+)([kMGTPE]?)',str(value))
    if not m:raise ValueError('Unsupported integer API quantity: '+str(value))
    exponent={'':0,'k':3,'M':6,'G':9,'T':12,'P':15,'E':18}[m[2]]
    return int(m[1])*10**exponent


def analyze(source,out):
    source,out=Path(source),Path(out);out.mkdir(parents=True,exist_ok=True)
    helper=source/'helpers/staging.py'
    if not helper.exists():helper=ROOT/'gpu_lab/staging.py'
    spec=importlib.util.spec_from_file_location('round8_saved_staging',helper)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    native_commit_receipt,allocations=module.native_commit_receipt,module.allocations
    ex=source/'execution'
    state=json.loads((ex/'state.json').read_text())
    proof_rows=[];scores=[];deltas=[];release_rows=[];stages=[]
    for path in sorted(ex.glob('*-uuid-proof.json')):
        p=json.loads(path.read_text());uid=p['pod_uid'];advice=p['rf_advice']
        native=native_commit_receipt(p['native_commit_log'])
        assert not native['fallback_used'],'Unexpected native fallback: '+native['fallback']
        uuid=p['native_permanent_uuids'][0]
        assert p['rf_recommended_uuids']==p['native_permanent_uuids']==p['device_plugin_consumed_uuids']
        assert advice['model_version']=='rf-v4-known-states-ee35cfabe30f'
        pod=json.loads((ex/(path.name.replace('-uuid-proof.json','-pod.json'))).read_text())
        req=pod['spec']['containers'][0]['resources']['limits'];mem=resource_integer(req['volcano.sh/vgpu-memory']);core=resource_integer(req['volcano.sh/vgpu-cores'])
        assert pod['metadata']['uid']==uid and pod['spec']['containers'][0]['command']==['/bin/sleep','600']
        ids=allocations(pod['metadata']['annotations']['volcano.sh/vgpu-ids-new'])
        assert [d['uuid'] for d in ids]==[uuid]
        before={g['uuid']:g for g in native['logicalBefore']};after={g['uuid']:g for g in native['logicalAfter']}
        for gpu,old in before.items():
            new=after[gpu]
            delta=tuple(new[k]-old[k] for k in ['allocated_memory_mib','allocated_compute_quota','allocated_vgpu_count'])
            assert delta==((mem,core,1) if gpu==uuid else (0,0,0))
            assert 0<=new['allocated_memory_mib']<=new['registered_memory_mib'] and 0<=new['allocated_compute_quota']<=100
            assert 0<=new['allocated_vgpu_count']<=new['max_instances']
            deltas.append({'pod_uid':uid,'pod_name':p['pod_name'],'gpu_uuid':gpu,'native_memory_before':old['allocated_memory_mib'],
                'native_memory_after':new['allocated_memory_mib'],'native_cores_before':old['allocated_compute_quota'],'native_cores_after':new['allocated_compute_quota'],
                'native_instances_before':old['allocated_vgpu_count'],'native_instances_after':new['allocated_vgpu_count'],
                'source':'direct native Allocate object logicalBefore/After; not unique global cache transaction dump'})
        proof_rows.append(dict(pod_name=p['pod_name'],pod_uid=uid,task_type=p['workload_type'],request_memory_mib=mem,request_compute_quota=core,
            request_vgpu_number=1,rf_uuid=p['rf_recommended_uuids'][0],allocate_uuid=uuid,plugin_uuid=p['device_plugin_consumed_uuids'][0],
            memory_api_quantity=req['volcano.sh/vgpu-memory'],
            phase=p['real_pod_phase'],request_id=advice['request_id'],native_annotation=pod['metadata']['annotations']['volcano.sh/vgpu-ids-new'],
            native_fallback=native['fallback_used'],
            node_annotation=pod['metadata']['annotations']['volcano.sh/vgpu-node'],plugin_evidence=p['device_plugin_evidence_source'],
            label_source='frozen RF synthesized-theory supervision; actual response during real scheduling'))
        inputs={g['uuid']:g for g in advice['gpus']}
        for c in advice['classifications']:
            g=inputs[c['gpu_uuid']];cl=c['classification']
            for task,s in c['request_specific_scores'].items():
                decomp=s.get('score_decomposition',{})
                scores.append(dict(pod_name=p['pod_name'],pod_uid=uid,pod_type=p['workload_type'],score_for_task=task,gpu_uuid=c['gpu_uuid'],
                    selected=c['gpu_uuid']==uuid,label=cl['label'],raw_label=(cl.get('raw_rf') or {}).get('label'),
                    prediction_status=cl['prediction_status'],training_adapted=cl['training_adapted'],inference_adapted=cl['inference_adapted'],
                    free_memory_mib=g['registered_memory_mib']-g['allocated_memory_mib'],free_compute_quota=100-g['allocated_compute_quota'],
                    allocated_vgpu_count=g['allocated_vgpu_count'],hard_feasible=s['feasible'],rejection_reasons=s['rejection_reasons'],
                    rf_contribution=decomp.get('applied_rf_contribution'),heuristic_contribution=decomp.get('heuristic_contribution'),
                    total_score=s.get('score'),raw_rf=cl.get('raw_rf'),request_id=advice['request_id']))
        released=ex/('released-'+uid+'-scheduler.log')
        lines=[line for line in released.read_text().splitlines() if 'rf-job logical release' in line and uid in line]
        assert lines,'No release evidence for '+uid
        release_rows.append(dict(pod_name=p['pod_name'],pod_uid=uid,matching_native_release_log_count=len(lines),
            interpretation='release observed; cloned/cache views may repeat same UID, not counted as separate releases',
            release_log=lines[-1]))
    assert len(proof_rows)==len(state['pods'])==8
    for s in state.get('states',[]):
        c=s['classification'];cl=c['classification'];p=next(p for p in proof_rows if p['pod_uid']==s['pod_uid'])
        task=s['task_type'];sc=c['request_specific_scores'][task];d=sc.get('score_decomposition',{})
        assert s['rf_uuid']==s['allocate_uuid']==s['plugin_uuid']==s['gpu_uuid']
        stages.append(dict(state=s['state'],gpu_uuid=s['gpu_uuid'],free_memory_mib=s['free_memory_mib'],free_compute_quota=s['free_compute_quota'],
            allocated_vgpu_count=s['allocated_vgpu_count'],label=cl['label'],raw_label=cl['raw_rf']['label'],prediction_status=cl['prediction_status'],
            training_adapted=cl['training_adapted'],inference_adapted=cl['inference_adapted'],pod_name=s['pod_name'],pod_uid=s['pod_uid'],task_type=task,
            rf_uuid=s['rf_uuid'],allocate_uuid=s['allocate_uuid'],plugin_uuid=s['plugin_uuid'],rf_contribution=d.get('applied_rf_contribution'),
            heuristic_contribution=d.get('heuristic_contribution'),total_score=sc.get('score'),request_memory_mib=p['request_memory_mib'],request_cores=p['request_compute_quota'],
            scope='real pre-allocation RF/native state; S2-S4 observed by temporary sleep probe',rf_vote_fraction_is_reliability=False))
    assert [s['label'] for s in stages]==['balanced','training_preferred','unavailable','inference_preferred','balanced']
    assert [(s['free_memory_mib'],s['free_compute_quota'],s['allocated_vgpu_count']) for s in stages]==[(24564,100,0),(23564,40,1),(6000,35,2),(7000,95,1),(24564,100,0)]
    restored=json.loads((ex/'restore-confirmation.json').read_text())
    assert restored['original_replicas']==restored['original_available']==1
    assert restored['quota_restored'] and restored['model_and_binary_hashes_equal'] and restored['rf_replicas']==0
    assert restored['own_pods_remaining']==0 and not restored['cleanup_errors']
    final=json.loads((ex/'cleanup-logical-zero-ledger.json').read_text())
    assert not final['node_lock'] and all(g['allocated_memory_mib']==g['allocated_compute_quota']==g['allocated_vgpu_count']==0 for g in final['devices'])
    write_csv(out/'states.csv',stages);write_csv(out/'allocations.csv',proof_rows);write_csv(out/'candidate-scores.csv',scores)
    write_csv(out/'native-ledger-transitions.csv',deltas);write_csv(out/'release-checks.csv',release_rows)
    write_json(out/'summary.json',dict(evidence_kind='real Kubernetes API + real frozen RF + native Volcano + kubelet AllocateResponse; sleep only',
        gpu_uuid=state['target_uuid'],actual_labels=[s['label'] for s in stages],expected_dynamic_transition_verified=True,
        pod_allocations=8,three_way_uuid_matches=8,quota_deltas_correct=8,observed_over_allocations=0,released_pod_uids=8,
        own_pods_remaining=0,original_scheduler_restored=True,quota_restored=True,model_config_binary_unchanged=True,
        restore_confirmation=restored,physical_performance_tested=False,training_inference_speedup_claimed=False,
        UUID_selection_benefit_claimed=False,placement_control='three full-core logical blockers create one native-hard-fit target',
        accounting_scope='direct per-allocation object snapshots plus separately reconstructed API state; not global transaction journal',
        original_stopped_utc=state['stopped_utc'],restored_utc=state['restored_utc']))
    print(json.dumps(dict(gpu_uuid=state['target_uuid'],classes=[s['label'] for s in stages],allocations=len(proof_rows),restored=True)))


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('source',type=Path);ap.add_argument('--output',type=Path,required=True);a=ap.parse_args()
    analyze(a.source,a.output)
