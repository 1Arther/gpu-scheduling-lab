"""Read-only original evidence diagnostics. No training, no new test selection."""
import argparse
import csv
from collections import Counter
import numpy as np
import torch
from sb3_contrib import MaskablePPO
from .common import OLD,OUT,read,dump,csv_write,finite_mean,now
from ..queue_rl.observations import Scales,encode

def probabilities(model, actions, scales, variant='joint'):
    obs=np.asarray([encode(a['context'],scales,variant) for a in actions])
    masks=np.asarray([a['mask'] for a in actions],dtype=bool)
    with torch.no_grad():
        d=model.policy.get_distribution(torch.as_tensor(obs,device=model.device),action_masks=masks)
        return d.distribution.probs.cpu().numpy(),d.entropy().cpu().numpy()

def action_stats(actions):
    legal=np.asarray([a['mask'] for a in actions],dtype=bool).reshape(-1,4)
    counts=Counter(a['action'] for a in actions);opportunities=int(legal[:,1:].any(axis=1).sum())
    return dict(decisions=len(actions),legal_counts=legal.sum(axis=0).tolist(),
        legal_ratio=(legal.mean(axis=0).tolist() if len(actions) else [0]*4),
        chosen=[counts[i] for i in range(4)],opportunities=opportunities,
        conditional_protection=sum(counts[i] for i in (1,2,3))/max(1,opportunities),
        c_legal_but_not_selected=int(sum(a['mask'][3] and a['action']!=3 for a in actions)))

