"""Paired common-trace evaluation. Final test refuses a missing/changed freeze."""
import argparse
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from datetime import datetime,timezone
from itertools import product
from pathlib import Path
from time import perf_counter
import numpy as np
from .experiment import DEFAULT,BASE_POLICIES,read,dump,evaluate_trace,objective,verify_freeze
from .observations import Scales
from .policies import HeuristicSpec
from .workloads import record,Condition,DEV_SEEDS

_state={}
def worker(task):
    out,trace,policy,variant,hspec,path=task;out=Path(out)
    if not _state:
        import torch
        torch.set_num_threads(1)
        from ..queue_study.placement import MemoizedPlacement
        _state['placement']=MemoizedPlacement();_state['scales']=Scales(**read(out/'scales.json'));_state['models']={}
    model=None
    if path:
        if path not in _state['models']:
            from sb3_contrib import MaskablePPO
            _state['models'][path]=MaskablePPO.load(out/path,device='cpu')
        model=_state['models'][path]
    row,result=evaluate_trace(trace,policy,_state['placement'],_state['scales'],model,variant,
        HeuristicSpec(**hspec) if hspec else None,path)
    # Full state audit executes before compact evidence is written. Save enough
    # event evidence to independently rerun every selected model/trace.
    row['physical_events']=[{k:e[k] for k in ('time','kind','job_id','plan','previous','reason') if k in e}
        for e in result['events'] if e['kind'] in ('start','complete','protection_create','protection_clear','estimate_update')]
    return row

def run_tasks(tasks,workers=3):
    with ProcessPoolExecutor(max_workers=workers) as pool:return list(pool.map(worker,tasks,chunksize=4))

