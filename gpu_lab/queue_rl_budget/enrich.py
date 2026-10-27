"""Additional old-record accounting; no environment execution or training."""
import csv
import numpy as np
from .common import OUT,OLD,read,dump,csv_write,finite_mean
from .diagnose import action_stats
from ..queue_rl.workloads import restore
from ..queue_study.metrics import size_class

def load_csv(path):
    with path.open(encoding='utf-8-sig') as f:return list(csv.DictReader(f))

def run():
    out=OUT/'diagnosis';training=load_csv(out/'original-training-and-seeds.csv')
    for row in training:
        folder=OLD/'training'/row['model'];episodes=read(folder/'episodes.json')
        actions=[dict(action=a,mask=m) for e in episodes for a,m in zip(e['actions'],e['masks'])]
        stat=action_stats(actions);row['training_actions_completed_episodes']=str(stat['chosen'])
        row['training_legality_completed_episodes']=str(stat['legal_counts'])
        row['exact_effective_inferred']=int(row['steps'])-int(row['completed_no_decision_episodes'])
        logs=load_csv(folder/'progress.csv')
        for metric in ('train/value_loss','train/explained_variance','train/approx_kl','train/entropy_loss'):
            v=[float(x[metric]) for x in logs if x.get(metric)]
            row[metric+'_min']=min(v);row[metric+'_max']=max(v)
    csv_write(out/'original-training-and-seeds.csv',training)
    sizes={}
    for t in read(OLD/'inputs/test.json'):
        fleet,jobs,_,_=restore(t);cond='__'.join(t['condition'].values())
        for j in jobs:sizes[t['seed'],cond,j.job_id]=(size_class(j,fleet),j.gpu_count)
    starts=load_csv(out/'same-job-starts.csv')
    for r in starts:r['size_class'],r['gpu_count']=sizes[int(r['seed']),r['condition'],r['job_id']]
    csv_write(out/'same-job-starts.csv',starts)
    group=[]
    for name in ('small','medium','large','multi_gpu'):
        rr=[r for r in starts if int(r['gpu_count'])>1] if name=='multi_gpu' else [r for r in starts if r['size_class']==name]
        group.append(dict(group=name,jobs=len(rr),earlier=sum(r['outcome']=='earlier' for r in rr),later=sum(r['outcome']=='later' for r in rr),
            mean_start_difference=finite_mean(float(r['delta_start']) for r in rr),
            total_benefit=-sum(min(0,float(r['delta_start'])) for r in rr),total_loss=sum(max(0,float(r['delta_start'])) for r in rr)))
    csv_write(out/'job-size-effects.csv',group)
    actions=load_csv(out/'per-action.csv')
    for r in actions:r['predicted_idle_cost_proxy']=float(r['delay'])*float(r['idle_fraction']) if r['delay'] and r['idle_fraction'] else None
    csv_write(out/'per-action.csv',actions)
    comparisons=load_csv(out/'candidate-comparison.csv')
    for c in comparisons:
        rr=[r for r in actions if r['action']==c['action'] and r['legal']=='True']
        c['all_predicted_idle_cost_proxy']=finite_mean(r['predicted_idle_cost_proxy'] for r in rr)
    csv_write(out/'candidate-comparison.csv',comparisons)
    print('Original telemetry accounting and size effects enriched',flush=True)

if __name__=='__main__':run()
