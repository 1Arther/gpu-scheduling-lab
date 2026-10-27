"""Exact memoization around the same frozen placement, shared by all policies.

UID, allocation IDs and revision are not scoring inputs. All resource amounts,
task-type instance counts, device/node constraints and health ARE cache keys.
Returned plans always carry current physical revisions and protection epoch.
"""
from collections import OrderedDict
from ..queue_sim.placement import FrozenPlacement, Plan, free, capacity, amount


class MemoizedPlacement(FrozenPlacement):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.cache=OrderedDict();self.cache_hits=0

    def plans(self,cluster,job,protection=None,*,empty=False):
        key=(job.task_type,amount(job),job.gpu_count,job.allowed_profiles,job.node,empty,
             tuple((k,cluster.nodes[k],g.profile.profile_id,g.max_instances,cluster.online[k],
                    capacity(g) if empty else free(g),0 if empty else g.instances_of_type('training'),
                    0 if empty else g.instances_of_type('inference')) for k,g in sorted(cluster.gpus.items())))
        if key in self.cache:
            saved,count=self.cache[key];self.cache.move_to_end(key)
            self.calls+=1;self.candidates+=count;self.cache_hits+=1
        else:
            before=self.candidates
            plans=super().plans(cluster,job,empty=empty)
            saved=tuple((p.node,p.gpu_ids,p.score) for p in plans);count=self.candidates-before
            self.cache[key]=(saved,count)
            if len(self.cache)>8192:self.cache.popitem(last=False)
        return [Plan(node,ids,score,tuple((k,cluster.gpus[k].revision) for k in ids),protection.epoch if protection else None)
                for node,ids,score in saved
                if empty or protection is None or protection.allows(cluster,job,ids)]
