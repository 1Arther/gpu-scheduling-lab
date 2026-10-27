"""Process-local authoritative mock ledger: CAS reserve / confirm / rollback.

This lock is NOT a distributed Volcano lock. In a real integration Volcano's
device cache remains authoritative; Python service performs advice only.
"""
from copy import deepcopy
import threading
import time

from .pods import parse_pod
from .schema import apply_slot, digest, gpu_errors


class ReservationConflict(RuntimeError):
    def __init__(self,code,retryable=True):
        super().__init__(code)
        self.code,self.retryable = code,retryable


class GPUPool:
    def __init__(self,service,gpu_list,*,reservation_timeout_s=30.):
        self.service,self._lock = service,threading.RLock()
        self._gpus,self._held,self._classifications = {},{},{}
        self.reservation_timeout_s = reservation_timeout_s
        for original in gpu_list:
            errors = gpu_errors(original)
            if errors:
                raise ValueError(errors)
            g = deepcopy(original)
            key = g["uuid"]
            if key in self._gpus:
                raise ValueError("Duplicate physical GPU UUID")
            d = g["dynamic"]
            d.setdefault("running_unknown_count",d["allocated_vgpu_count"]-d["running_training_count"]-d["running_inference_count"])
            self._gpus[key] = g

    def snapshot(self):
        with self._lock:
            return deepcopy([self._gpus[k] for k in sorted(self._gpus)])

    def classifications(self):
        with self._lock:
            return {key:self.service.classify_gpu(deepcopy(g)) for key,g in self._gpus.items()}

    def _refresh(self,keys):
        return {key:self.service.classify_gpu(deepcopy(self._gpus[key])) for key in sorted(keys)}

    def reserve(self,pod,selection):
        pod = deepcopy(parse_pod(pod))
        with self._lock:
            if pod.uid in self._held:
                old = self._held[pod.uid]
                if old["fingerprint"] != pod.fingerprint():
                    raise ReservationConflict("pod_uid_reused_with_different_request",False)
                return {"reservation_id":pod.uid,"status":"already_reserved","assignments":deepcopy(old["assignments"])}
            if not isinstance(selection,dict):
                raise ReservationConflict("missing_selection",False)
            check = deepcopy(selection)
            token = check.pop("selection_fingerprint",None)
            if token != digest(check) or selection.get("pod_fingerprint") != pod.fingerprint() or selection.get("model_version") != self.service.version:
                raise ReservationConflict("selection_request_or_model_mismatch",False)
            assignments = selection.get("assignments",[])
            expected = {(r.container,i):r for r in pod.requests for i in range(r.count)}
            if len(assignments) != len(expected) or {(a.get("container"),a.get("unit")) for a in assignments} != set(expected):
                raise ReservationConflict("incomplete_or_duplicate_plan",False)
            working = deepcopy(self._gpus)
            keys,per_container = set(),{}
            for a in assignments:
                key = a["gpu_uuid"]
                if key not in working or selection["expected_revisions"].get(key) != self._gpus[key].get("revision",0):
                    raise ReservationConflict("stale_or_missing_gpu_revision")
                g = working[key]
                if a["node"] != selection["node"] or g["node"] != selection["node"]:
                    raise ReservationConflict("cross_node_or_wrong_node_plan",False)
                seen = per_container.setdefault(a["container"],set())
                if key in seen:
                    raise ReservationConflict("native_container_requires_distinct_physical_GPUs",False)
                seen.add(key)
                request = expected[(a["container"],a["unit"])]
                errors,resolved = self.service.hard_filter(pod,g,request)
                if errors:
                    raise ReservationConflict("resource_revalidation_failed:"+",".join(errors))
                if any(a.get(k) != resolved[k] for k in ("memory_mib","compute_quota","instances")):
                    raise ReservationConflict("request_amount_changed_after_scoring",False)
                apply_slot(g,pod.task_type,a["memory_mib"],a["compute_quota"])
                keys.add(key)
            for key in keys:
                if pod.task_type:
                    field = "running_"+pod.task_type+"_pod_count"
                    working[key]["dynamic"][field] = working[key]["dynamic"].get(field,0)+1
                if gpu_errors(working[key]):
                    raise ReservationConflict("ledger_invariant_failure",False)
            # Publish all device changes atomically, after every slot passed.
            for key in keys:
                self._gpus[key] = working[key]
            self._held[pod.uid] = {"pod":pod,"fingerprint":pod.fingerprint(),"assignments":deepcopy(assignments),"state":"reserved","created":time.monotonic()}
            return {"reservation_id":pod.uid,"status":"reserved","assignments":deepcopy(assignments),"classifications_after":self._refresh(keys),
                    "requires_native_commit":True,"scope":"offline process-local mock ledger"}

    def commit(self,reservation_id,actual_gpu_uuids):
        with self._lock:
            if reservation_id not in self._held:
                raise ReservationConflict("reservation_not_found",False)
            held = self._held[reservation_id]
            expected = [a["gpu_uuid"] for a in held["assignments"]]
            if list(actual_gpu_uuids) != expected:
                self.release(reservation_id)
                raise ReservationConflict("native_uuid_mismatch_rolled_back")
            # Recheck actual health/plugin/constraints without double counting
            # this already-held reservation.
            working = deepcopy(self._gpus)
            for a in held["assignments"]:
                apply_slot(working[a["gpu_uuid"]],held["pod"].task_type,a["memory_mib"],a["compute_quota"],-1)
            requests = {r.container:r for r in held["pod"].requests}
            for a in held["assignments"]:
                g = working[a["gpu_uuid"]]
                errors,_ = self.service.hard_filter(held["pod"],g,requests[a["container"]])
                if errors:
                    self.release(reservation_id)
                    raise ReservationConflict("native_commit_revalidation_failed:"+",".join(errors))
                apply_slot(g,held["pod"].task_type,a["memory_mib"],a["compute_quota"])
            held["state"] = "committed"
            return {"reservation_id":reservation_id,"status":"committed","gpu_uuids":expected,"native_uuid_match":True}

    def release(self,reservation_id):
        with self._lock:
            held = self._held.pop(reservation_id,None)
            if held is None:
                return {"reservation_id":reservation_id,"released":False,"reason":"already_released_or_unknown"}
            keys = set()
            for a in held["assignments"]:
                key = a["gpu_uuid"]
                apply_slot(self._gpus[key],held["pod"].task_type,a["memory_mib"],a["compute_quota"],-1)
                keys.add(key)
            for key in keys:
                if held["pod"].task_type:
                    field = "running_"+held["pod"].task_type+"_pod_count"
                    self._gpus[key]["dynamic"][field] -= 1
                assert not gpu_errors(self._gpus[key])
            return {"reservation_id":reservation_id,"released":True,"classifications_after":self._refresh(keys)}

    rollback = release

    def reserve_best(self,pod,*,max_attempts=3):
        for attempt in range(max_attempts):
            ranking = self.service.rank_gpus(pod,self.snapshot())
            if ranking["recommendation"] is None:
                return {"status":ranking["status"],"ranking":ranking}
            try:
                return {"status":"reserved","ranking":ranking,"reservation":self.reserve(pod,ranking["recommendation"]),"attempts":attempt+1}
            except ReservationConflict as exc:
                if not exc.retryable:
                    raise
        return {"status":"retry_required","reason":"concurrent_revision_changes","attempts":max_attempts}

    def update_health(self,uuid,healthy):
        if not isinstance(healthy,bool):
            raise ValueError("health must be bool")
        with self._lock:
            self._gpus[uuid]["healthy"] = healthy
            self._gpus[uuid]["revision"] = self._gpus[uuid].get("revision",0)+1

    def expire(self,now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            keys = [key for key,r in self._held.items() if r["state"] == "reserved" and now-r["created"] > self.reservation_timeout_s]
            return [self.release(key) for key in keys]
