"""Offline replay of all original actual guards; actual events stay outside policy input."""
import csv
from collections import defaultdict,Counter
import numpy as np
import torch
from sb3_contrib import MaskablePPO
from .common import OLD,OUT,read,dump,csv_write,finite_mean,now
from ..queue_rl.experiment import evaluate_trace
from ..queue_rl.observations import Scales
from ..queue_study.placement import MemoizedPlacement

def run():
    torch.set_num_threads(1);selected=read(OLD/'selection.json')
    model=MaskablePPO.load(OLD/selected['models']['joint']['path'],device='cpu')
    placement=MemoizedPlacement();scales=Scales(**read(OLD/'scales.json'))
    with (OUT/'diagnosis/per-action.csv').open(encoding='utf-8-sig') as f:aa=list(csv.DictReader(f))
    original=defaultdict(list)
    for a in aa:
        if a['chosen']=='True':original[int(a['seed']),a['condition']].append(a)
    positive={k for k,v in original.items() if any(int(a['action']) for a in v)}
    samples=[];episodes=[];replays=[]
    for t in read(OLD/'inputs/test.json'):
        key=t['seed'],'__'.join(t['condition'].values())
        if key not in positive:continue
        row,r=evaluate_trace(t,'hrrn_ppo',placement,scales,model)
        assert [(a['action'],a['context']['time'],a['context']['owner']['job_id']) for a in row['actions']]==[
            (int(a['action']),float(a['time']),a['job_id']) for a in original[key]]
        jobs={j['job_id']:j for j in r['jobs']};guards={};started=set();last={};monotone=True
        for event in r['events']:
            if event['kind']=='start':started.add(event['job_id'])
            p=event.get('protection')
            if not p or p['job_id'] in started:continue
            epoch=p['epoch'];jid=p['job_id'];guards.setdefault(epoch,event)
            if event['kind'] not in ('protection_create','complete','start','dispatch_end'):continue
            for gpu in p['targets']:
                g=event['state'][gpu];free=[g['remaining_memory_mib'],g['remaining_compute_units'],g['remaining_instances']]
                required=p['required'][gpu]
                for d,label in enumerate(('memory_mib','compute_units','slots')):
                    idle=min(free[d],required[d]);prev=last.get((epoch,gpu,d),0)
                    monotone &= idle>=prev-1e-8;last[epoch,gpu,d]=idle
                    samples.append(dict(seed=key[0],condition=key[1],epoch=epoch,job_id=jid,gpu=gpu,dimension=label,
                        time=event['time'],event=event['kind'],event_job=event.get('job_id'),
                        required=required[d],free=free[d],protected_idle=idle,missing=max(0,required[d]-free[d])))
        assert monotone,'Later jobs consumed accumulated guard capacity'
        for epoch,e in guards.items():
            p=e['protection'];jid=p['job_id'];stop=jobs[jid]['start_at']
            areas=[sum((x['end']-x['start'])*x['protected_idle'][d] for x in r['timeline'] if x['protection_epoch']==epoch) for d in range(3)]
            decision=next(a for a in row['actions'] if a['action'] and a['context']['time']==e['time'] and a['context']['owner']['job_id']==jid)
            combo=decision['context']['combos'][decision['action']-1]
            episodes.append(dict(seed=key[0],condition=key[1],epoch=epoch,job_id=jid,action=decision['action'],
                activation=e['time'],start=stop,predicted_delay=combo['delay'],observed_delay=stop-e['time'],
                targets='|'.join(p['targets']),target_count=len(p['targets']),gpu_count=jobs[jid]['gpu_count'],
                idle_memory_time=areas[0],idle_core_time=areas[1],idle_slots_time=areas[2],monotone=True,
                idle_time_scope='clipped to original observation window'))
        replays.append(dict(seed=key[0],condition=key[1],audit=row['audit'],reproduces_saved_actions=True))
    csv_write(OUT/'diagnosis/resource-accumulation.csv',samples)
    csv_write(OUT/'diagnosis/protection-episodes.csv',episodes)
    dump(OUT/'diagnosis/accumulation-summary.json',dict(time=now(),episodes=len(episodes),traces=len(replays),
        all_accumulated_capacity_monotone=True,mean_predicted_delay=finite_mean(x['predicted_delay'] for x in episodes),
        mean_observed_delay=finite_mean(x['observed_delay'] for x in episodes),
        replay_audits=replays,semantics='Offline full synthetic trace replay of ORIGINAL policy actions; no alternative-action or future-information policy input'))
    print(dict(replayed=len(replays),protections=len(episodes),snapshots=len(samples)),flush=True)

if __name__=='__main__':run()
