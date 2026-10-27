"""Predeclared all-Job and size-stratified metrics; synthetic service only."""
import numpy as np
from ..queue_sim.placement import capacity


def size_class(job,fleet):
    minimum=[min(capacity(g)[d] for g in fleet.values()) for d in range(3)]
    r=job.request
    dominant=job.gpu_count*max(a/b for a,b in zip((r.memory_mib,r.compute_units,r.instances),minimum))
    return 'large' if dominant>=1.0 else 'small' if dominant<=.4 else 'medium'


def extended_metrics(result,jobs,fleet,diagnostics,tau_eval=1.):
    rows={r['job_id']:r for r in result['jobs']};m=dict(result['metrics'])
    assert all(r['status']=='completed' for r in rows.values()),'Formal finite workloads must drain; do not drop censored Jobs'
    wait=[rows[j.job_id]['wait'] for j in jobs]
    m['p99_wait']=float(np.quantile(wait,.99))
    slowdown=[max(1.,(rows[j.job_id]['finish_at']-j.ready_at)/max(j.actual_service,tau_eval)) for j in jobs]
    m['mean_bounded_slowdown']=float(np.mean(slowdown))
    window=result['config']['observation_end']
    m['completed_work']=sum(j.gpu_count*j.request.compute_units/100*j.actual_service for j in jobs
                            if rows[j.job_id]['finish_at']<=window)
    m['completed_work_semantics']='synthetic committed core-quota x GPU count x service; not FLOPs or measured work'
    m['small_large_mean_wait_gap']=None
    for group in ('small','medium','large','multi_gpu','single_gpu'):
        chosen=[j for j in jobs if (j.gpu_count>1 if group=='multi_gpu' else j.gpu_count==1 if group=='single_gpu' else size_class(j,fleet)==group)]
        w=[rows[j.job_id]['wait'] for j in chosen]
        m[group+'_count']=len(w)
        for label,q in (('mean',None),('p95',.95),('p99',.99)):
            m[group+'_'+label+'_wait']=float(np.mean(w) if q is None else np.quantile(w,q)) if w else None
        m[group+'_long_wait_ratio']=float(np.mean(np.array(w)>=result['config']['evaluation_long_wait'])) if w else None
    if m['large_mean_wait'] is not None and m['small_mean_wait'] is not None:
        m['small_large_mean_wait_gap']=m['large_mean_wait']-m['small_mean_wait']
    times=[d['wall_seconds'] for d in diagnostics]
    m['decision_ms_mean']=1000*float(np.mean(times)) if times else 0.
    m['decision_ms_p95']=1000*float(np.quantile(times,.95)) if times else 0.
    creates=[e for e in result['events'] if e['kind']=='protection_create']
    m['activation_count']=len(creates)
    m['activation_mean_idle_fraction']=float(np.mean([e['idle_fraction_of_required'] for e in creates])) if creates else None
    ds=[e['estimated_target_delay'] for e in creates if e['estimated_target_delay'] is not None]
    m['activation_mean_predicted_delay']=float(np.mean(ds)) if ds else None
    penalties=[e['estimated_target_delay']-e['earliest_any_combination_delay'] for e in creates
               if e['estimated_target_delay'] is not None and e['earliest_any_combination_delay'] is not None]
    m['activation_mean_target_penalty']=float(np.mean(penalties)) if penalties else None
    episodes={e['protection']['epoch']:e for e in creates}
    for label in ('near','far','unknown'):
        epochs={epoch for epoch,e in episodes.items() if
                ('unknown' if e['estimated_target_delay'] is None else 'near' if e['estimated_target_delay']<=2. else 'far')==label}
        m['activation_'+label+'_count']=len(epochs)
        m['protected_idle_core_time_'+label]=sum((x['end']-x['start'])*x['protected_idle'][1]
            for x in result['timeline'] if x['protection_epoch'] in epochs)
    m['target_penalty_positive_count']=sum(x>1e-8 for x in penalties)
    m['other_jobs_bypassed_during_protection']=len({e['job_id'] for e in result['events'] if e['kind']=='bypass'
        and e['protection'] is not None and e['job_id']!=e['protection']['job_id']})
    m['easy_reservation_delays']=sum(e['kind']=='easy_reservation_update' and e['delayed'] for e in result['events'])
    m['estimate_extensions']=sum(e['kind']=='estimate_update' for e in result['events'])
    return m


def flatten(metrics):
    out={}
    for k,v in metrics.items():
        if isinstance(v,dict):
            out.update({k+'_'+n:x for n,x in v.items()})
        elif isinstance(v,(int,float,type(None))):out[k]=v
    return out
