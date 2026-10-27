"""New stage-2 namespace, finite synthetic workloads, no phase-one traces."""
from dataclasses import dataclass
from hashlib import sha256
import random,math
from ..profiles import load_profiles
from ..resources import GPUState,ResourceRequest
from ..queue_sim.models import Job,Config

NAMESPACE='hrrn-stage2-20261026-v1'
DEV_SEEDS=(1103,2207,3301,4409)
TEST_SEEDS=tuple(900001+104729*i for i in range(20))


@dataclass(frozen=True)
class Condition:
    scenario:str
    correlation:str='independent'
    error:str='noisy'

    @property
    def name(self):return f'{self.scenario}__{self.correlation}__{self.error}'


SCENARIOS=('steady','bursty','large_heavy','small_heavy','multi_heavy','low_load','high_load','overload')
CORRELATIONS=('independent','positive','negative')
ERRORS=('nominal','under','over','noisy')
TEST_CONDITIONS=tuple(Condition(s,c,'noisy') for s in SCENARIOS for c in CORRELATIONS)+tuple(
    Condition('high_load',c,e) for c in CORRELATIONS for e in ('nominal','under','over'))
DEV_CONDITIONS=tuple(Condition(s,CORRELATIONS[i%3],'noisy' if i%2 else 'under') for i,s in enumerate(SCENARIOS))


def generator_seed(seed,scenario,correlation,purpose):
    return int.from_bytes(sha256(f'{NAMESPACE}|{seed}|{scenario}|{correlation}|{purpose}'.encode()).digest()[:8],'big')


def generate(seed,condition,count=24):
    rng=random.Random(generator_seed(seed,condition.scenario,condition.correlation,'base'))
    erng=random.Random(generator_seed(seed,condition.scenario,condition.correlation,'estimates'))
    profiles=load_profiles();fleet={}
    for node,p in (('stage2-a','rtx4090'),('stage2-b','l40')):
        for i in range(2):
            k=f'{node}/gpu-{i}';fleet[k]=GPUState(k,profiles[p],max_instances=10)
    minimum=min(g.profile.modeled_memory_mib for g in fleet.values());jobs=[]
    s=condition.scenario
    big_probability=.7 if s=='large_heavy' else .1 if s=='small_heavy' else .35
    multi_probability=.65 if s=='multi_heavy' else .1 if s=='small_heavy' else .25
    for i in range(count):
        large=rng.random()<big_probability
        level=rng.choice((.65,.8)) if large else rng.choice((.2,.35,.5))
        core=round(100*level*rng.choice((.75,1.)))
        memory=int(minimum*level*rng.choice((.75,1.)))
        slots=rng.choice((1,2,3,5 if large else 2))
        gpu_count=2 if rng.random()<multi_probability else 1
        intensity=max(memory/minimum,core/100,slots/10)
        if condition.correlation=='positive':duration=(1+8*intensity)*rng.uniform(.7,1.3)
        elif condition.correlation=='negative':duration=(1+8*(1-intensity))*rng.uniform(.7,1.3)
        else:duration=rng.uniform(1,8)
        duration=round(duration,4)
        factor={'nominal':1.,'under':.5,'over':2.,'noisy':math.exp(erng.gauss(0,.65))}[condition.error]
        estimate=max(.2,round(duration*factor,4))
        jobs.append(dict(job_id=f'phase2-{i:03d}',task_type=rng.choice(('training','inference')),
            estimated_service=estimate,actual_service=duration,request=ResourceRequest(memory,core,slots),gpu_count=gpu_count,
            estimate_source=f'synthetic_{condition.error}_estimate'))
    rho={'low_load':.2,'high_load':1.6,'overload':1.15}.get(s,.8)
    horizon=sum(j['gpu_count']*j['request'].compute_units/100*j['actual_service'] for j in jobs)/(4*rho)
    arrivals=[]
    for i in range(count):
        if s=='bursty':a=rng.choice((0.,.35,.7))*horizon+rng.uniform(0,.06*horizon)
        elif s=='overload':a=rng.uniform(0,.12*horizon) if i<count*3//4 else rng.uniform(.5*horizon,horizon)
        else:a=rng.uniform(0,horizon)
        arrivals.append(a)
    origin=min(arrivals)
    result=[]
    for d,a in zip(jobs,arrivals):
        arrival=round(a-origin,4);ready=round(arrival+rng.uniform(0,.2),4)
        result.append(Job(arrival_at=arrival,ready_at=ready,**d))
    # Config window independent of estimates and policy; full trace then drains.
    config=Config(evaluation_long_wait=8.,observation_end=round(horizon+4.,4))
    return fleet,tuple(result),(),config
