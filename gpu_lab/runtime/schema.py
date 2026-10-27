"""JSON GPU snapshots: physical capabilities are independent of reservations."""
from copy import deepcopy
import hashlib
import json
import math

from ..crossgpu.catalog import Device, STATIC, REQUIRED
from ..crossgpu.features import SYNTHETIC_QUOTA

SCHEMA_VERSION = "pod-gpu-api/v1"
HAMI_QUOTA = "volcano_hami_core_percent_100_v1"
MEMORY_REFERENCE_MAPPING = "registered_fraction_to_frozen_catalog_reference_v1"
MAPPED_QUOTAS = {SYNTHETIC_QUOTA,HAMI_QUOTA,"physical_unpartitioned"}


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()


def gpu_errors(g):
    if not isinstance(g,dict):
        return ["gpu_snapshot_must_be_mapping"]
    errors = []
    for name in ("uuid","node"):
        if not isinstance(g.get(name),str) or not g[name]:
            errors.append(f"missing_{name}")
    h,d = g.get("hardware"),g.get("dynamic")
    if not isinstance(h,dict) or not isinstance(d,dict):
        return errors+["hardware_and_dynamic_must_be_mappings"]
    cap = h.get("memory_capacity_mib")
    if isinstance(cap,bool) or not isinstance(cap,int) or cap <= 0:
        errors.append("invalid_physical_memory_capacity_mib")
    for k in STATIC:
        v = h.get(k)
        if v is not None and (isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v <= 0):
            errors.append(f"invalid_hardware:{k}")
    fields = ("free_memory_mib","free_compute_quota","allocated_vgpu_count","running_training_count","running_inference_count")
    for k in fields:
        v = d.get(k)
        if isinstance(v,bool) or not isinstance(v,int) or v < 0:
            errors.append(f"invalid_dynamic:{k}")
    limit,revision = g.get("max_instances",10),g.get("revision",0)
    if isinstance(limit,bool) or not isinstance(limit,int) or limit < 1:
        errors.append("invalid_instance_limit")
    if isinstance(revision,bool) or not isinstance(revision,int) or revision < 0:
        errors.append("invalid_revision")
    if errors:
        return errors
    logical = g.get("logical_memory_capacity_mib",cap)
    mapped_memory = g.get("model_memory_mapping") == MEMORY_REFERENCE_MAPPING and g.get("quota_semantics") == HAMI_QUOTA
    if isinstance(logical,bool) or not isinstance(logical,int) or logical <= 0 or (logical > cap and not mapped_memory):
        errors.append("invalid_logical_memory_capacity_mib")
    elif d["free_memory_mib"] > logical:
        errors.append("free_memory_exceeds_logical_capacity")
    if d["free_memory_mib"] > cap and not mapped_memory:
        errors.append("free_memory_exceeds_physical_capacity")
    if d["free_compute_quota"] > 100:
        errors.append("compute_quota_exceeds_100")
    if d["allocated_vgpu_count"] > limit:
        errors.append("instance_count_exceeds_limit")
    unknown = d.get("running_unknown_count",d["allocated_vgpu_count"]-d["running_training_count"]-d["running_inference_count"])
    if not isinstance(unknown,int) or isinstance(unknown,bool) or unknown < 0 or unknown+d["running_training_count"]+d["running_inference_count"] != d["allocated_vgpu_count"]:
        errors.append("task_instance_counts_inconsistent")
    if g.get("quota_semantics") == "physical_unpartitioned" and (d["allocated_vgpu_count"] or d["free_compute_quota"] != 100):
        errors.append("physical_unpartitioned_snapshot_is_not_empty")
    return errors


def device_for(g,catalog):
    device_id = g.get("device_id","unknown_gpu")
    if device_id in catalog:
        record = deepcopy(catalog[device_id].record)
    else:
        record = {"device_id":device_id,"model_group":device_id,"vendor":g.get("vendor","NVIDIA"),
                  "model":g.get("model",device_id),"architecture":g.get("architecture"),"device_type":"GPU","software_backend":g.get("backend","CUDA"),"specifications":{}}
    record.update(device_id=device_id,model=g.get("model",record["model"]),vendor=g.get("vendor",record["vendor"]),
                  architecture=g.get("architecture",record.get("architecture")),software_backend=g.get("backend","CUDA"))
    record["specifications"] = {k:{"value":g["hardware"].get(k),"model_comparable":k not in STATIC[3:6]} for k in STATIC}
    return Device(record)


def model_state(g):
    state = {**g["dynamic"],"quota_semantics":SYNTHETIC_QUOTA if g.get("quota_semantics") == HAMI_QUOTA else g.get("quota_semantics"),
             "instance_limit":g.get("max_instances",10)}
    if g.get("model_memory_mapping") == MEMORY_REFERENCE_MAPPING:
        # Legacy training used conservative nominal-GB catalog capacities. Keep
        # its feature domain while native admission uses the UNCHANGED ledger.
        state["free_memory_mib"] = (g["dynamic"]["free_memory_mib"] * g["hardware"]["memory_capacity_mib"]
                                    // g["logical_memory_capacity_mib"])
    return state


def apply_slot(g,task,memory,compute,sign=1):
    """Speculative update or locked ledger update, never changes hardware."""
    d = g["dynamic"]
    d["free_memory_mib"] -= sign*memory
    d["free_compute_quota"] -= sign*compute
    d["allocated_vgpu_count"] += sign
    key = "running_"+(task if task is not None else "unknown")+"_count"
    d[key] = d.get(key,0)+sign
    if compute == 0:
        d["unrestricted_vgpu_count"] = d.get("unrestricted_vgpu_count",0)+sign
    g["revision"] = g.get("revision",0)+1


def make_fleet(catalog):
    result = []
    for device_id,count in (("rtx4090",4),("rtx5090",4),("l40",3)):
        for i in range(count):
            result.append({"uuid":f"SIM-GPU-{device_id}-{i:02d}","node":f"sim-node-{device_id}","device_id":device_id,
                           "model":catalog[device_id].record["model"],"hardware":catalog[device_id].values(),
                           "dynamic":{"free_memory_mib":catalog[device_id].modeled_memory_mib,"free_compute_quota":100,
                                      "allocated_vgpu_count":0,"running_training_count":0,"running_inference_count":0,"running_unknown_count":0,
                                      "running_training_pod_count":0,"running_inference_pod_count":0},
                           "healthy":True,"managed":True,"plugin":"volcano-vgpu","backend":"CUDA","sharing_mode":"hami-core",
                           "quota_semantics":SYNTHETIC_QUOTA,"max_instances":10,"revision":0,"node_labels":{},
                           "identity_source":"synthetic_fixture_not_live_UUID_or_node","resource_source":"scheduler_reservation_ledger_not_measured_utilization"})
    return result
