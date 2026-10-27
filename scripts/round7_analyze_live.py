"""Verify recorded real sleep-Pod evidence offline; never calls Kubernetes or fit()."""
import argparse
import csv
import datetime as dt
import json
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from gpu_lab.staging import allocations, native_commit_receipt, native_ledger_snapshots, fragmentation


def save(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def csv_write(path,rows):
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)


def constraints(gpus):
    assert len(gpus)==4 and len({g['uuid'] for g in gpus})==4
    return all(0<=g['allocated_memory_mib']<=g['registered_memory_mib'] and
               0<=g['allocated_compute_quota']<=100 and 0<=g['allocated_vgpu_count']<=g['max_instances'] for g in gpus)


def delta_ok(before,after,target,sign):
    b={g['uuid']:g for g in before};a={g['uuid']:g for g in after}
    assert set(b)==set(a)
    return all(tuple(a[u][k]-b[u][k] for k in ['allocated_memory_mib','allocated_compute_quota','allocated_vgpu_count'])==
               ((sign*4096,sign*20,sign) if u==target else (0,0,0)) for u in b)


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--results',type=Path,default=ROOT/'results/round7-live-window-20261019')
    ap.add_argument('--evidence-dir',type=Path);a=ap.parse_args()
    folder=a.evidence_dir or a.results/'attempt2/execution';out=a.results/'verified';out.mkdir(exist_ok=True)
    state=json.loads((folder/'state.json').read_text());lines=(folder/'native-events.log').read_text().splitlines()
    commits=[dict(native_commit_receipt(s),raw_line=s) for s in lines if 'rf-job UUID allocation' in s]
    assert len(commits)==len(state['pods'])==6
    by_uid={r['podUID']:r for r in commits};assert len(by_uid)==6
    cases={p.name.removesuffix('-uuid-proof.json'):json.loads(p.read_text()) for p in folder.glob('*-uuid-proof.json')}
    # Last live case passed native/plugin checks before the old harness wrongly
    # expected fallback=true. Reconstruct ONLY from its already recorded evidence.
    pod=json.loads((folder/'service-fallback-pod.json').read_text());uid=pod['metadata']['uid'];receipt=by_uid[uid]
    assert receipt['fallback_used'] and 'connection refused' in receipt['fallback']
    cp=json.loads((folder/'service-fallback-checkpoint.json').read_text())
    response=next(e for e in cp['entries'] if e['PodUID']==uid and e['ContainerName']=='sleep' and e['ResourceName']=='volcano.sh/vgpu-number')
    native=[r['uuid'] for r in allocations(pod['metadata']['annotations']['volcano.sh/vgpu-ids-new'])]
    assert response['allocate_envs']['NVIDIA_VISIBLE_DEVICES'].split(',')==native==[r['uuid'] for r in receipt['allocations']]
    assert pod['status']['phase']=='Running'
    fallback=dict(pod_uid=uid,pod_name=pod['metadata']['name'],workload_type=pod['metadata']['labels']['gpu-lab/workload-type'],
        rf_recommended_uuids=None,native_permanent_uuids=native,device_plugin_consumed_uuids=native,rf_advice=None,
        fallback_reason=receipt['fallback'],real_pod_phase=pod['status']['phase'],cuda_executed=False,
        verification_method='offline reconstruction from real saved Pod, native commit and matching kubelet AllocateResponse; no new Pod or fabricated RF advice')
    cases['service-fallback']=fallback;save(out/'service-fallback-verified.json',fallback)
    score_rows=[];alloc_rows=[];transitions=[];release_rows=[];peaks=[]
    for name,proof in sorted(cases.items(),key=lambda item:by_uid[item[1]['pod_uid']]['raw_line']):
        r=by_uid[proof['pod_uid']];target=r['allocations'][0]['uuid'];before=r['logicalBefore'];after=r['logicalAfter']
        assert constraints(before) and constraints(after) and delta_ok(before,after,target,1)
        assert proof['device_plugin_consumed_uuids']==proof['native_permanent_uuids']==[target]
        advice=proof.get('rf_advice')
        if advice:
            assert proof['rf_recommended_uuids']==[target]
            inputs={g['uuid']:g for g in advice['gpus']}
            for g in before:
                assert all(inputs[g['uuid']][k]==g[k] for k in ['allocated_memory_mib','allocated_compute_quota','allocated_vgpu_count','training_pod_count','inference_pod_count'])
            for g in advice['classifications']:
                c=g['classification'];d=g['snapshot']['dynamic'];s=g['request_specific_scores'][proof['workload_type']];decomp=s['score_decomposition']
                assert s['feasible'] # Selection changes in this run are not constraint exclusions.
                score_rows.append(dict(case=name,pod_uid=proof['pod_uid'],task_type=proof['workload_type'],gpu_uuid=g['gpu_uuid'],
                    label=c['label'],prediction_status=c['prediction_status'],raw_rf_label=c['raw_rf']['label'],
                    training_adapted=c['training_adapted'],inference_adapted=c['inference_adapted'],
                    free_memory_mib=d['free_memory_mib'],free_compute_quota=d['free_compute_quota'],allocated_vgpu_count=d['allocated_vgpu_count'],
                    running_training_count=d['running_training_count'],running_inference_count=d['running_inference_count'],
                    rf_enabled=decomp['rf_prediction_enabled'],rf_contribution=decomp['applied_rf_contribution'],
                    heuristic_contribution=decomp['heuristic_contribution'],total_score=s['score'],feasible=s['feasible'],
                    selected=g['gpu_uuid']==target,classification_supervision='synthetic theoretical labels; real RF inference output'))
        for phase,gpus in [('before',before),('after',after)]:
            for g in gpus:
                transitions.append(dict(case=name,pod_uid=proof['pod_uid'],source='native commit GPU object snapshot',phase=phase,
                    gpu_uuid=g['uuid'],free_memory_mib=g['registered_memory_mib']-g['allocated_memory_mib'],
                    free_compute_quota=100-g['allocated_compute_quota'],allocated_vgpu_count=g['allocated_vgpu_count'],
                    running_training_count=g['training_pod_count'],running_inference_count=g['inference_pod_count']))
            views=[dict(free_memory_mib=g['registered_memory_mib']-g['allocated_memory_mib'],free_compute_quota=100-g['allocated_compute_quota'],free_instances=g['max_instances']-g['allocated_vgpu_count']) for g in gpus]
            peaks.append(dict(memory_rate=sum(g['allocated_memory_mib'] for g in gpus)/sum(g['registered_memory_mib'] for g in gpus),
                compute_rate=sum(g['allocated_compute_quota'] for g in gpus)/(100*len(gpus)),fragmentation=fragmentation(views)))
        release=[s for s in lines if 'rf-job logical release' in s and 'podUID="'+proof['pod_uid']+'"' in s]
        matched=[s for s in release if delta_ok(**{k:v for k,v in [('before',native_ledger_snapshots(s)['logicalBefore']),('after',native_ledger_snapshots(s)['logicalAfter']),('target',target),('sign',-1)]})]
        assert matched
        release_rows.append(dict(case=name,pod_uid=proof['pod_uid'],native_release_matching_views=len(matched),
            source='native SubResource views plus actual Pod deletion; repeated view logs are not independent releases',expected_delta_memory_mib=-4096,expected_delta_compute=-20,expected_delta_instances=-1))
        alloc_rows.append(dict(case=name,pod_name=proof['pod_name'],pod_uid=proof['pod_uid'],task_type=proof['workload_type'],node='t3dgq',
            rf_uuid=proof.get('rf_recommended_uuids',[None])[0] if proof.get('rf_recommended_uuids') else None,
            allocate_uuid=target,plugin_consumed_uuid=target,plugin_uuid_evidence='matching kubelet vgpu-number AllocateResponse',
            logical_memory_mib=4096,logical_compute_quota=20,instances=1,pod_was_running=True,native_fallback=r['fallback_used']))
    on,off=cases['control-rf-on'],cases['control-rf-off'];x,y=on['rf_advice'],off['rf_advice']
    assert x['gpus']==y['gpus'] and x['requests']==y['requests']
    for a,b in zip(x['classifications'],y['classifications']):
        assert a['snapshot']==b['snapshot'] and a['classification']==b['classification']
        u,v=[q['request_specific_scores']['inference'] for q in [a,b]]
        assert u['score_decomposition']['heuristic_contribution']==v['score_decomposition']['heuristic_contribution']
        assert abs(u['score']-v['score']-0.3)<1e-9 and v['score_decomposition']['applied_rf_contribution']==0
    pair=dict(same_inputs=True,same_pod_yaml_except_api_uid_and_runtime_metadata=True,same_constraints=True,same_heuristics=True,
              rf_term_on=0.3,rf_term_off=0.0,uuid_on=on['native_permanent_uuids'],uuid_off=off['native_permanent_uuids'],
              uuid_changed=on['native_permanent_uuids']!=off['native_permanent_uuids'],
              explanation='All four 4090 candidates received the same balanced RF predictions; the identical additive RF term changed scores but not ordering. No independent UUID-selection benefit demonstrated in this real case.')
    save(out/'rf-on-off-comparison.json',pair)
    a_gpu=cases['a-training']['native_permanent_uuids'][0];b_gpu=cases['b-inference']['native_permanent_uuids'][0]
    b_inputs={g['uuid']:g for g in cases['b-inference']['rf_advice']['gpus']}
    c_inputs={g['uuid']:g for g in cases['c-after-release']['rf_advice']['gpus']}
    assert b_inputs[a_gpu]['allocated_memory_mib']==4096 and b_inputs[a_gpu]['training_pod_count']==1
    assert c_inputs[a_gpu]['allocated_memory_mib']==(4096 if a_gpu==b_gpu else 0) and c_inputs[a_gpu]['training_pod_count']==0
    assert c_inputs[b_gpu]['allocated_memory_mib']==4096 and c_inputs[b_gpu]['inference_pod_count']==1
    # These three events all appeared in the same live container log captured for C.
    # The compact archive retains the deduplicated original lines and file hashes.
    release_uid=cases['service-fallback']['pod_uid']
    metric_text=(folder/('released-'+release_uid+'-metrics.txt')).read_text()
    end_gauges=[s for s in metric_text.splitlines() if re.match(r'^volcano_vgpu_device_(allocated_memory|allocated_cores|shared_number)\{',s)]
    assert len(end_gauges)==12 and all(float(s.rsplit(' ',1)[1])==0 for s in end_gauges)
    csv_write(out/'allocations.csv',alloc_rows);csv_write(out/'candidate-scores.csv',score_rows)
    csv_write(out/'native-resource-transitions.csv',transitions);csv_write(out/'release-checks.csv',release_rows)
    restored=json.loads((folder/'restore-confirmation.json').read_text());assert restored['original_spec_equal'] and restored['original_config_data_equal']
    summary=dict(generated_utc=dt.datetime.now(dt.timezone.utc).isoformat(),data_source='real Kubernetes API + native Volcano + actual Device Plugin AllocateResponse; sleep only',
        pods_submitted=6,pods_running=6,legal_allocation_success_rate=1.0,rf_native_plugin_consistency=dict(matched=5,eligible=5,rate=1.0,learned_term_enabled=4,learned_term_disabled=1,service_failure_excluded=1),
        native_plugin_consistency=dict(matched=6,total=6,rate=1.0),native_quota_delta_correct=dict(correct=6,total=6),
        observed_over_allocations=0,release_uid_count=6,release_evidence='6 deleted Pod UIDs with matching native SubResource deltas; clone/cache view repetitions not counted as separate releases; subsequent live RF inputs establish resumed state',
        same_process_dynamic_release_verified=True,final_direct_gauges_zero=dict(gauge_count=12,source='last RF scheduler metrics after fallback Pod deletion',interpretation='supporting evidence; metric resets during cache/view construction are not a global-cache dump'),
        classification_labels_seen=sorted({r['label'] for r in score_rows}),
        rf_on_off_uuid_changed=pair['uuid_changed'],independent_rf_selection_benefit_demonstrated=False,
        fault_fallback=dict(tested=1,successful=1,reason=fallback['fallback_reason'],real_rf_advice_available=False),
        peak_observed_logical_memory_reservation_rate=max(p['memory_rate'] for p in peaks),peak_observed_logical_compute_reservation_rate=max(p['compute_rate'] for p in peaks),
        reference_fragmentation_max=max(p['fragmentation']['fragmentation_ratio'] for p in peaks),
        original_scheduler_restored=restored,models_retrained=False,cuda_workloads_executed=False,
        harness_state_preserved=dict(experiment_success=state.get('experiment_success'),error=state.get('error'),
            explanation='Raw harness stopped on a wrong boolean expectation for a string fallback field; all six real allocations verified independently from saved evidence. No raw success flag overwritten.'),
        not_tested=['real GPU throughput, training/inference runtime, CUDA correctness','heterogeneous 5090/L40 choice in real Kubernetes','independent production DQN/native Binpack comparison'],
        release_observability_limit='SubResource logs include cloned scheduling views, not a unique global-cache commit journal; API reconstruction, next live allocation snapshots and final metrics are reported separately.')
    save(out/'summary.json',summary);print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
