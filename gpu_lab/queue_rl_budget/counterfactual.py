"""OFFLINE diagnostic interventions on old, already seen synthetic trajectories.

Future simulated outcomes are used ONLY to measure counterfactual differences.
They are never placed in PPO observations, rewards for new training, or selection data.
"""
import csv
from collections import defaultdict
import numpy as np
import torch
from sb3_contrib import MaskablePPO
from .common import OUT,OLD,read,dump,csv_write,now
from ..queue_rl.simulator import DecisionSimulator
from ..queue_rl.actions import masks
from ..queue_rl.observations import encode,Scales
from ..queue_rl.workloads import restore
from ..queue_rl.audit import verify_rl
from ..queue_sim.placement import Cluster
from ..queue_study.placement import MemoizedPlacement
from ..queue_study.metrics import extended_metrics,flatten

def run():
    torch.set_num_threads(1)
    with (OUT/'diagnosis/per-action.csv').open(encoding='utf-8-sig') as f:all_rows=list(csv.DictReader(f))
    original=defaultdict(list)
    for r in all_rows:
        if r['chosen']=='True':original[int(r['seed']),r['condition']].append(r)
    eligible=defaultdict(list)
    for (seed,condition),rows in original.items():
        guards=[r for r in rows if int(r['action'])>0]
        if guards:eligible[condition].append((seed,guards[0]))
    selected=[]
    for condition,v in sorted(eligible.items()):
        selected.extend((seed,condition,g) for seed,g in sorted(v,key=lambda x:x[0])[:2])
    # Sample declared and saved BEFORE any counterfactual outcomes are simulated.
    manifest=OUT/'diagnosis/counterfactual-protocol.json'
    if manifest.exists():raise RuntimeError('Counterfactual diagnostic already exists')
    dump(manifest,dict(time=now(),selection='first actual protection in two lowest original-test seeds per condition that contains protection',
        scope='biased toward states actually protected by the old model; NOT a representative estimate of all decision states',
        continuation='identical saved action prefix; intervene only at target decision; frozen original PPO thereafter',
        oracle='best action by realized P95 is OFFLINE future-aware diagnostic, forbidden as deployable policy or training signal',
        sample=[dict(seed=s,condition=c,decision=int(g['decision']),time=float(g['time']),job_id=g['job_id']) for s,c,g in selected]))
    source={(t['seed'],'__'.join(t['condition'].values())):t for t in read(OLD/'inputs/test.json')}
    model=MaskablePPO.load(OLD/read(OLD/'selection.json')['models']['joint']['path'],device='cpu')
    placement=MemoizedPlacement();scales=Scales(**read(OLD/'scales.json'));result=[]
    for seed,cond,target in selected:
        trace=source[seed,cond];idx=int(target['decision']);prefix=original[seed,cond]
        legal=[r for r in all_rows if int(r['seed'])==seed and r['condition']==cond and int(r['decision'])==idx and r['legal']=='True']
        alternatives=[]
        for branch in (int(r['action']) for r in legal):
            f,j,av,cfg=restore(trace);sim=DecisionSimulator(Cluster(f),j,placement,config=cfg,availability=av);count=0
            def choose(context,mask):
                nonlocal count
                current=count;count+=1
                if current<=idx:
                    expected=prefix[current]
                    assert context['time']==float(expected['time']) and context['owner']['job_id']==expected['job_id']
                    a=int(expected['action']) if current<idx else branch
                    assert mask[a]
                    return a,'offline_saved_prefix' if current<idx else 'offline_intervention'
                a,_=model.predict(encode(context,scales),action_masks=np.asarray(mask),deterministic=True)
                return int(a),'frozen_original_ppo_continuation'
            r=sim.run_policy(choose);audit=verify_rl(r,j,f);assert count>idx
            m=flatten(extended_metrics(r,j,f,sim.planning_diagnostics))
            alternatives.append(dict(seed=seed,condition=cond,decision=idx,job_id=target['job_id'],
                intervention=branch,original_action=int(target['action']),audit_passed=audit['success'],
                owner_start=next(x['start_at'] for x in r['jobs'] if x['job_id']==target['job_id']),
                **{k:m[k] for k in ('p95_wait','p99_wait','mean_wait','small_p95_wait','large_p95_wait','long_wait_ratio','completed_work')}))
        no=next(r for r in alternatives if r['intervention']==0)
        for r in alternatives:
            for metric in ('p95_wait','mean_wait','small_p95_wait','long_wait_ratio','completed_work','owner_start'):
                r['delta_'+metric]=r[metric]-no[metric] if r[metric] is not None and no[metric] is not None else None
            r['guardrails_pass']=(r['small_p95_wait'] is None or r['small_p95_wait']<=no['small_p95_wait']+max(.05,.05*no['small_p95_wait'])) and r['long_wait_ratio']<=no['long_wait_ratio']+.005 and r['completed_work']>=.99*no['completed_work']
        result.extend(alternatives)
    csv_write(OUT/'diagnosis/offline-counterfactual.csv',result)
    groups=defaultdict(list)
    for r in result:groups[r['seed'],r['condition'],r['decision']].append(r)
    summary=dict(time=now(),cases=len(groups),branches=len(result),all_audits_passed=all(r['audit_passed'] for r in result),
        cases_any_protection_improves_p95=sum(any(r['intervention']>0 and r['delta_p95_wait']<-1e-8 for r in v) for v in groups.values()),
        cases_any_protection_improves_p95_with_bounds=sum(any(r['intervention']>0 and r['delta_p95_wait']<-1e-8 and r['guardrails_pass'] for r in v) for v in groups.values()),
        original_protection_improves_p95=sum(any(r['intervention']==r['original_action'] and r['delta_p95_wait']<-1e-8 for r in v) for v in groups.values()),
        interpretation='Offline full-synthetic-trace single-decision interventions; realized future used only for diagnostic evaluation, not PPO observation/training/selection; selected states are not population representative')
    dump(OUT/'diagnosis/counterfactual-summary.json',summary);print(summary,flush=True)

if __name__=='__main__':run()
