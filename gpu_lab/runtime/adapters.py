"""Simulator and mock Volcano boundaries; never import a Kubernetes/DQN client."""
from copy import deepcopy

from ..crossgpu.features import from_ledger, SYNTHETIC_QUOTA
from .pods import DeviceRequest, PodSpec, parse_pod
from .pool import GPUPool, ReservationConflict
from .schema import digest, apply_slot
from .service import SuitabilityService


def legacy_view(state,catalog):
    d = catalog[state.profile.profile_id]
    return {"uuid":"SIM-"+state.gpu_id,"node":state.gpu_id.split("/")[0],"device_id":d.device_id,"model":d.record["model"],
            "hardware":d.values(),"dynamic":from_ledger(state),"healthy":True,"managed":True,"plugin":"volcano-vgpu",
            "backend":"CUDA","sharing_mode":"hami-core","quota_semantics":SYNTHETIC_QUOTA,"revision":state.revision,
            "max_instances":state.max_instances,"node_labels":{},"identity_source":"synthetic_simulator_identity"}


class RuntimeScheduler:
    """Old simulator remains the sole owner of its GPUState mutations."""
    def __init__(self,name,service):
        if name not in ("binpack","static_rf","dynamic_rf"):
            raise ValueError("Invalid simulator strategy")
        self.name,self.service,self.calls = name,service,0

    def choose(self,fleet,pod_request):
        if pod_request.request.instances != 1:
            raise ValueError("Legacy aggregate instances need a native placement request; do not reinterpret them as vgpu-number")
        visible = PodSpec("sim-visible-pod","sim-visible-pod","simulation",pod_request.task_type,"CUDA",
                          (DeviceRequest("workload",1,pod_request.request.memory_mib,None,pod_request.request.compute_units),),None,{}, {})
        views = [legacy_view(s,self.service.catalog) for s in fleet.values()]
        self.calls += 1
        ranking = self.service.rank_gpus(visible,views,strategy=self.name)
        if ranking["recommendation"] is None:
            return None,None
        uuid = ranking["recommendation"]["gpu_uuid"]
        state = next(s for s in fleet.values() if "SIM-"+s.gpu_id == uuid)
        return state,{"policy":self.name,"runtime_api":ranking}

    def on_state_change(self,state):
        return self.service.classify_gpu(legacy_view(state,self.service.catalog))


class MockVolcanoAdapter:
    """Runnable contract mock, NOT a compiled or installed Go scheduler plugin."""
    def __init__(self,service,pool,*,adviser=None):
        self.service,self.pool,self.adviser = service,pool,adviser
        self.fallback = SuitabilityService(service.bundle_dir,force_fallback=True)

    def plan(self,pod):
        snapshots = self.pool.snapshot()
        if self.adviser is None:
            return self.service.rank_gpus(pod,snapshots)
        try:
            result = self.adviser.rank_gpus(pod,snapshots)
            rec = result.get("recommendation")
            if result.get("schema_version") != "pod-gpu-api/v1" or result.get("model_version") != self.service.version:
                raise ValueError("adviser_model_version_mismatch")
            if rec is None:
                # Resource failure decisions are re-evaluated locally as well.
                raise ValueError("adviser_no_complete_plan")
            p = parse_pod(pod)
            check = deepcopy(rec); token = check.pop("selection_fingerprint",None)
            if token != digest(check) or rec.get("pod_fingerprint") != p.fingerprint():
                raise ValueError("adviser_plan_fingerprint_mismatch")
            known = {g["uuid"]:g for g in snapshots}
            requests = {r.container:r for r in p.requests}
            expected_units = {(r.container,i) for r in p.requests for i in range(r.count)}
            if len(rec["assignments"]) != len(expected_units) or {(a.get("container"),a.get("unit")) for a in rec["assignments"]} != expected_units:
                raise ValueError("adviser_incomplete_request_slots")
            if rec.get("model_version") != self.service.version:
                raise ValueError("adviser_plan_model_version_mismatch")
            used = set()
            for a in rec["assignments"]:
                key = a["gpu_uuid"]
                if key not in known or a["container"] not in requests:
                    raise ValueError("adviser_unknown_UUID_or_container")
                marker = (a["container"],key)
                if marker in used or rec["expected_revisions"].get(key) != known[key].get("revision",0):
                    raise ValueError("adviser_duplicate_UUID_or_stale_revision")
                used.add(marker)
                errors,resolved = self.service.hard_filter(p,known[key],requests[a["container"]])
                if errors:
                    raise ValueError("adviser_violated_hard_constraints")
                if a.get("node") != known[key]["node"] or rec.get("node") != known[key]["node"] or any(a.get(k) != resolved[k] for k in ("memory_mib","compute_quota","instances")):
                    raise ValueError("adviser_changed_node_or_request_amount")
                apply_slot(known[key],p.task_type,a["memory_mib"],a["compute_quota"])
            return result
        except Exception as exc:
            fallback = self.fallback.rank_gpus(pod,snapshots)
            fallback["adviser_failure"] = f"{type(exc).__name__}:{exc}"
            return fallback

    def allocate(self,pod,plan,*,actual_gpu_uuids=None):
        if plan.get("recommendation") is None:
            return {"status":plan.get("status"),"allocated":False}
        held = self.pool.reserve(pod,plan["recommendation"])
        expected = [a["gpu_uuid"] for a in held["assignments"]]
        committed = self.pool.commit(held["reservation_id"],expected if actual_gpu_uuids is None else actual_gpu_uuids)
        return {"reservation":held,"commit":committed,"allocated":True,"scope":"offline_mock_only"}


def dispatch(service,payload):
    """Transport-independent JSON handler; a future local HTTP handler can call it."""
    if not isinstance(payload,dict) or payload.get("schema_version") != "pod-gpu-api/v1":
        return {"status":"invalid_input","errors":["schema_version_mismatch"]}
    if payload.get("method") == "classify_gpu":
        return service.classify_gpu(payload.get("gpu"))
    if payload.get("method") == "rank_gpus":
        return service.rank_gpus(payload.get("pod"),payload.get("gpus",[]))
    return {"status":"invalid_input","errors":["unknown_method"]}
