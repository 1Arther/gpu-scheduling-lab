"""Explicit label and local custom Volcano/HAMi-core resource parser."""
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re

import yaml

LABEL = "gpu-lab/workload-type"
KEYS = {k:"volcano.sh/vgpu-"+k for k in ("number","memory","cores","memory-percentage")}


class PodValidationError(ValueError):
    pass


def integer(value, field, *, minimum=0, maximum=None):
    if isinstance(value,bool) or not isinstance(value,(str,int)) or not re.fullmatch(r"[0-9]+",str(value)):
        raise PodValidationError(f"{field}: expected integer resource units, no Mi/Gi/m suffix")
    result = int(value)
    if result < minimum or maximum is not None and result > maximum:
        raise PodValidationError(f"{field}: outside [{minimum},{maximum}]")
    return result


@dataclass(frozen=True)
class DeviceRequest:
    container: str
    count: int
    memory_mib: int
    memory_percent: int | None
    compute_quota: int

    def memory_for(self, gpu):
        capacity = gpu.get("logical_memory_capacity_mib",gpu["hardware"]["memory_capacity_mib"])
        return self.memory_mib or max(1,int(capacity*self.memory_percent//100))


@dataclass(frozen=True)
class PodSpec:
    uid: str
    name: str
    namespace: str
    task_type: str | None
    backend: str
    requests: tuple[DeviceRequest,...]
    node_name: str | None
    node_selector: dict
    annotations: dict
    warnings: tuple[str,...] = ()
    ordinary_reason: str | None = None

    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self),sort_keys=True).encode()).hexdigest()


def parse_pod(value):
    if isinstance(value,PodSpec):
        return value
    if isinstance(value,Path):
        try:
            value = yaml.safe_load(value.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise PodValidationError("Invalid YAML syntax") from exc
    elif isinstance(value,str):
        try:
            value = yaml.safe_load(value)
        except yaml.YAMLError as exc:
            raise PodValidationError("Invalid YAML syntax") from exc
    if not isinstance(value,dict) or value.get("kind") != "Pod":
        raise PodValidationError("Expected a Pod YAML/mapping")
    metadata,spec = value.get("metadata",{}),value.get("spec",{})
    if not isinstance(metadata,dict) or not isinstance(spec,dict):
        raise PodValidationError("metadata/spec must be mappings")
    labels = metadata.get("labels",{}) or {}
    if not isinstance(labels,dict):
        raise PodValidationError("metadata.labels must be a mapping")
    task = labels.get(LABEL)
    if LABEL in labels and task not in ("training","inference"):
        raise PodValidationError(f"Illegal {LABEL}: {task!r}; expected training/inference")
    namespace,name = metadata.get("namespace","default"),metadata.get("name","")
    if not isinstance(name,str) or not name:
        raise PodValidationError("metadata.name is required")
    uid = metadata.get("uid") or f"offline:{namespace}/{name}"
    if not isinstance(uid,str) or not uid:
        raise PodValidationError("metadata.uid must be a string")
    annotations = metadata.get("annotations",{}) or {}
    selector = spec.get("nodeSelector",{}) or {}
    if not isinstance(annotations,dict) or not isinstance(selector,dict):
        raise PodValidationError("annotations/nodeSelector must be mappings")
    if any(not isinstance(k,str) or not isinstance(v,str) for mapping in (annotations,selector) for k,v in mapping.items()):
        raise PodValidationError("annotations/nodeSelector keys and values must be strings")
    if spec.get("nodeName") is not None and not isinstance(spec["nodeName"],str):
        raise PodValidationError("nodeName must be a string")
    if spec.get("affinity") or spec.get("topologySpreadConstraints"):
        raise PodValidationError("Native scheduling required for affinity/topologySpreadConstraints; not approximated by this GPU API")
    for container in spec.get("initContainers",[]) or []:
        if any(k.startswith("volcano.sh/vgpu-") for where in ("limits","requests") for k in container.get("resources",{}).get(where,{})):
            raise PodValidationError("vGPU initContainers need native lifetime accounting; unsupported in MVP")
    containers = spec.get("containers",[])
    if not isinstance(containers,list) or not containers:
        raise PodValidationError("spec.containers must be nonempty")
    requests,warnings = [],[]
    for index,c in enumerate(containers):
        if not isinstance(c,dict):
            raise PodValidationError("container must be a mapping")
        resources = c.get("resources",{}) or {}
        if not isinstance(resources,dict):
            raise PodValidationError("container resources must be a mapping")
        limits,req = resources.get("limits",{}) or {},resources.get("requests",{}) or {}
        if not isinstance(limits,dict) or not isinstance(req,dict):
            raise PodValidationError("limits/requests must be mappings")
        for key in KEYS.values():
            if key in limits and key in req and integer(limits[key],key) != integer(req[key],key):
                raise PodValidationError(f"{key}: requests and limits disagree")
        triggered = KEYS["number"] in limits or KEYS["memory"] in limits
        if not triggered:
            if any(k in req or k in limits for k in KEYS.values()):
                raise PodValidationError("Local native parser requires vgpu-number or vgpu-memory in limits")
            continue
        count = integer(limits.get(KEYS["number"],1),KEYS["number"],minimum=1,maximum=64)
        def read(key,default):
            return limits.get(KEYS[key],req.get(KEYS[key],default))
        memory = integer(read("memory",0),KEYS["memory"])
        percent = read("memory-percentage",None)
        if percent is not None:
            percent = integer(percent,KEYS["memory-percentage"],minimum=1,maximum=100)
        if memory > 0 and percent is not None:
            warnings.append("absolute_memory_overrides_percentage_as_in_local_native_source")
        if memory == 0 and percent is None:
            percent = 100
        cores = integer(read("cores",0),KEYS["cores"],maximum=100)
        if cores == 0:
            warnings.append("zero_cores_is_unrestricted_native_request_not_guaranteed_compute")
        requests.append(DeviceRequest(str(c.get("name",f"container-{index}")),count,memory,percent,cores))
    if len({r.container for r in requests}) != len(requests):
        raise PodValidationError("Duplicate GPU container names")
    reason = "no_vgpu_resource_request" if not requests else "missing_workload_label" if task is None else None
    return PodSpec(uid,name,str(namespace),task,str(annotations.get("gpu-lab/backend","CUDA")),tuple(requests),
                   spec.get("nodeName"),dict(selector),dict(annotations),tuple(warnings),reason)
