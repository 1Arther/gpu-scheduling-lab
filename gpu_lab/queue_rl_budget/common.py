"""Protocol, immutable inputs, and shared helpers for the supplementary study."""
import csv
import os
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
from ..queue_rl.experiment import ROOT, DEFAULT as OLD, read, dump, digest, verify_freeze
from ..queue_rl.workloads import record, CONDITIONS, TRAIN_SEEDS, DEV_SEEDS, TEST_SEEDS

OUT = Path(os.environ.get('GPU_QUEUE_BUDGET_OUT',str(ROOT / 'results/hrrn-ppo-budget-20261026'))).resolve()
SEEDS = (17, 43, 71)
NEW_TRAIN = tuple(21000001 + 7919*i for i in range(24))
NEW_DEV = tuple(31000019 + 104729*i for i in range(12))
METRICS = ('p95_wait','p99_wait','mean_wait','long_wait_ratio','large_p95_wait',
           'small_p95_wait','large_mean_wait','small_mean_wait','completed_work',
           'mean_bounded_slowdown','multi_gpu_p95_wait','protect_action_ratio')

def now(): return datetime.now(timezone.utc).isoformat()

def csv_write(path, rows):
    rows=list(rows); path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    if not rows: return
    keys=list(dict.fromkeys(k for r in rows for k in r))
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=keys);w.writeheader();w.writerows(rows)

def finite_mean(values):
    v=[x for x in values if x is not None and np.isfinite(x)]
    return float(np.mean(v)) if v else None

def preserved():
    paths=list((ROOT/'gpu_lab/queue_rl').glob('*.py'))
    paths += [p for p in OLD.rglob('*') if p.is_file() and '__pycache__' not in str(p)]
    return {str(p.relative_to(ROOT)):digest(p) for p in paths}

def prepare():
    if (OUT/'protocol.json').exists(): raise RuntimeError('Refusing to overwrite a prepared study')
    assert not (set(NEW_TRAIN)|set(NEW_DEV)) & (set(TRAIN_SEEDS)|set(DEV_SEEDS)|set(TEST_SEEDS))
    assert not set(NEW_TRAIN)&set(NEW_DEV)
    verify_freeze();dump(OUT/'preserved-before.json',preserved())
    # Keep the old normalization as part of the unchanged model/reward definition.
    dump(OUT/'scales.json',read(OLD/'scales.json'))
    for name,seeds in (('train',NEW_TRAIN),('development',NEW_DEV)):
        dump(OUT/f'inputs/{name}.json',[record(s,c) for s in seeds for c in CONDITIONS])
    protocol=dict(created_at_utc=now(),scope='new training and development only; old test is diagnostic evidence, never a selection set',
        training_seeds=SEEDS,train_trace_seeds=NEW_TRAIN,development_trace_seeds=NEW_DEV,
        budgets=[0,512,2048,8192,32768],variant='joint',reward_idle=1.,
        hyperparameters=dict(net_arch=[64,64],learning_rate=.0003,n_steps=64,batch_size=32,n_epochs=6,gamma=1.,gae_lambda=1.,ent_coef=.01),
        frozen_scales=read(OLD/'scales.json'),generator='unchanged stage3 generator, all new disjoint seeds, identical 14-condition distribution',
        primary='macro all-job P95 waiting, average conditions within workload-seed blocks',
        small_job_bound='small-job P95 <= HRRN small-job P95 + max(0.05, 5% of HRRN small-job P95)',
        long_wait_bound='long-wait ratio <= HRRN + 0.005 (0.5 percentage points); threshold remains 8',
        work_bound='completed synthetic work >= 99% of HRRN',
        promotion='At least 2 of 3 seeds: P95 <= 97% of HRRN and <= 99% of best(EASY,heuristic,fixed); all three bounds pass. Mean P95 improves >= 1% against 512 and >= 3% against untrained.',
        staircase_stop='After 8192, stop before 32768 if mean P95 improves < 1% against 2048 AND fewer than 2/3 seeds improve >= 1% AND promotion fails. Otherwise execute 32768.',
        final_test='Generate and freeze a completely new final test only if promotion passes; otherwise no new test and preserve negative results.',
        uncertainty='Exploratory paired 95% percentile bootstrap over 12 new workload-seed blocks; 10000 replicates RNG 92931; training seed range reported separately, not 36 independent samples.',
        all_durations='synthetic; CPU discrete events only',original_artifacts_immutable=True)
    dump(OUT/'protocol.json',protocol)
    files=[OUT/'protocol.json',OUT/'scales.json',*sorted((OUT/'inputs').glob('*.json'))]
    dump(OUT/'input-freeze.json',dict(time=now(),files={str(p.relative_to(ROOT)):digest(p) for p in files}))
    print(dict(prepared=True,train=24*14,development=12*14),flush=True)

def verify_inputs():
    for p,h in read(OUT/'input-freeze.json')['files'].items():
        assert digest(ROOT/p)==h,p

if __name__=='__main__': prepare()
