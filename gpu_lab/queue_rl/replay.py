"""Reconstruct full audited physical snapshots from frozen compact evidence."""
import argparse,json
from pathlib import Path
import numpy as np
from .experiment import DEFAULT,read,dump,verify_freeze,evaluate_trace
from .observations import Scales
from .policies import HeuristicSpec
from ..queue_study.placement import MemoizedPlacement

def replay(out,seed,condition,policy,save=None):
    import torch
    torch.set_num_threads(1);out=Path(out);verify_freeze(out)
    trace=next(t for t in read(out/'inputs/test.json') if t['seed']==seed and '__'.join(t['condition'].values())==condition)
    expected=next(r for r in read(out/'test/results.json') if r['seed']==seed and r['condition']==condition and r['policy']==policy)
    sel=read(out/'selection.json');variant='switch_only' if policy=='ppo_switch_only' else 'no_hrrn' if policy=='ppo_no_hrrn' else 'joint'
    model=None
    if 'ppo' in policy:
        from sb3_contrib import MaskablePPO
        model=MaskablePPO.load(out/sel['models'][variant]['path'],device='cpu')
    actual,result=evaluate_trace(trace,policy,MemoizedPlacement(),Scales(**read(out/'scales.json')),
        model,variant,HeuristicSpec(**sel['heuristic']['spec']))
    # Wall-clock quantities are intentionally excluded from deterministic replay.
    excluded={'simulation_wall_seconds','inference_ms_mean','inference_ms_p95','decision_ms_mean','decision_ms_p95','dispatch_total_ms_mean'}
    for key,value in expected['metrics'].items():
        if key in excluded:continue
        assert actual['metrics'][key]==value,(key,actual['metrics'][key],value)
    assert json.loads(json.dumps(actual['actions']))==expected['actions']
    if save:dump(save,result)
    print(dict(success=True,seed=seed,condition=condition,policy=policy,audit=actual['audit']))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,default=DEFAULT);p.add_argument('--seed',type=int,required=True)
    p.add_argument('--condition',required=True);p.add_argument('--policy',default='hrrn_ppo');p.add_argument('--save',type=Path)
    a=p.parse_args();replay(a.out,a.seed,a.condition,a.policy,a.save)
