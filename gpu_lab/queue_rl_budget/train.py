"""Unchanged MaskablePPO algorithm with read-only rollout/update telemetry."""
import argparse
import csv
from time import perf_counter, process_time
import numpy as np
import torch
from sb3_contrib import MaskablePPO
from stable_baselines3.common.callbacks import BaseCallback
from stable_baselines3.common.logger import configure
from .common import OUT,read,dump,digest,now,verify_inputs
from ..queue_rl.env import QueueProtectionEnv
from ..queue_rl.observations import Scales
from ..queue_rl.rewards import RewardSpec

class Episodes(BaseCallback):
    def __init__(self):super().__init__();self.records=[]
    def _on_step(self):
        for i in self.locals['infos']:
            if 'episode_result' in i:self.records.append(dict(timestep=self.num_timesteps,**i['episode_result']))
        return True

class InstrumentedPPO(MaskablePPO):
    """Diagnostics never change the optimizer, reward, actions or observations."""
    def initialize_telemetry(self,target):
        self.telemetry_target=target;self.update_records=[];self.step_records=[];self.observation_records=[]

    def train(self):
        b=self.rollout_buffer
        obs=b.observations.reshape(-1,*self.observation_space.shape).copy()
        masks=b.action_masks.reshape(-1,4).copy().astype(bool)
        actions=b.actions.reshape(-1).astype(int);adv=b.advantages.reshape(-1).copy()
        with torch.no_grad():
            dist=self.policy.get_distribution(torch.as_tensor(obs,device=self.device),action_masks=masks)
            probs=dist.distribution.probs.cpu().numpy();entropy=dist.entropy().cpu().numpy()
            before=torch.cat([p.detach().reshape(-1) for p in self.policy.parameters()]).clone()
        n=len(actions);offset=self.num_timesteps-n
        for i in range(n):
            self.step_records.append(dict(step=offset+i+1,action=int(actions[i]),effective=bool(masks[i,1:].any()),
                dummy=bool(not masks[i,1:].any()),episode_start=bool(b.episode_starts.reshape(-1)[i]),
                entropy=float(entropy[i]),advantage=float(adv[i]),value=float(b.values.reshape(-1)[i]),
                returns=float(b.returns.reshape(-1)[i]),reward=float(b.rewards.reshape(-1)[i]),
                sampled_log_probability=float(b.log_probs.reshape(-1)[i]),
                **{f'legal_{k}':bool(masks[i,k]) for k in range(4)},
                **{f'prob_{k}':float(probs[i,k]) for k in range(4)}))
        self.observation_records.append(obs)
        super().train()
        after=torch.cat([p.detach().reshape(-1) for p in self.policy.parameters()])
        logs={k:float(v) for k,v in self.logger.name_to_value.items() if k.startswith('train/') and np.isscalar(v)}
        logs={k:(v if np.isfinite(v) else None) for k,v in logs.items()}
        self.update_records.append(dict(step=self.num_timesteps,entropy=float(np.mean(entropy)),
            effective=int(masks[:,1:].any(axis=1).sum()),chosen=np.bincount(actions,minlength=4).tolist(),
            advantage_mean=float(adv.mean()),advantage_std=float(adv.std()),advantage_min=float(adv.min()),advantage_max=float(adv.max()),
            parameter_delta_norm=float(torch.linalg.vector_norm(after-before)),all_finite=bool(np.isfinite(adv).all() and torch.isfinite(after).all()),**logs))

    def _excluded_save_params(self):
        return super()._excluded_save_params()+['telemetry_target','update_records','step_records','observation_records']

def checkpoint(model,callback,target,steps,start,cpu_start):
    # Called AFTER the update at this budget, unlike a callback inside collect_rollouts.
    model.save(target/f'model-{steps}')
    rows=model.step_records
    with (target/'steps.csv').open('w',encoding='utf-8',newline='') as f:
        if rows:
            w=csv.DictWriter(f,fieldnames=rows[0]);w.writeheader();w.writerows(rows)
    dump(target/'updates.json',model.update_records);dump(target/'episodes.json',callback.records)
    np.savez_compressed(target/'observations.npz',observations=np.concatenate(model.observation_records) if model.observation_records else np.empty((0,202)))
    info=dict(seed=model.seed,steps=steps,episodes=len(callback.records),effective_decisions=sum(r['effective'] for r in rows),
        dummy_transitions=sum(r['dummy'] for r in rows),action_counts=[sum(r['action']==k for r in rows) for k in range(4)],
        mean_entropy=float(np.mean([r['entropy'] for r in rows])) if rows else None,
        wall_seconds=perf_counter()-start,cpu_seconds=process_time()-cpu_start,
        sha256=digest(target/f'model-{steps}.zip'),saved_at=now(),completed_after_update=True)
    dump(target/f'metadata-{steps}.json',info)
    print(info,flush=True)

def train(seed,maximum=8192):
    verify_inputs();torch.set_num_threads(1)
    suffix='-full32768' if maximum==32768 else ''
    target=OUT/f'training/seed-{seed}{suffix}';target.mkdir(parents=True,exist_ok=True)
    if (target/'metadata-0.json').exists():raise RuntimeError('Refuse to replace training artifacts')
    scales=Scales(**read(OUT/'scales.json'));reward=RewardSpec(idle=1.,job_scale=scales.jobs,time_scale=scales.time)
    env=QueueProtectionEnv(read(OUT/'inputs/train.json'),scales,reward,'joint')
    model=InstrumentedPPO('MlpPolicy',env,learning_rate=3e-4,n_steps=64,batch_size=32,n_epochs=6,
        gamma=1.,gae_lambda=1.,ent_coef=.01,policy_kwargs=dict(net_arch=[64,64]),seed=seed,device='cpu',verbose=0)
    model.set_logger(configure(str(target),['csv']));model.initialize_telemetry(target)
    callback=Episodes();start=perf_counter();cpu_start=process_time()
    checkpoint(model,callback,target,0,start,cpu_start)
    previous=0
    for budget in (512,2048,8192,32768):
        if budget>maximum:break
        model.learn(total_timesteps=budget-previous,reset_num_timesteps=False,callback=callback)
        assert model.num_timesteps==budget
        checkpoint(model,callback,target,budget,start,cpu_start);previous=budget
    dump(target/'complete.json',dict(completed=maximum,wall_seconds=perf_counter()-start,cpu_seconds=process_time()-cpu_start))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--seed',type=int,required=True);p.add_argument('--maximum',type=int,default=8192)
    a=p.parse_args();train(a.seed,a.maximum)
