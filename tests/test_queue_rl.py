from dataclasses import replace,asdict
import json,time
import numpy as np
import pytest
from gpu_lab.queue_sim import Cluster,Config,Job
from gpu_lab.queue_sim.scenarios import blocking_case,cancellation_case,invalidation_case,invalid_request_case
from gpu_lab.queue_study import StudySimulator
from gpu_lab.queue_study.placement import MemoizedPlacement
from gpu_lab.queue_rl.simulator import DecisionSimulator
from gpu_lab.queue_rl.policies import never,fixed,heuristic,safe_predict
from gpu_lab.queue_rl.actions import masks
from gpu_lab.queue_rl.observations import Scales,encode
from gpu_lab.queue_rl.workloads import TRAIN_SEEDS,DEV_SEEDS,TEST_SEEDS,CONDITIONS,Condition,record,restore
from gpu_lab.queue_rl.audit import verify_rl
from gpu_lab.queue_rl.rewards import RewardSpec,interval_cost

@pytest.fixture(scope='module')
def placement():return MemoizedPlacement()

def sim(factory=blocking_case,placement=None,**kw):
    f,j,a=factory();return DecisionSimulator(Cluster(f),j,placement or MemoizedPlacement(),availability=a,**kw),f,j

@pytest.mark.parametrize('factory',(blocking_case,cancellation_case,invalidation_case,invalid_request_case))
def test_never_matches_original_hrrn(placement,factory):
    s,f,j=sim(factory,placement);r=s.run_policy(never)
    original=StudySimulator(Cluster(f),j,placement,'hrrn',availability=factory()[2]).run()
    assert r['jobs']==original['jobs'] and r['timeline']==original['timeline']
    assert [e for e in r['events'] if e['kind'] in ('start','complete','cancel')]==[e for e in original['events'] if e['kind'] in ('start','complete','cancel')]
    assert verify_rl(r,j,f)['success']

@pytest.mark.parametrize('factory',(blocking_case,cancellation_case,invalidation_case,invalid_request_case))
@pytest.mark.parametrize('policy',(fixed,heuristic))
def test_atomic_capacity_release_and_guard_audit(placement,factory,policy):
    s,f,j=sim(factory,placement);r=s.run_policy(policy)
    assert verify_rl(r,j,f)['success']
    assert all(len(row['gpu_ids'])==row['gpu_count'] for row in r['jobs'] if row['start_at'] is not None)
    assert r['metrics']['all_resources_released'] and r['metrics']['final_protection_cleared']

def test_mask_invalid_and_fixed_target(placement):
    s,f,j=sim(placement=placement);c=s.advance();assert c
    assert masks(c)==[True,True,False,False]
    s.advance(3);assert s.decisions[-1]['action']==0
    while not s.done:s.advance(1)
    assert verify_rl(s.result,j,f)['success']
    for e in s.events:
        if e['kind']=='start' and e['reason']=='protected_owner':
            assert e['plan']['gpu_ids']==tuple(e['protection']['targets'])

def test_seeded_random_policy_reproducible(placement):
    def run(seed):
        rng=np.random.default_rng(seed);s,f,j=sim(placement=placement)
        r=s.run_policy(lambda c,m:(int(rng.choice(np.flatnonzero(m))),'random_legal'))
        assert verify_rl(r,j,f)['success'];return r
    assert run(123)==run(123)

def test_no_future_actual_or_arrivals(placement):
    f,j,a=blocking_case();a_sim=DecisionSimulator(Cluster(f),j,placement);ca=a_sim.advance()
    changed=tuple(replace(x,actual_service=x.actual_service+50) for x in j)
    # Unknown future jobs can be changed/added without affecting the prefix.
    future=replace(j[-1],job_id='unseen',arrival_at=1000,ready_at=1000)
    b_sim=DecisionSimulator(Cluster(f),changed+(future,),placement);cb=b_sim.advance()
    assert ca==cb and np.array_equal(encode(ca,Scales()),encode(cb,Scales()))
    assert 'actual_service' not in json.dumps(ca)
    assert all(x['ready_at']<=ca['time'] for x in ca['queue'])

def test_split_and_error_physical_pairing():
    assert len(set(TRAIN_SEEDS+DEV_SEEDS+TEST_SEEDS))==len(TRAIN_SEEDS+DEV_SEEDS+TEST_SEEDS)
    a=record(TRAIN_SEEDS[0],Condition('high_load',error='under'))
    b=record(TRAIN_SEEDS[0],Condition('high_load',error='over'))
    for x,y in zip(a['jobs'],b['jobs']):
        for field in ('arrival_at','ready_at','actual_service','request','gpu_count'):assert x[field]==y[field]
        assert x['estimate_source']==y['estimate_source']
        assert not any(s in x['estimate_source'] for s in ('under','over','noisy','nominal'))
    assert record(TRAIN_SEEDS[0],Condition('high_load',error='under'))==a

