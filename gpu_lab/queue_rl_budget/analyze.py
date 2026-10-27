"""Paired development analysis; workload blocks and training seeds kept distinct."""
from collections import defaultdict,Counter
import numpy as np
from .common import OUT,SEEDS,METRICS,read,dump,csv_write,finite_mean,now
from .evaluate import summarize_group,promotion

def paired_ci(rows,reference,metric,replicates=10000):
    ref={(r['seed'],r['condition']):r for r in reference};blocks=defaultdict(list)
    for r in rows:
        a,b=r['metrics'].get(metric),ref[r['seed'],r['condition']]['metrics'].get(metric)
        if a is not None and b is not None:blocks[r['seed']].append(a-b)
    values=np.array([np.mean(v) for _,v in sorted(blocks.items())]);rng=np.random.default_rng(92931)
    samples=values[rng.integers(len(values),size=(replicates,len(values)))].mean(axis=1)
    return dict(delta=float(values.mean()),low=float(np.quantile(samples,.025)),high=float(np.quantile(samples,.975)),
                workload_blocks=len(values),block_sd=float(values.std(ddof=1)))

def analyze():
    groups={p.stem:read(p) for p in sorted((OUT/'development').glob('*.json'))}
    summaries={k:summarize_group(v) for k,v in groups.items()}
    budgets=sorted({r['budget'] for v in groups.values() for r in v if r['budget'] is not None})
    aggregate={};stability=[];training=[];update_stats=[]
    for b in budgets:
        names=[f'ppo-{b}-seed{s}' for s in SEEDS]
        assert all(n in groups for n in names)
        pooled=[]
        for i in range(len(groups[names[0]])):
            rows=[groups[n][i] for n in names];first=rows[0]
            assert len({(r['seed'],r['condition']) for r in rows})==1
            pooled.append(dict(seed=first['seed'],condition=first['condition'],
                metrics={m:finite_mean(r['metrics'].get(m) for r in rows) for m in summaries[names[0]] if m not in ('action_counts','legal_counts')}))
        aggregate[f'ppo-{b}-seedmean']=pooled
        for m in METRICS+('conditional_protect','policy_entropy','protect_probability'):
            vals=[summaries[n].get(m) for n in names];v=[x for x in vals if x is not None]
            if not v:continue
            stability.append(dict(budget=b,metric=m,mean=float(np.mean(v)),sd=float(np.std(v,ddof=1)),
                minimum=float(min(v)),maximum=float(max(v)),values=str(vals),n_training_seeds=len(v)))
        for seed in SEEDS:
            suffix='-full32768' if b==32768 else ''
            training.append(read(OUT/f'training/seed-{seed}{suffix}/metadata-{b}.json'))
    for p in sorted((OUT/'training').glob('*/updates.json')):
        updates=read(p)
        for u in updates:update_stats.append(dict(model=p.parent.name,**u))
    paired=[]
    for name,rows in {**groups,**aggregate}.items():
        if not name.startswith('ppo-'):continue
        for comparator in ('hrrn','hrrn_easy','hrrn_protect','hrrn_adaptive_heuristic'):
            for metric in METRICS[:11]:
                paired.append(dict(model=name,comparator=comparator,metric=metric,**paired_ci(rows,groups[comparator],metric)))
    conditions=[]
    for name,rows in {**groups,**aggregate}.items():
        for condition in sorted({r['condition'] for r in rows}):
            rr=[r for r in rows if r['condition']==condition]
            conditions.append(dict(model=name,condition=condition,**{m:finite_mean(r['metrics'].get(m) for r in rr) for m in METRICS}))
    flat=[dict(model=k,workload_seed=r['seed'],condition=r['condition'],training_seed=r['training_seed'],budget=r['budget'],**r['metrics']) for k,v in groups.items() for r in v]
    dump(OUT/'analysis/summary.json',summaries);dump(OUT/'analysis/stability.json',stability);dump(OUT/'analysis/training.json',training)
    dump(OUT/'analysis/paired.json',paired);dump(OUT/'analysis/conditions.json',conditions)
    for name,rows in (('summary',[dict(model=k,**v) for k,v in summaries.items()]),('training',training),('stability',stability),
        ('paired',paired),('conditions',conditions),('updates',update_stats),('per-trace',flat)):
        csv_write(OUT/f'analysis/{name}.csv',rows)
    audits=[r['audit'] for v in groups.values() for r in v]
    reference={(r['seed'],r['condition']):r for r in groups['hrrn']};equivalence=[]
    for name,rr in groups.items():
        if name.startswith('ppo-') and not sum(sum(r['action_statistics']['chosen'][1:]) for r in rr):
            identical=all(r['jobs']==reference[r['seed'],r['condition']]['jobs'] for r in rr)
            assert identical,'No-protection actions should reproduce HRRN job outcomes exactly'
            equivalence.append(dict(model=name,traces=len(rr),all_job_records_exactly_equal_to_hrrn=identical))
    dump(OUT/'analysis/no-protection-equivalence.json',equivalence)
    reasons=Counter()
    for name,v in groups.items():
        if name.startswith('ppo-'):
            for r in v:reasons.update(r['reason_counts'])
    dump(OUT/'analysis/verification.json',dict(time=now(),runs=len(audits),all_audits_passed=all(a['success'] for a in audits),
        promotions={str(b):promotion(summaries,b) for b in budgets if b>=512},
        ppo_inference_reasons=dict(reasons),
        final_test_executed=False,scope='Exploratory new development; no retuning on original final test'))
    print(dict(runs=len(audits),budgets=budgets,passed=all(a['success'] for a in audits)),flush=True)

if __name__=='__main__':analyze()
