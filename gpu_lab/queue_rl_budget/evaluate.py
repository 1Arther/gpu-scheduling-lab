"""New development set only. Shared frozen backend and independent ledger audit."""
import argparse
from concurrent.futures import ProcessPoolExecutor
from collections import Counter
from time import perf_counter,sleep
import numpy as np
from .common import OUT,OLD,SEEDS,METRICS,read,dump,csv_write,finite_mean,now,verify_inputs
from ..queue_rl.experiment import evaluate_trace
from ..queue_rl.observations import Scales
from ..queue_rl.policies import HeuristicSpec
from .diagnose import probabilities,action_stats

_state={}

def worker(task):
    trace,policy,seed,budget,path=task
    if not _state:
        import torch
        from ..queue_study.placement import MemoizedPlacement
        torch.set_num_threads(1)
        _state.update(placement=MemoizedPlacement(),scales=Scales(**read(OUT/'scales.json')),models={})
    model=None
    if path:
        if path not in _state['models']:
            from sb3_contrib import MaskablePPO
            _state['models'][path]=MaskablePPO.load(path,device='cpu')
        model=_state['models'][path]
    row,result=evaluate_trace(trace,policy,_state['placement'],_state['scales'],model,'joint',
        HeuristicSpec(**read(OLD/'selection.json')['heuristic']['spec']),path)
    acts=row.pop('actions');stats=action_stats(acts)
    probabilities_rows=[]
    if model and acts:
        pp,ee=probabilities(model,acts,_state['scales'])
        row['metrics']['policy_entropy']=float(np.mean(ee))
        row['metrics']['protect_probability']=float(np.mean(pp[:,1:].sum(axis=1)))
        for a,p,e in zip(acts,pp,ee):
            probabilities_rows.append(dict(time=a['context']['time'],job_id=a['context']['owner']['job_id'],
                action=a['action'],reason=a['reason'],mask=a['mask'],probabilities=p.tolist(),entropy=float(e)))
    row.update(training_seed=seed,budget=budget,action_statistics=stats,action_records=probabilities_rows,
               reason_counts=dict(Counter(a['reason'] for a in acts)),jobs=result['jobs'])
    return row

def model_path(seed,budget):
    suffix='-full32768' if budget==32768 else ''
    return OUT/f'training/seed-{seed}{suffix}/model-{budget}.zip'

def group_name(policy,seed,budget):return policy if seed is None else f'ppo-{budget}-seed{seed}'

def summarize_group(rows):
    keys=sorted({k for r in rows for k,v in r['metrics'].items() if v is None or isinstance(v,(int,float))})
    result={k:finite_mean(r['metrics'].get(k) for r in rows) for k in keys}
    result.update(action_counts=[sum(r['action_statistics']['chosen'][k] for r in rows) for k in range(4)],
        legal_counts=[sum(r['action_statistics']['legal_counts'][k] for r in rows) for k in range(4)],
        opportunities=sum(r['action_statistics']['opportunities'] for r in rows))
    result['conditional_protect']=sum(result['action_counts'][1:])/max(1,result['opportunities'])
    return result

def summary():
    result={}
    for p in sorted((OUT/'development').glob('*.json')):
        rows=read(p);result[p.stem]=summarize_group(rows)
    dump(OUT/'analysis/development-summary.json',result)
    csv_write(OUT/'analysis/development-summary.csv',[dict(model=k,**v) for k,v in result.items()])
    return result

def promotion(s,budget):
    h=s['hrrn'];best=min(s[k]['p95_wait'] for k in ('hrrn_easy','hrrn_protect','hrrn_adaptive_heuristic'))
    rows=[]
    for seed in SEEDS:
        v=s[f'ppo-{budget}-seed{seed}']
        criteria=dict(beats_hrrn=v['p95_wait']<=.97*h['p95_wait'],beats_strong=v['p95_wait']<=.99*best,
            small_ok=v['small_p95_wait']<=h['small_p95_wait']+max(.05,.05*h['small_p95_wait']),
            long_ok=v['long_wait_ratio']<=h['long_wait_ratio']+.005,
            work_ok=v['completed_work']>=.99*h['completed_work'])
        rows.append(dict(seed=seed,**criteria,passes=all(criteria.values())))
    mean=lambda b:float(np.mean([s[f'ppo-{b}-seed{x}']['p95_wait'] for x in SEEDS]))
    change512=(mean(512)-mean(budget))/mean(512);change0=(mean(0)-mean(budget))/mean(0)
    ok=sum(r['passes'] for r in rows)>=2 and change512>=.01 and change0>=.03
    return dict(passes=ok,seeds=rows,improvement_vs512=change512,improvement_vs0=change0)

def gate():
    s=summary();p=promotion(s,8192)
    improvements=[1-s[f'ppo-8192-seed{x}']['p95_wait']/s[f'ppo-2048-seed{x}']['p95_wait'] for x in SEEDS]
    avg=lambda b:np.mean([s[f'ppo-{b}-seed{x}']['p95_wait'] for x in SEEDS])
    gain=float(1-avg(8192)/avg(2048))
    stop=gain<.01 and sum(x>=.01 for x in improvements)<2 and not p['passes']
    result=dict(time=now(),mean_p95_improvement_2048_to8192=gain,seed_p95_improvements=improvements,
        promotion=p,stop_before32768=bool(stop),final_test_executed=False,
        interpretation='Development selection evidence only. No new test generated unless the predeclared promotion criterion passes.')
    dump(OUT/'staircase-decision.json',result);print(result,flush=True);return result

def run(workers=2,budgets=(0,512,2048,8192)):
    verify_inputs();traces=read(OUT/'inputs/development.json');start=perf_counter()
    groups=[(p,None,None,None) for p in ('hrrn','hrrn_easy','hrrn_protect','hrrn_adaptive_heuristic')]
    groups += [('hrrn_ppo',seed,b,str(model_path(seed,b))) for b in budgets for seed in SEEDS]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for policy,seed,budget,path in groups:
            name=group_name(policy,seed,budget);dest=OUT/f'development/{name}.json'
            if dest.exists():continue
            if path:
                meta=model_path(seed,budget).with_name(f'metadata-{budget}.json')
                while not meta.exists():sleep(2)
            rows=list(pool.map(worker,[(t,policy,seed,budget,path) for t in traces],chunksize=3))
            assert all(r['audit']['success'] for r in rows)
            dump(dest,rows);print(dict(completed=name,traces=len(rows),summary=summarize_group(rows)),flush=True)
    dump(OUT/'development-execution.json',dict(finished=now(),wall_seconds=perf_counter()-start,workers=workers,
        scope='new development only',note='wall includes waiting for checkpoints, CPU training costs recorded separately'))
    s=summary()
    if 8192 in budgets:gate()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--workers',type=int,default=2);p.add_argument('--extended',action='store_true')
    a=p.parse_args();run(a.workers,(32768,) if a.extended else (0,512,2048,8192))
