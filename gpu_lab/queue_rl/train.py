"""Actual mature MaskablePPO CPU training; one process per independent model."""
import argparse
from dataclasses import asdict
from pathlib import Path
from time import perf_counter
import torch
from sb3_contrib import MaskablePPO
from stable_baselines3.common.callbacks import BaseCallback
from stable_baselines3.common.logger import configure
from .env import QueueProtectionEnv
from .observations import Scales
from .rewards import RewardSpec
from .experiment import DEFAULT,read,dump,digest

class Recorder(BaseCallback):
    def __init__(self,out):super().__init__();self.out=out;self.records=[]
    def _on_step(self):
        for info in self.locals['infos']:
            if 'episode_result' in info:self.records.append(dict(timestep=self.num_timesteps,**info['episode_result']))
        if self.num_timesteps%256==0:
            self.model.save(self.out/f'checkpoint-{self.num_timesteps}')
            dump(self.out/'episodes.json',self.records)
        return True

def train(out=DEFAULT,variant='joint',idle=.25,seed=17,steps=512):
    torch.set_num_threads(1);out=Path(out);model_id=f'{variant}-idle{idle:g}-seed{seed}'
    target=out/'training'/model_id
    if (target/'metadata.json').exists():raise RuntimeError('Completed model already exists')
    target.mkdir(parents=True,exist_ok=True)
    scales=Scales(**read(out/'scales.json'));reward=RewardSpec(idle=idle,job_scale=scales.jobs,time_scale=scales.time)
    env=QueueProtectionEnv(read(out/'inputs/train.json'),scales,reward,variant)
    model=MaskablePPO('MlpPolicy',env,learning_rate=3e-4,n_steps=64,batch_size=32,n_epochs=6,
        gamma=1.,gae_lambda=1.,ent_coef=.01,policy_kwargs=dict(net_arch=[64,64]),seed=seed,device='cpu',verbose=0)
    model.set_logger(configure(str(target),['csv']))
    callback=Recorder(target);start=perf_counter();model.learn(total_timesteps=steps,callback=callback)
    wall=perf_counter()-start;model.save(target/'model');dump(target/'episodes.json',callback.records)
    meta=dict(model_id=model_id,variant=variant,seed=seed,steps=model.num_timesteps,wall_seconds=wall,
        reward=asdict(reward),scales=asdict(scales),net_arch=[64,64],n_steps=64,batch_size=32,n_epochs=6,
        learning_rate=3e-4,gamma=1.,gae_lambda=1.,ent_coef=.01,device='cpu',torch_threads=1,
        model_sha256=digest(target/'model.zip'),training_input_sha256=digest(out/'inputs/train.json'),
        episodes=len(callback.records),training_only=True)
    dump(target/'metadata.json',meta);print(meta)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,default=DEFAULT);p.add_argument('--variant',default='joint',choices=['joint','switch_only','no_hrrn'])
    p.add_argument('--idle',type=float,default=.25);p.add_argument('--seed',type=int,default=17);p.add_argument('--steps',type=int,default=512)
    a=p.parse_args();train(a.out,a.variant,a.idle,a.seed,a.steps)