def diagnose():
    torch.set_num_threads(1);out=OUT/'diagnosis';out.mkdir(parents=True,exist_ok=True)
    selected=read(OLD/'selection.json');scales=Scales(**read(OLD/'scales.json'))
    model=MaskablePPO.load(OLD/selected['models']['joint']['path'],device='cpu')
    print('Reading preserved original test evidence once (638 MB)',flush=True)
    data=read(OLD/'test/results.json')
    ppo=[r for r in data if r['policy']=='hrrn_ppo'];hrrn={(r['seed'],r['condition']):r for r in data if r['policy']=='hrrn'}
    all_actions=[];action_rows=[];job_rows=[];by_condition=[]
    for r in ppo:
        acts=r['actions'];all_actions.extend(acts)
        if acts:
            probs,entropy=probabilities(model,acts,scales)
            for i,(a,ps,en) in enumerate(zip(acts,probs,entropy)):
                c=a['context']
                for k in range(4):
                    combo=c['combos'][k-1] if 0<k<=len(c['combos']) else {}
                    action_rows.append(dict(seed=r['seed'],condition=r['condition'],decision=i,time=c['time'],job_id=c['owner']['job_id'],
                        action=k,label=('0','A','B','C')[k],legal=a['mask'][k],chosen=a['action']==k,
                        probability=float(ps[k]),entropy=float(en),owner_gpu_count=c['owner']['gpu_count'],
                        delay=combo.get('delay'),deficit_memory=(combo.get('deficit') or [None]*3)[0],
                        deficit_core=(combo.get('deficit') or [None]*3)[1],deficit_slots=(combo.get('deficit') or [None]*3)[2],
                        idle_fraction=finite_mean(combo.get('idle',[])),blocked=combo.get('blocked_count'),
                        rf_score=combo.get('rf_score'),targets='|'.join(combo.get('targets',[]))))
        ref=hrrn[r['seed'],r['condition']]
        starts=lambda row:{e['job_id']:e['time'] for e in row['physical_events'] if e['kind']=='start'}
        a,b=starts(r),starts(ref)
        protected={x['context']['owner']['job_id'] for x in acts if x['action']}
        for jid in sorted(a):
            job_rows.append(dict(seed=r['seed'],condition=r['condition'],job_id=jid,protected=jid in protected,
                ppo_start=a[jid],hrrn_start=b[jid],delta_start=a[jid]-b[jid],
                outcome='earlier' if a[jid]<b[jid]-1e-8 else 'later' if a[jid]>b[jid]+1e-8 else 'same'))
    for condition in sorted({r['condition'] for r in ppo}):
        by_condition.append(dict(condition=condition,**action_stats([a for r in ppo if r['condition']==condition for a in r['actions']])))
    csv_write(out/'per-action.csv',action_rows);csv_write(out/'same-job-starts.csv',job_rows)
    dump(out/'by-condition.json',by_condition)
    comparisons=[]
    # Compare the three candidates on IDENTICAL states where all three exist;
    # separate this from conditional summaries biased by legality.
    complete={(r['seed'],r['condition'],r['decision']) for r in action_rows if r['action']==3 and r['legal']}
    for k in (1,2,3):
        rr=[r for r in action_rows if r['action']==k and r['legal']]
        paired=[r for r in rr if (r['seed'],r['condition'],r['decision']) in complete]
        comparisons.append(dict(action=k,legal_count=len(rr),multi_gpu_count=sum(r['owner_gpu_count']>1 for r in rr),
            **{f'all_{m}':finite_mean(r[m] for r in rr) for m in ('delay','deficit_memory','deficit_core','deficit_slots','idle_fraction','blocked','rf_score','probability')},
            **{f'paired_ABC_{m}':finite_mean(r[m] for r in paired) for m in ('delay','deficit_memory','deficit_core','deficit_slots','idle_fraction','blocked','rf_score','probability')}))
    csv_write(out/'candidate-comparison.csv',comparisons)
    old_train=[]
    for path in sorted((OLD/'training').glob('*/metadata.json')):
        meta=read(path);episodes=read(path.parent/'episodes.json')
        aa=[dict(action=a,mask=m) for e in episodes for a,m in zip(e['actions'],e['masks'])]
        with (path.parent/'progress.csv').open(encoding='utf-8') as f:logs=list(csv.DictReader(f))
        dev=read(OLD/f"development/{meta['model_id']}.json")
        st=action_stats([a for r in dev for a in r['actions']])
        old_train.append(dict(model=meta['model_id'],variant=meta['variant'],seed=meta['seed'],idle=meta['reward']['idle'],
            steps=meta['steps'],episodes=len(episodes),completed_episode_decisions=len(aa),
            completed_episode_effective=action_stats(aa)['opportunities'],
            completed_no_decision_episodes=sum(not e['actions'] for e in episodes),
            unaccounted_transitions=meta['steps']-len(aa)-sum(not e['actions'] for e in episodes),
            last_logged_updates=logs[-1].get('train/n_updates'),last_logged_entropy_loss=logs[-1].get('train/entropy_loss'),
            last_logged_value_loss=logs[-1].get('train/value_loss'),last_explained_variance=logs[-1].get('train/explained_variance'),
            last_kl=logs[-1].get('train/approx_kl'),last_clip_fraction=logs[-1].get('train/clip_fraction'),
            dev_actions=str(st['chosen']),dev_conditional_protection=st['conditional_protection'],
            note='Original episode records omit final unfinished episode; last progress dump precedes final PPO train update'))
    csv_write(out/'original-training-and-seeds.csv',old_train)
    summary=dict(completed_at=now(),original_model=selected['models']['joint'],**action_stats(all_actions),
        entropy=finite_mean(r['entropy'] for r in action_rows if r['action']==0),
        jobs=Counter(r['outcome'] for r in job_rows),
        protected_jobs=Counter(r['outcome'] for r in job_rows if r['protected']),
        sum_start_benefit=-sum(min(0,r['delta_start']) for r in job_rows),
        sum_start_loss=sum(max(0,r['delta_start']) for r in job_rows),
        probabilities='Recomputed from frozen final policy on saved PUBLIC observations; not historical rollout probabilities',
        advantages='Original raw advantages were not saved; cannot recover them from final weights. Supplementary runs instrument rollout/update data.',
        causal_scope='Paired full-trajectory scheduling differences, not isolated one-action causal effects; no counterfactual action optimization used.')
    dump(out/'summary.json',summary);print(summary,flush=True)

if __name__=='__main__':diagnose()
