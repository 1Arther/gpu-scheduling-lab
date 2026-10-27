"""Interpretable strong baseline and safe frozen-model inference."""
from dataclasses import dataclass,asdict
from time import perf_counter
import numpy as np

@dataclass(frozen=True)
class HeuristicSpec:
    severe_wait:float=4.
    near_delay:float=2.
    max_blocked:int=1
    max_idle:float=.25

def heuristic(context,mask,spec=None):
    spec=spec or HeuristicSpec();o=context['owner']
    if o['wait']<max(spec.severe_wait,.75*o['estimated_service']):return 0,'wait_not_severe'
    viable=[]
    for i,p in enumerate(context['combos']):
        if not mask[i+1] or p['delay'] is None:continue
        if p['delay']>spec.near_delay:continue
        if p['blocked_count']>spec.max_blocked:continue
        idle=sum(p['idle'])/3
        if idle>spec.max_idle:continue
        # Discount delay + stranded capacity + immediate blocked jobs. A public
        # forecast close to release is necessary; waiting alone is insufficient.
        viable.append((p['delay']+2*idle+.5*p['blocked_count'],-p['rf_score'],i))
    if not viable:return 0,'far_or_idle_or_blocking_cost'
    i=min(viable)[2]
    return i+1,'severe_wait_near_complete_acceptable_cost'

def never(context,mask):return 0,'never_protect'
def fixed(context,mask):return 1 if mask[1] else 0,'fixed_RF_first'

def safe_predict(model,obs,mask,limit_ms=100.):
    """No ledger mutation while inference runs. Late results are discarded.

    This is a post-return deadline check, not preemptive process cancellation.
    """
    started=perf_counter()
    try:
        value,_=model.predict(obs,action_masks=np.asarray(mask,dtype=bool),deterministic=True)
        action=int(np.asarray(value).item())
        elapsed=1000*(perf_counter()-started)
        if elapsed>limit_ms:return 0,'inference_deadline_fallback',elapsed
        if action<0 or action>=len(mask) or not mask[action]:return 0,'inference_illegal_fallback',elapsed
        return action,'model',elapsed
    except Exception as exc:
        return 0,'inference_exception_fallback_'+type(exc).__name__,1000*(perf_counter()-started)
