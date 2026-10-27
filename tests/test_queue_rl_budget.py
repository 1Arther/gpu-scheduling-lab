import numpy as np
import pytest
from gpu_lab.queue_rl_budget.common import NEW_TRAIN,NEW_DEV
from gpu_lab.queue_rl_budget.diagnose import action_stats
from gpu_lab.queue_rl.workloads import TRAIN_SEEDS,DEV_SEEDS,TEST_SEEDS,record,Condition

def test_disjoint_new_seeds():
    assert not set(NEW_TRAIN)&set(NEW_DEV)
    assert not (set(NEW_TRAIN)|set(NEW_DEV))&(set(TRAIN_SEEDS)|set(DEV_SEEDS)|set(TEST_SEEDS))

def test_legal_and_chosen_are_separate():
    s=action_stats([{'mask':[True,True,False,True],'action':0},{'mask':[True,True,True,False],'action':2}])
    assert s['chosen']==[1,0,1,0] and s['legal_counts']==[2,2,1,1]
    assert s['conditional_protection']==.5 and s['c_legal_but_not_selected']==1
    assert action_stats([])['opportunities']==0

def test_telemetry_and_segmented_learning_do_not_change_ppo(tmp_path):
    import torch
    from sb3_contrib import MaskablePPO
    from stable_baselines3.common.logger import configure
    from gpu_lab.queue_rl_budget.train import InstrumentedPPO
    from gpu_lab.queue_rl.env import QueueProtectionEnv
    from gpu_lab.queue_rl.observations import Scales
    from gpu_lab.queue_rl.rewards import RewardSpec
    torch.set_num_threads(1)
    traces=[record(NEW_TRAIN[0],Condition('high_load'),8)]
    kw=dict(learning_rate=3e-4,n_steps=64,batch_size=32,n_epochs=6,gamma=1.,gae_lambda=1.,ent_coef=.01,
            policy_kwargs=dict(net_arch=[64,64]),seed=17,device='cpu')
    def env():return QueueProtectionEnv(traces,Scales(),RewardSpec(idle=1.),'joint')
    ref=MaskablePPO('MlpPolicy',env(),**kw);ref.set_logger(configure(str(tmp_path/'ref'),[]));ref.learn(128)
    actual=InstrumentedPPO('MlpPolicy',env(),**kw);actual.set_logger(configure(str(tmp_path/'actual'),[]));actual.initialize_telemetry(tmp_path)
    actual.learn(64,reset_num_timesteps=False);actual.learn(64,reset_num_timesteps=False)
    assert all(torch.equal(a,b) for a,b in zip(ref.policy.parameters(),actual.policy.parameters()))
    assert len(actual.step_records)==128 and len(actual.update_records)==2
    assert all(u['all_finite'] and u['parameter_delta_norm']>0 for u in actual.update_records)
    assert all(abs(sum(r[f'prob_{k}'] for k in range(4))-1)<1e-5 for r in actual.step_records)
    actual.save(tmp_path/'model');loaded=MaskablePPO.load(tmp_path/'model',device='cpu')
    assert all(torch.equal(a,b) for a,b in zip(actual.policy.parameters(),loaded.policy.parameters()))

@pytest.mark.parametrize('gain,expected',[(0.,False),(.1,True)])
def test_promotion_requires_learning_and_loss_bounds(gain,expected):
    from gpu_lab.queue_rl_budget.evaluate import promotion
    base=dict(p95_wait=10.,small_p95_wait=1.,long_wait_ratio=.1,completed_work=100.)
    s={k:dict(base) for k in ('hrrn','hrrn_easy','hrrn_protect','hrrn_adaptive_heuristic')}
    for seed in (17,43,71):
        for budget,p95 in ((0,12.),(512,11.),(8192,10.*(1-gain))):s[f'ppo-{budget}-seed{seed}']=dict(base,p95_wait=p95)
    assert promotion(s,8192)['passes']==expected
    for seed in (17,43,71):s[f'ppo-8192-seed{seed}']['small_p95_wait']=1.2
    assert not promotion(s,8192)['passes']

def test_paired_ci_uses_workload_blocks():
    from gpu_lab.queue_rl_budget.analyze import paired_ci
    a=[dict(seed=i,condition=c,metrics={'p95_wait':v}) for i,v in ((1,2.),(2,4.)) for c in ('a','b')]
    b=[dict(seed=i,condition=c,metrics={'p95_wait':1.}) for i in (1,2) for c in ('a','b')]
    r=paired_ci(a,b,'p95_wait',100)
    assert r['workload_blocks']==2 and r['delta']==2.