def development(out=DEFAULT,workers=3):
    out=Path(out);traces=read(out/'inputs/development.json');start=perf_counter()
    tasks=[(str(out),t,p,'joint',None,None) for p in BASE_POLICIES for t in traces]
    baseline=run_tasks(tasks,workers);dump(out/'development/baselines.json',baseline)
    candidates=[]
    for wait,near,blocked in product((2.,6.),(1.,3.),(0,2)):
        spec=asdict(HeuristicSpec(wait,near,blocked,.25))
        rows=run_tasks([(str(out),t,'hrrn_adaptive_heuristic','joint',spec,None) for t in traces],workers)
        name=f'w{wait:g}-d{near:g}-b{blocked}'
        dump(out/f'development/heuristic-{name}.json',rows)
        candidates.append(dict(id=name,spec=spec,objective=objective(rows)))
    best_h=min(candidates,key=lambda x:(x['objective'],x['id']))
    dump(out/'development/heuristic-selection.json',dict(candidates=candidates,selected=best_h))
    models=[]
    for metadata in sorted((out/'training').glob('*/metadata.json')):
        meta=read(metadata);path=str((metadata.parent/'model.zip').relative_to(out)).replace('\\','/')
        rows=run_tasks([(str(out),t,'hrrn_ppo',meta['variant'],best_h['spec'],path) for t in traces],workers)
        dump(out/f"development/{meta['model_id']}.json",rows)
        models.append(dict(id=meta['model_id'],variant=meta['variant'],objective=objective(rows),
            path=path,metadata=str(metadata.relative_to(out)).replace('\\','/'),sha256=meta['model_sha256'],
            mean_work=float(np.mean([r['metrics']['completed_work'] for r in rows]))))
    selected={v:min((m for m in models if m['variant']==v),key=lambda m:(m['objective'],-m['mean_work'],m['id']))
              for v in ('joint','switch_only','no_hrrn')}
    dump(out/'selection.json',dict(heuristic=best_h,models=selected,all_model_candidates=models,
        selection_scope='development only; no final test statistics read',wall_seconds=perf_counter()-start))
    # Diagnostics only: no extra checkpoint selection or test retuning. All three
    # variants get the same 0/256/512 learning-curve assessment.
    from sb3_contrib import MaskablePPO
    from .env import QueueProtectionEnv
    from .rewards import RewardSpec
    import torch
    torch.set_num_threads(1);diagnostics=[]
    for variant,m in selected.items():
        meta=read(out/m['metadata']);scales=Scales(**meta['scales'])
        env=QueueProtectionEnv(read(out/'inputs/train.json'),scales,RewardSpec(**meta['reward']),variant)
        initial=MaskablePPO('MlpPolicy',env,learning_rate=3e-4,n_steps=64,batch_size=32,n_epochs=6,
            gamma=1.,gae_lambda=1.,ent_coef=.01,policy_kwargs=dict(net_arch=[64,64]),seed=meta['seed'],device='cpu')
        initial_path=f'development/untrained-{variant}.zip';initial.save(out/initial_path)
        for step,path in ((0,initial_path),(256,str((Path(m['path']).parent/'checkpoint-256.zip')).replace('\\','/')),(512,m['path'])):
            rr=run_tasks([(str(out),t,'hrrn_ppo',variant,best_h['spec'],path) for t in traces],workers)
            dump(out/f'development/curve-{variant}-{step}.json',rr)
            diagnostics.append(dict(variant=variant,steps=step,objective=objective(rr),
                p95_wait=float(np.mean([r['metrics']['p95_wait'] for r in rr])),
                mean_wait=float(np.mean([r['metrics']['mean_wait'] for r in rr])),
                protect_ratio=float(np.mean([r['metrics']['protect_action_ratio'] for r in rr]))))
    dump(out/'development/learning-diagnostics.json',diagnostics)
    stress=[]
    for seed in DEV_SEEDS:
        for scenario in ('steady','high_load','multi_heavy'):
            for count,gpu_count in ((24,4),(24,8),(40,4),(40,8)):
                t=record(seed,Condition(scenario),count,gpu_count)
                t['condition']['scenario']+=f'_jobs{count}_gpus{gpu_count}'
                stress.extend([(str(out),t,p,'joint',best_h['spec'],selected['joint']['path'] if p=='hrrn_ppo' else None)
                    for p in ('hrrn','hrrn_easy','hrrn_adaptive_heuristic','hrrn_ppo')])
    dump(out/'development/scaling-stress.json',run_tasks(stress,workers))
    print(dict(selected=selected,heuristic=best_h,wall_seconds=perf_counter()-start))

def final_test(out=DEFAULT,workers=3):
    out=Path(out);verify_freeze(out)
    if (out/'test/results.json').exists():raise RuntimeError('Formal results already exist; refusing overwrite')
    start=perf_counter();started_utc=datetime.now(timezone.utc).isoformat();traces=read(out/'inputs/test.json');sel=read(out/'selection.json');spec=sel['heuristic']['spec']
    tasks=[]
    for t in traces:
        tasks.extend((str(out),t,p,'joint',None,None) for p in BASE_POLICIES)
        tasks.append((str(out),t,'hrrn_adaptive_heuristic','joint',spec,None))
        for variant,m in sel['models'].items():
            policy='hrrn_ppo' if variant=='joint' else 'ppo_'+variant
            tasks.append((str(out),t,policy,variant,spec,m['path']))
    rows=run_tasks(tasks,workers);dump(out/'test/results.json',rows)
    verify_freeze(out)
    dump(out/'test/execution.json',dict(runs=len(rows),traces=len(traces),seed_blocks=20,
        started_at_utc=started_utc,finished_at_utc=datetime.now(timezone.utc).isoformat(),
        wall_seconds=perf_counter()-start,all_audits_pass=all(r['audit']['success'] for r in rows),freeze_verified=True))
    print(read(out/'test/execution.json'))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['development','test']);p.add_argument('--out',type=Path,default=DEFAULT);p.add_argument('--workers',type=int,default=3)
    a=p.parse_args();(development if a.command=='development' else final_test)(a.out,a.workers)
