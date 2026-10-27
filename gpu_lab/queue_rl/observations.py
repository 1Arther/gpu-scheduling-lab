"""Fixed public vector, padded to 8 GPUs, 4 queue jobs, 3 combinations.

Scaling is fit solely to submitted training estimates; physical fractions have
known bounds. HRRN values can be ablated without changing queue ordering.
"""
from dataclasses import dataclass, asdict
import numpy as np

@dataclass(frozen=True)
class Scales:
    time: float=5.
    jobs: float=24.
    @classmethod
    def fit(cls,traces):
        estimates=[j['estimated_service'] for t in traces for j in t['jobs']]
        return cls(max(.1,float(np.median(estimates))),float(max(len(t['jobs']) for t in traces)))

def encode(context,scales,variant='joint'):
    # Stable feature layout and length even for a terminal/no-decision state.
    x=[];c=context
    q=c['queue'] if c else [];o=c['owner'] if c else None
    x.extend((len(q)/scales.jobs,sum(j['overdue'] for j in q)/scales.jobs,
        sum(j['feasible'] for j in q)/scales.jobs,len(c['forecasts'])/scales.jobs if c else 0))
    gs=c['gpus'] if c else []
    mincap=[min(g['capacity'][d] for g in gs) if gs else 1 for d in range(3)]
    for j in ([o]+q[:3]):
        if j is None: x.extend([0.]*16);continue
        r=j['request'];src=j['estimate_source']
        x.extend([1.,j['wait']/scales.time,j['estimated_service']/scales.time,
            0. if variant=='no_hrrn' else j['hrrn']/10,
            r['memory_mib']/mincap[0],r['compute_units']/100,r['instances']/mincap[2],
            j['gpu_count']/8,j['task_type']=='training',j['feasible'],j['overdue'],
            j['trigger']/scales.time,'submitted' in src,'history' in src,'external' in src,bool(j['allowed_profiles'])])
    # Padding when queue has fewer than 3 ordinary candidates.
    x.extend([0.]*(16*(4-(1+len(q[:3])))))
    for i in range(8):
        if i>=len(gs):x.extend([0.]*10);continue
        g=gs[i]
        x.extend([1.,*[a/b for a,b in zip(g['free'],g['capacity'])],g['active_jobs']/10,
            g['profile']=='rtx4090',g['profile']=='l40',g['online'],g['compatible'],g['protected']])
    for i in range(3):
        if not c or i>=len(c['combos']):x.extend([0.]*18);continue
        p=c['combos'][i]
        x.extend([1.,(p['delay'] if p['delay'] is not None else 50)/scales.time,
            *p['deficit'],*p['idle'],p['blocked_count']/scales.jobs,p['rf_score'],
            *[g['gpu_id'] in p['targets'] for g in gs],*[0.]*(8-len(gs))])
    out=np.asarray(x,dtype=np.float32)
    # Public unbounded waiting can exceed the training range; fixed clipping only.
    return np.clip(out,-100,100)
