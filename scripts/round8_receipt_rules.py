"""Receipt checks for this single-container, positive-MiB HAMI experiment.

Audit only. Native Volcano is the allocator; this does not choose devices.
Mirrors the audited canFitForOrder constraints, with health/type checked outside.
"""


def candidate_uuids(devices,memory_mib,cores):
    result=set()
    for d in devices:
        used=d['allocated_vgpu_count'];used_core=d['allocated_compute_quota']
        if not d['healthy'] or used>=d['max_instances']:continue
        if d['registered_memory_mib']-d['allocated_memory_mib']<memory_mib:continue
        if used_core+cores>100:continue
        if cores==100 and used>0:continue
        if cores==0 and used_core==100:continue
        result.add(d['uuid'])
    return result
