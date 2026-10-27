"""Only public state enters decisions. Candidate index 1 is frozen RF best."""
from dataclasses import asdict
from ..queue_sim.placement import capacity, free, amount
from ..queue_sim.policies import Protection, order, overdue, response_ratio
from ..queue_study.easy import project

def decision_context(cluster, waiting, forecasts, placement, cfg, now):
    ordered=order(waiting,now,'hrrn',cfg)
    owner=None; plans=[]
    for j in sorted(waiting,key=lambda j:(j.ready_at,j.job_id)):
        if not overdue(j,now,cfg): continue
        if placement.choose(cluster,j) is not None: continue
        plans=placement.plans(cluster,j,empty=True)
        if plans: owner=j;break
    if owner is None: return None
    # Stable RF rank, then deterministic IDs. Full query retains all combinations;
    # action space exposes at most three. No RF scores are changed.
    plans=plans[:3]; combos=[]
    caps=[sum(capacity(g)[d] for g in cluster.gpus.values()) for d in range(3)]
    # One public calendar traversal for all combinations. Projecting separately
    # for every (combination, deadline) repeatedly copied the same physical state.
    # Use only observed running Jobs and their public deadlines, never the heap.
    projected=project(cluster,forecasts,now);delays={p.gpu_ids:None for p in plans}
    for when in sorted({now,*(f.expected_end for f in forecasts.values())}):
        for jid,f in forecasts.items():
            if f.expected_end<=when and jid in projected.running:projected.release(jid)
        for plan in plans:
            if delays[plan.gpu_ids] is None and projected.valid(owner,plan.gpu_ids):
                delays[plan.gpu_ids]=when-now
        if all(v is not None for v in delays.values()):break
    for plan in plans:
        guard=Protection.create(1,owner,plan,cluster,now)
        delay=delays[plan.gpu_ids]
        idle=guard.idle(cluster)
        fractions=[sum(v[d] for v in idle.values())/caps[d] for d in range(3)]
        blocked=[]
        for j in ordered:
            if j.job_id==owner.job_id: continue
            possible=placement.plans(cluster,j)
            if possible and not any(guard.allows(cluster,j,p.gpu_ids) for p in possible): blocked.append(j.job_id)
        deficits=[sum(max(0,amount(owner)[d]-free(cluster.gpus[k])[d]) for k in plan.gpu_ids)/caps[d] for d in range(3)]
        combos.append(dict(targets=list(plan.gpu_ids),node=plan.node,rf_score=plan.score,delay=delay,
            deficit=deficits,idle=fractions,blocked_jobs=blocked,blocked_count=len(blocked)))
    def job_view(j):
        return dict(**asdict(j),wait=now-j.ready_at,hrrn=response_ratio(j,now,cfg),
            feasible=placement.choose(cluster,j) is not None,overdue=overdue(j,now,cfg),
            trigger=cfg.trigger(j))
    gpu_views=[]
    compatible={k for p in placement.plans(cluster,owner,empty=True) for k in p.gpu_ids}
    for k,g in sorted(cluster.gpus.items()):
        gpu_views.append(dict(gpu_id=k,profile=g.profile.profile_id,node=cluster.nodes[k],
            capacity=list(capacity(g)),free=list(free(g)),active_jobs=len(g.allocations),
            online=cluster.online[k],compatible=k in compatible,protected=False))
    return dict(time=now,owner=job_view(owner),queue=[job_view(j) for j in ordered],
        gpus=gpu_views,combos=combos,forecasts=[f.record() for _,f in sorted(forecasts.items())],
        long_threshold=cfg.evaluation_long_wait)

def masks(context, variant='joint'):
    n=len(context['combos']) if context else 0
    return [True]+[i<n and (variant!='switch_only' or i==0) for i in range(3)]
