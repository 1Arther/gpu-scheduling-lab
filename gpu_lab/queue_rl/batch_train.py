"""Equal training/tuning budgets for all three variants."""
from concurrent.futures import ProcessPoolExecutor
from itertools import product
import argparse
from pathlib import Path
from .experiment import DEFAULT,read

def worker(task):
    from .train import train
    train(*task)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,default=DEFAULT);p.add_argument('--workers',type=int,default=3)
    a=p.parse_args();cfg=read(a.out/'protocol.json')['training']
    tasks=[(a.out,v,idle,seed,cfg['steps_per_model']) for v,idle,seed in product(cfg['variants'],cfg['idle_weights'],cfg['seeds'])
        if not (a.out/'training'/f'{v}-idle{idle:g}-seed{seed}'/'metadata.json').exists()]
    with ProcessPoolExecutor(max_workers=a.workers) as pool:list(pool.map(worker,tasks))
