"""Gymnasium environment: one complete finite trace per episode."""
from dataclasses import replace
import gymnasium as gym
from gymnasium import spaces
import numpy as np
from ..queue_sim.placement import Cluster
from ..queue_study.placement import MemoizedPlacement
from ..queue_study.metrics import extended_metrics
from .simulator import DecisionSimulator
from .actions import masks
from .observations import Scales,encode
from .rewards import RewardSpec
from .workloads import restore

class QueueProtectionEnv(gym.Env):
    metadata={'render_modes':[]}
    def __init__(self,traces,scales=None,reward_spec=None,variant='joint',service=None):
        super().__init__()
        if not traces:raise ValueError('Nonempty trace set required')
        self.traces=traces;self.scales=scales or Scales.fit(traces)
        self.reward_spec=reward_spec or RewardSpec(job_scale=self.scales.jobs,time_scale=self.scales.time)
        self.variant=variant;self.placement=MemoizedPlacement(service=service)
        self.action_space=spaces.Discrete(4)
        self.observation_space=spaces.Box(-100.,100.,shape=encode(None,self.scales).shape,dtype=np.float32)
        self.sim=None;self.episode_records=[];self.last_cost=0.;self.trace_index=None

    def reset(self,*,seed=None,options=None):
        super().reset(seed=seed)
        index=(options or {}).get('trace_index',int(self.np_random.integers(len(self.traces))))
        self.trace_index=int(index);f,self.jobs,av,cfg=restore(self.traces[index]);self.fleet=f
        self.sim=DecisionSimulator(Cluster(f),self.jobs,self.placement,config=cfg,availability=av,
            reward_spec=self.reward_spec,variant=self.variant)
        self.sim.advance();self.last_cost=0. # Include action-independent prefix in first reward.
        return encode(self.sim.context,self.scales,self.variant),{'no_decision_episode':self.sim.done}

    def action_masks(self):return np.asarray(masks(self.sim.context,self.variant),dtype=bool)

    def step(self,action):
        if self.sim is None:raise RuntimeError('reset first')
        if not self.sim.done:self.sim.advance(int(action))
        reward=-(self.sim.cost-self.last_cost);self.last_cost=self.sim.cost
        info={'cost_parts':dict(self.sim.cost_parts),'dummy_transition':not self.sim.decisions and self.sim.done}
        if self.sim.done:
            m=extended_metrics(self.sim.result,self.jobs,self.fleet,self.sim.planning_diagnostics)
            # Gym deterministic replay includes info; wall-clock measurements
            # belong to the external evaluator, not the deterministic API.
            m={k:v for k,v in m.items() if not k.startswith('decision_ms')}
            record=dict(trace_index=self.trace_index,reward=-self.sim.cost,metrics=m,
                actions=[d['action'] for d in self.sim.decisions],masks=[d['mask'] for d in self.sim.decisions],
                cost_parts=dict(self.sim.cost_parts))
            self.episode_records.append(record);info['episode_result']=record
        return encode(self.sim.context,self.scales,self.variant),float(reward),self.sim.done,False,info
