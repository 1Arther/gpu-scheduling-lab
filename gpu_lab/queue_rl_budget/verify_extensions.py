"""Check independently repeated training prefixes without relying on ZIP timestamps."""
import numpy as np
import torch
from sb3_contrib import MaskablePPO
from .common import OUT,SEEDS,read,dump,now

def same(a,b):
    if isinstance(a,torch.Tensor):return torch.equal(a,b)
    if isinstance(a,np.ndarray):return np.array_equal(a,b)
    if isinstance(a,dict):return a.keys()==b.keys() and all(same(a[k],b[k]) for k in a)
    if isinstance(a,(list,tuple)):return len(a)==len(b) and all(same(x,y) for x,y in zip(a,b))
    return a==b

def run():
    torch.set_num_threads(1);rows=[]
    for seed in SEEDS:
        for budget in (0,512,2048,8192):
            a=MaskablePPO.load(OUT/f'training/seed-{seed}/model-{budget}.zip',device='cpu')
            b=MaskablePPO.load(OUT/f'training/seed-{seed}-full32768/model-{budget}.zip',device='cpu')
            checks=dict(policy_bit_exact=same(a.policy.state_dict(),b.policy.state_dict()),
                optimizer_bit_exact=same(a.policy.optimizer.state_dict(),b.policy.optimizer.state_dict()),
                last_observation_bit_exact=same(a._last_obs,b._last_obs))
            assert all(checks.values()),(seed,budget,checks)
            rows.append(dict(seed=seed,budget=budget,**checks))
    dump(OUT/'extension-prefix-verification.json',dict(time=now(),checks=rows,all_bit_exact=True))
    print('All repeated prefix parameters, optimizer states and last observations are bit exact',flush=True)

if __name__=='__main__':run()