@pytest.mark.parametrize('condition',CONDITIONS,ids=lambda c:c.name)
def test_new_workloads_never_matches_hrrn(placement,condition):
    f,j,av,cfg=restore(record(DEV_SEEDS[0],condition,12))
    a=StudySimulator(Cluster(f),j,placement,'hrrn',cfg,av).run()
    b=DecisionSimulator(Cluster(f),j,placement,config=cfg,availability=av).run_policy(never)
    assert a['jobs']==b['jobs'] and a['timeline']==b['timeline']
    assert verify_rl(b,j,f)['success']

def test_switch_mask_and_hrrn_ablation(placement):
    row=record(DEV_SEEDS[1],Condition('high_load'),12)
    f,j,av,cfg=restore(row);s=DecisionSimulator(Cluster(f),j,placement,config=cfg)
    context=s.advance();assert context
    mask=masks(context,'switch_only');assert mask[0] and not mask[2] and not mask[3]
    a=encode(context,Scales());b=encode(context,Scales(),'no_hrrn')
    changed=np.flatnonzero(a!=b)
    assert set(changed).issubset({7,23,39,55}) and len(changed)>0
    assert np.all(b[[7,23,39,55]]==0)

@pytest.mark.parametrize('scenario',('high_load','multi_heavy','compatibility'))
def test_shared_calendar_equals_independent_projection(placement,scenario):
    from gpu_lab.queue_study.easy import project
    f,j,av,cfg=restore(record(DEV_SEEDS[1],Condition(scenario,error='under'),12))
    s=DecisionSimulator(Cluster(f),j,placement,config=cfg);s.advance()
    while not s.done:
        owner=s.public[s.context['owner']['job_id']]
        before=s.cluster.snapshot()
        for combo in s.context['combos']:
            delay=None
            for when in sorted({s.clock,*(f.expected_end for f in s.forecasts.values())}):
                if project(s.cluster,s.forecasts,when).valid(owner,combo['targets']):delay=when-s.clock;break
            assert combo['delay']==delay
        assert s.cluster.snapshot()==before
        s.advance(0)

@pytest.mark.parametrize('kind',('illegal','exception','timeout'))
def test_safe_inference_fallback(placement,kind):
    class Bad:
        def predict(self,*a,**kw):
            if kind=='exception':raise RuntimeError('failed')
            if kind=='timeout':time.sleep(.005);return 1,None
            return 100,None
    s,f,j=sim(placement=placement);c=s.advance()
    a,reason,elapsed=safe_predict(Bad(),encode(c,Scales()),masks(c),limit_ms=.1 if kind=='timeout' else 100)
    assert a==0 and 'fallback' in reason
    s.advance(a,reason)
    while not s.done:s.advance(0)
    assert verify_rl(s.result,j,f)['success']

def test_backend_provenance(placement,monkeypatch):
    calls=[];original=Cluster.commit
    def commit(self,job,plan,protection=None):
        calls.append(job.job_id)
        assert plan in placement.plans(self,job,protection) or plan.gpu_ids==protection.targets
        return original(self,job,plan,protection)
    monkeypatch.setattr(Cluster,'commit',commit)
    s,f,j=sim(placement=placement);s.run_policy(fixed);assert len(calls)==len(j)

def test_reward_time_integral_additive(placement):
    s,f,j=sim(placement=placement);wait=[j[2].public()];spec=RewardSpec()
    total=interval_cost(wait,s.cluster,None,0,12,8,spec)
    a=interval_cost(wait,s.cluster,None,0,5,8,spec);b=interval_cost(wait,s.cluster,None,5,12,8,spec)
    assert all(total[k]==pytest.approx(a[k]+b[k]) for k in total)

def test_gym_episode_and_train_only_scales(placement):
    from gymnasium.utils.env_checker import check_env
    from gpu_lab.queue_rl.env import QueueProtectionEnv
    traces=[record(TRAIN_SEEDS[0],Condition('overload'),12)]
    env=QueueProtectionEnv(traces,service=placement.service);check_env(env,skip_render_check=True)
    obs,_=env.reset(seed=42);total=0
    while True:
        obs,r,done,_,info=env.step(0);total+=r
        assert env.observation_space.contains(obs)
        if done:break
    assert total==pytest.approx(-env.sim.cost)
    assert verify_rl(env.sim.result,env.jobs,env.fleet)['success']
    assert encode(env.sim.context,env.scales).shape==encode(None,env.scales,'no_hrrn').shape

def test_frozen_ppo_replay(tmp_path,placement):
    from sb3_contrib import MaskablePPO
    from gpu_lab.queue_rl.env import QueueProtectionEnv
    traces=[record(TRAIN_SEEDS[1],Condition('overload'),12)]
    env=QueueProtectionEnv(traces,service=placement.service)
    model=MaskablePPO('MlpPolicy',env,n_steps=8,batch_size=8,n_epochs=1,seed=3,
        policy_kwargs=dict(net_arch=[16,16]),gamma=1.,gae_lambda=1.,device='cpu')
    model.learn(8);model.save(tmp_path/'model')
    loaded=MaskablePPO.load(tmp_path/'model',device='cpu')
    def run(m):
        obs,_=env.reset(seed=9);actions=[]
        while True:
            a,_,_=safe_predict(m,obs,env.action_masks());actions.append(a)
            obs,_,done,_,_=env.step(a)
            if done:return actions,env.sim.result
    assert run(model)==run(loaded)
