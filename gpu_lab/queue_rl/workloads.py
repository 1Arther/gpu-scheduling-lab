"""Independent stage-3 namespace. All service times are synthetic."""
from dataclasses import dataclass, asdict
from hashlib import sha256
import math
import random
from ..profiles import load_profiles
from ..resources import GPUState, ResourceRequest
from ..queue_sim.models import Job, Config, job_record, job_from_record

NAMESPACE = 'hrrn-rl-stage3-20261026-v1'
TRAIN_SEEDS = tuple(3100001 + 7919*i for i in range(24))
DEV_SEEDS = (4100021, 4108039, 4116047, 4124059)
TEST_SEEDS = tuple(7100009 + 104729*i for i in range(20))

@dataclass(frozen=True)
class Condition:
    scenario: str
    correlation: str = 'independent'
    error: str = 'noisy'
    @property
    def name(self): return f'{self.scenario}__{self.correlation}__{self.error}'

SCENARIOS = ('steady','bursty','high_load','large_heavy','multi_heavy','memory_heavy',
             'core_heavy','small_heavy','low_load','overload','compatibility')
CONDITIONS = tuple(Condition(s, 'independent' if s=='high_load' else ('independent','positive','negative')[i%3])
                   for i,s in enumerate(SCENARIOS)) + tuple(
    Condition('high_load','independent',e) for e in ('nominal','under','over'))

def generate(seed, condition, count=24, gpu_count=4):
    c=condition
    def stream(purpose):
        key=f'{NAMESPACE}|{seed}|{c.scenario}|{c.correlation}|{purpose}'
        return random.Random(int.from_bytes(sha256(key.encode()).digest()[:8],'big'))
    rng=stream('physical'); erng=stream('estimates'); srng=stream('source_metadata'); p=load_profiles(); fleet={}
    if gpu_count not in (4,8): raise ValueError('Protocol supports 4 or 8 GPUs')
    for n in range(gpu_count//2):
        for i in range(2):
            key=f'rl-node-{n}/gpu-{i}'
            fleet[key]=GPUState(key,p['rtx4090' if n%2==0 else 'l40'],max_instances=10)
    minimum=min(g.profile.modeled_memory_mib for g in fleet.values()); raw=[]
    big_p=.7 if c.scenario=='large_heavy' else .1 if c.scenario=='small_heavy' else .35
    multi_p=.65 if c.scenario=='multi_heavy' else .1 if c.scenario=='small_heavy' else .3
    for i in range(count):
        large=rng.random()<big_p
        level=rng.choice((.65,.8)) if large else rng.choice((.2,.35,.5))
        memory=int(minimum*level*rng.choice((.75,1.)))
        core=max(1,round(100*level*rng.choice((.75,1.))))
        if c.scenario=='memory_heavy': memory=int(minimum*rng.uniform(.65,.9));core=rng.choice((10,20,30))
        if c.scenario=='core_heavy': core=rng.choice((65,80,95));memory=int(minimum*rng.uniform(.1,.3))
        slots=rng.choice((1,2,3,5 if large else 2)); multi=2 if rng.random()<multi_p else 1
        intensity=max(memory/minimum,core/100,slots/10)
        duration=(1+8*intensity if c.correlation=='positive' else
                  1+8*(1-intensity) if c.correlation=='negative' else rng.uniform(1,8))
        if c.correlation!='independent': duration*=rng.uniform(.7,1.3)
        duration=round(duration,4)
        noise=math.exp(erng.gauss(0,.65))
        factor={'nominal':1.,'under':.5,'over':2.,'noisy':noise}[c.error]
        allowed=(rng.choice(('rtx4090','l40')),) if c.scenario=='compatibility' and rng.random()<.65 else ()
        raw.append(dict(job_id=f'rl-{i:03d}',task_type=rng.choice(('training','inference')),
            request=ResourceRequest(memory,core,slots),gpu_count=multi,allowed_profiles=allowed,
            actual_service=duration,estimated_service=max(.2,round(duration*factor,4)),
            # Observable estimator provenance is independent of the hidden
            # experimental error regime. Never label a Job as under/overestimated.
            estimate_source=srng.choice(('synthetic_submitted','synthetic_history','synthetic_external'))))
    rho={'low_load':.2,'high_load':1.6,'overload':1.15}.get(c.scenario,.85)
    # Physical arrivals depend on actual synthetic service only inside the generator,
    # never inside a scheduler. Shared physical trace across estimate-error variants.
    horizon=sum(j['gpu_count']*j['request'].compute_units/100*j['actual_service'] for j in raw)/(gpu_count*rho)
    arrivals=[]
    for i in range(count):
        if c.scenario=='bursty': a=rng.choice((0.,.35,.7))*horizon+rng.uniform(0,.06*horizon)
        elif c.scenario=='overload': a=rng.uniform(0,.12*horizon) if i<count*3//4 else rng.uniform(.5*horizon,horizon)
        else: a=rng.uniform(0,horizon)
        arrivals.append(a)
    origin=min(arrivals); jobs=[]
    for d,a in zip(raw,arrivals):
        arrival=round(a-origin,4);ready=round(arrival+rng.uniform(0,.2),4)
        jobs.append(Job(arrival_at=arrival,ready_at=ready,**d))
    cfg=Config(protection_absolute_wait=2.,protection_kappa=1.,evaluation_long_wait=8.,observation_end=round(horizon+4,4))
    return fleet,tuple(jobs),(),cfg

def record(seed,condition,count=24,gpu_count=4):
    fleet,jobs,_,cfg=generate(seed,condition,count,gpu_count)
    return dict(namespace=NAMESPACE,seed=seed,condition=asdict(condition),count=count,gpu_count=gpu_count,
        fleet={k:dict(profile=g.profile.profile_id,max_instances=g.max_instances) for k,g in fleet.items()},
        jobs=[job_record(j) for j in jobs],config=asdict(cfg))

def restore(row):
    p=load_profiles()
    fleet={k:GPUState(k,p[v['profile']],max_instances=v['max_instances']) for k,v in row['fleet'].items()}
    return fleet,tuple(job_from_record(j) for j in row['jobs']),(),Config(**row['config'])
