"""Reusable classify_gpu / rank_gpus service; pure advice, no reservation side effects."""
from copy import deepcopy
import json
import math
from pathlib import Path
import threading

from ..crossgpu.catalog import compile_catalog, REQUIRED
from ..crossgpu.features import SYNTHETIC_QUOTA
from ..crossgpu.theory import CONTRACT, NAMES, UNKNOWN
from ..nvidia.model import NvidiaModel
from ..profiles import ROOT
from ..run import sha256
from .bundle import DEFAULT_BUNDLE, SOURCE
from .pods import PodSpec, PodValidationError, parse_pod
from .schema import SCHEMA_VERSION, MAPPED_QUOTAS, apply_slot, device_for, digest, gpu_errors, model_state


class SuitabilityService:
    def __init__(self,bundle_dir=DEFAULT_BUNDLE,*,force_fallback=False):
        self.bundle_dir = Path(bundle_dir)
        self.model,self.model_error,self.catalog = None,None,{}
        self.manifest = {"model_version":"unavailable","label_source":"synthetic"}
        try:
            self.manifest = json.loads((self.bundle_dir/"manifest.json").read_text(encoding="utf-8"))
            for name,expected in self.manifest["artifact_sha256"].items():
                if sha256(self.bundle_dir/name) != expected:
                    raise ValueError(f"bundle_checksum_mismatch:{name}")
            self.catalog,_ = compile_catalog(self.bundle_dir/"source-records.json")
            self.model = NvidiaModel.load(self.bundle_dir/"random_forest.joblib")
            if self.model.metadata["feature_names"] != self.manifest["feature_order"]:
                raise ValueError("feature_order_mismatch")
            if force_fallback:
                self.model = None
                self.model_error = "forced_offline_failure_case"
        except Exception as exc:
            self.model = None
            self.model_error = f"{type(exc).__name__}:{exc}"
        if not self.catalog:
            try:
                self.catalog,_ = compile_catalog(SOURCE/"source-records.json")
            except (OSError,ValueError):
                pass
        try:
            if self.model_error and self.model_error != "forced_offline_failure_case":
                raise ValueError("Do not use an unverified bundle's scoring policy")
            self.policy = json.loads((self.bundle_dir/"scoring-policy.json").read_text(encoding="utf-8"))
            if set(self.policy["weights"]) != {"model_probability","hardware_sharing","packing","balance"}:
                raise ValueError("scoring_policy_shape_mismatch")
        except (OSError,ValueError,KeyError,TypeError):
            self.policy = json.loads((ROOT/"data/synthetic/scoring-policy.json").read_text(encoding="utf-8"))
        self._lock = threading.RLock()
        self.inference_calls = 0
        self._prediction_cache = {}

    @property
    def version(self):
        return self.manifest.get("model_version","unavailable")

    def classify_gpu(self,gpu_state):
        g = deepcopy(gpu_state)
        errors = gpu_errors(g)
        result = {"schema_version":SCHEMA_VERSION,"model_version":self.version,"gpu_uuid":g.get("uuid") if isinstance(g,dict) else None,
                  "node":g.get("node") if isinstance(g,dict) else None,"prediction_status":"invalid_input" if errors else "pending_verification",
                  "label":UNKNOWN,"label_zh":NAMES[UNKNOWN],"training_adapted":None,"inference_adapted":None,"raw_rf":None,
                  "reasons":errors,"model_load_error":self.model_error,"supervision":"synthetic","real_performance_guarantee":False,
                  "rf_probability_is_reliability":False,"resource_semantics":"scheduler reservation ledger, not observed GPU utilization"}
        if errors:
            return result
        result["input_sha256"] = digest(g)
        result["quota_semantics"] = g.get("quota_semantics")
        result["model_limitations"] = ["fixed dense-FP32 reference contracts only","hypothetical quota performance scaling",
                                      "RF instance counters differ from distinct Pod counters","health/backend/resource admission remains independent"]
        if g.get("quota_semantics") not in MAPPED_QUOTAS:
            result["reasons"] = ["unmapped_quota_semantics"]
            return result
        if self.model is None:
            result["reasons"] = ["model_unavailable_use_deterministic_ranking"]
            return result
        try:
            device,state = device_for(g,self.catalog),model_state(g)
            if g.get("model_memory_mapping"):
                result["model_feature_mapping"] = {"method":g["model_memory_mapping"],
                    "registered_logical_capacity_mib":g["logical_memory_capacity_mib"],
                    "catalog_reference_capacity_mib":g["hardware"]["memory_capacity_mib"],
                    "logical_free_memory_mib":g["dynamic"]["free_memory_mib"],
                    "model_reference_free_memory_mib":state["free_memory_mib"],
                    "admission_uses_original_logical_mib":True,"empirically_validated":False}
            cache_key = digest({"device":device.record,"state":state})
            with self._lock:
                prediction = deepcopy(self._prediction_cache.get(cache_key))
            if prediction is None:
                prediction = self.model.predict_device(device,state)
                with self._lock:
                    self.inference_calls += 1
                    if len(self._prediction_cache) >= 1024:
                        self._prediction_cache.pop(next(iter(self._prediction_cache)))
                    self._prediction_cache[cache_key] = deepcopy(prediction)
            result.update(label=prediction["label"],label_zh=prediction["label_zh"],training_adapted=prediction["training"],
                          inference_adapted=prediction["inference"],raw_rf=prediction["raw_rf"],reasons=prediction["reasons"],
                          validation_evidence=prediction.get("target_validation_evidence",{}),
                          related_validation_models=prediction.get("related_validation_models",[]),
                          outside_training_ranges=prediction.get("outside_training_ranges",{}),
                          prediction_status="provisional" if prediction["label"] != UNKNOWN else "pending_verification")
            if g["dynamic"].get("unrestricted_vgpu_count",0):
                result.update(label=UNKNOWN,label_zh=NAMES[UNKNOWN],training_adapted=None,inference_adapted=None,
                              prediction_status="pending_verification",reasons=["unrestricted_compute_allocations_not_covered_by_RF_quota_assumptions"])
            if g["dynamic"].get("running_unknown_count",0):
                result.update(label=UNKNOWN,label_zh=NAMES[UNKNOWN],training_adapted=None,inference_adapted=None,
                              prediction_status="pending_verification",reasons=["existing_unlabelled_allocations_have_unknown_task_composition"])
        except Exception as exc:
            result["reasons"] = [f"inference_failure:{type(exc).__name__}"]
        return result

    def hard_filter(self,pod,g,request):
        reasons = gpu_errors(g)
        if reasons:
            return reasons,None
        if g.get("backend") != pod.backend or pod.backend != "CUDA" or g.get("vendor","NVIDIA") != "NVIDIA":
            reasons.append("software_backend_incompatible")
        if g.get("healthy") is not True:
            reasons.append("gpu_not_healthy")
        if g.get("managed") is not True or g.get("plugin") not in ("volcano-vgpu","volcano-vgpu-device-plugin","hamivgpu"):
            reasons.append("gpu_not_managed_by_supported_plugin")
        if g.get("quota_semantics") not in MAPPED_QUOTAS or g.get("sharing_mode") != "hami-core":
            reasons.append("unsupported_allocation_semantics")
        d,mem = g["dynamic"],request.memory_for(g)
        if d["free_memory_mib"] < mem:
            reasons.append("insufficient_memory_mib")
        if d["free_compute_quota"] < request.compute_quota:
            reasons.append("insufficient_compute_quota")
        if d["allocated_vgpu_count"] >= g.get("max_instances",10):
            reasons.append("insufficient_instance_slots")
        if request.compute_quota == 100 and d["allocated_vgpu_count"] > 0:
            reasons.append("full_core_request_requires_empty_GPU")
        if request.compute_quota == 0 and d["free_compute_quota"] == 0:
            reasons.append("zero_core_request_cannot_share_full_core_GPU")
        if pod.node_name and pod.node_name != g["node"]:
            reasons.append("node_name_mismatch")
        if any(g.get("node_labels",{}).get(k) != v for k,v in pod.node_selector.items()):
            reasons.append("node_selector_mismatch")
        mode = pod.annotations.get("volcano.sh/vgpu-mode")
        if mode and mode != g.get("sharing_mode"):
            reasons.append("sharing_mode_mismatch")
        card = g.get("card_type",g.get("vendor","NVIDIA")+" "+g.get("model",g.get("device_id",""))).upper()
        include,exclude = pod.annotations.get("nvidia.com/use-gputype"),pod.annotations.get("nvidia.com/nouse-gputype")
        if include is not None:
            if not any(token.strip().upper() in card for token in include.split(",") if token.strip()):
                reasons.append("GPU_type_not_in_whitelist")
        elif exclude is not None and any(token.strip().upper() in card for token in exclude.split(",") if token.strip()):
            reasons.append("GPU_type_in_blacklist")
        return reasons,{"memory_mib":mem,"compute_quota":request.compute_quota,"instances":1}

    def _score(self,pod,g,request,strategy):
        reasons,resolved = self.hard_filter(pod,g,request)
        row = {"gpu_uuid":g.get("uuid"),"node":g.get("node"),"revision":g.get("revision",0),"feasible":not reasons,
               "rejection_reasons":reasons,"resolved_request":resolved,"score":None}
        if reasons:
            return row
        d,h = g["dynamic"],g["hardware"]
        post_mem = (d["free_memory_mib"]-resolved["memory_mib"])/g.get("logical_memory_capacity_mib",h["memory_capacity_mib"])
        post_compute = (d["free_compute_quota"]-request.compute_quota)/100
        residual = .5*post_mem+.4*post_compute+.1*(g.get("max_instances",10)-d["allocated_vgpu_count"]-1)/g.get("max_instances",10)
        packing,balance = 1-residual,1-abs(post_mem-post_compute)
        row.update(packing_residual=residual,components={"packing":packing,"balance":balance})
        if pod.task_type is None or strategy == "binpack":
            row.update(score=packing,ranking_basis="ordinary_resource_binpack",classification=None,rf_probability=None,fallback_score=None)
            return row
        snapshot = deepcopy(g)
        if strategy == "static_rf":
            snapshot["dynamic"] = {"free_memory_mib":h["memory_capacity_mib"],"free_compute_quota":100,"allocated_vgpu_count":0,
                                     "running_training_count":0,"running_inference_count":0}
        classification = self.classify_gpu(snapshot)
        raw = classification["raw_rf"]
        probability = raw[pod.task_type+"_vote_fraction"] if raw else None
        provisional = classification["prediction_status"] == "provisional" and request.compute_quota > 0
        if provisional:
            hint = probability if classification[pod.task_type+"_adapted"] else 0.
            basis,fallback = "RF_marginal_soft_hint",None
        else:
            # Conservative request-specific proxy; no empirical performance claim.
            fp,bw = h.get("fp32_tflops"),h.get("memory_bandwidth_gbps")
            q = min(d["free_compute_quota"],request.compute_quota)/100
            w = CONTRACT[pod.task_type]
            latency = max(w["work_tflop"]/(fp*.35*q*.8),w["traffic_gb"]/(bw*.55*q*.8)) if fp and bw and q else None
            fallback = min(1.,w["max_latency_s"]/latency) if latency else 0.
            hint,basis = fallback,"deterministic_conservative_proxy"
        params = self.policy[pod.task_type]
        fp,bw = h.get("fp32_tflops") or 0,h.get("memory_bandwidth_gbps") or 0
        hardware = params["compute_weight"]*min(fp/104.8,1)+(1-params["compute_weight"])*min(bw/1792,1)
        other = d["running_inference_count" if pod.task_type == "training" else "running_training_count"]
        hardware *= (.5+.5*request.compute_quota/100)/(1+params["per_neighbor"]*d["allocated_vgpu_count"]+params["per_opposite_type"]*other)
        components = {"adaptation_hint":hint,"hardware_sharing":hardware,"packing":packing,"balance":balance}
        weights = {"adaptation_hint":self.policy["weights"]["model_probability"],**{k:self.policy["weights"][k] for k in ("hardware_sharing","packing","balance")}}
        score = hint if strategy == "static_rf" else sum(weights[k]*v for k,v in components.items())
        row.update(score=score,components=components,component_weights=weights,classification=classification,ranking_basis=basis,
                   rf_probability=probability,rf_probability_is_reliability=False,fallback_score=fallback,
                   fallback_score_is_probability=False,reference_category_is_admission_gate=False)
        return row

    def rank_gpus(self,pod,gpu_list,*,strategy="dynamic_rf"):
        base = {"schema_version":SCHEMA_VERSION,"model_version":self.version,"recommendation":None,"advice_only":True,
                "scope":"GPU-only; native CPU/memory/affinity/taints and final device allocation remain authoritative"}
        try:
            pod = parse_pod(pod)
        except (PodValidationError,ValueError,TypeError) as exc:
            return {**base,"status":"invalid_input","errors":[str(exc)],"eligible_gpus":[],"rejected_gpus":[]}
        if strategy not in ("binpack","static_rf","dynamic_rf"):
            return {**base,"status":"invalid_input","errors":["unknown_strategy"]}
        if not pod.requests:
            return {**base,"status":"ordinary_scheduler_fallback","reason":pod.ordinary_reason,"eligible_gpus":[],"rejected_gpus":[]}
        if not isinstance(gpu_list,(list,tuple)):
            return {**base,"status":"invalid_input","errors":["gpu_list_must_be_list"],"eligible_gpus":[],"rejected_gpus":[]}
        views = deepcopy(list(gpu_list))
        ids = [g.get("uuid") for g in views if isinstance(g,dict)]
        if len(ids) != len(views) or len(set(ids)) != len(ids):
            return {**base,"status":"invalid_input","errors":["duplicate_or_invalid_gpu_uuid"],"eligible_gpus":[],"rejected_gpus":[]}
        evaluations = []
        for req in pod.requests:
            rows = [self._score(pod,g,req,strategy) for g in views]
            evaluations.append({"container":req.container,"count":req.count,"devices":rows})
        first = evaluations[0]["devices"]
        plans = []
        for node in sorted({g["node"] for g in views if not gpu_errors(g)}):
            working = {g["uuid"]:deepcopy(g) for g in views if g.get("node") == node}
            assignments,complete = [],True
            for req in pod.requests:
                used = set()
                for unit in range(req.count):
                    rows = [self._score(pod,g,req,strategy) for key,g in working.items() if key not in used]
                    candidates = [r for r in rows if r["feasible"]]
                    if not candidates:
                        complete = False
                        break
                    chosen = min(candidates,key=lambda r:(-r["score"],r["packing_residual"],r["gpu_uuid"]))
                    key,res = chosen["gpu_uuid"],chosen["resolved_request"]
                    used.add(key)
                    assignments.append({"container":req.container,"unit":unit,"gpu_uuid":key,"node":node,**res,
                                        "score":chosen["score"],"ranking_basis":chosen["ranking_basis"]})
                    apply_slot(working[key],pod.task_type,res["memory_mib"],res["compute_quota"])
                if not complete:
                    break
            if complete:
                plans.append({"node":node,"assignments":assignments,"score":sum(a["score"] for a in assignments)/len(assignments)})
        eligible = sorted([r for r in first if r["feasible"]],key=lambda r:(-r["score"],r["packing_residual"],r["gpu_uuid"]))
        result = {**base,"status":"ranked" if plans else "unschedulable","task_type":pod.task_type,
                  "pod_uid":pod.uid,"pod_fingerprint":pod.fingerprint(),"strategy":"ordinary_binpack" if pod.task_type is None else strategy,
                  "warnings":list(pod.warnings),"fallback_reason":pod.ordinary_reason,"eligible_gpus":eligible,
                  "rejected_gpus":[r for r in first if not r["feasible"]],"per_container_evaluations":evaluations,
                  "candidate_scope":"individual feasibility for first container; complete same-node placement required below","node_plans":plans}
        if plans:
            best = min(plans,key=lambda p:(-p["score"],p["node"],tuple(a["gpu_uuid"] for a in p["assignments"])))
            selected_ids = {a["gpu_uuid"] for a in best["assignments"]}
            versions = {g["uuid"]:g.get("revision",0) for g in views if g["uuid"] in selected_ids}
            rec = {**best,"gpu_uuid":best["assignments"][0]["gpu_uuid"] if len(best["assignments"]) == 1 else None,
                   "gpu_uuids":[a["gpu_uuid"] for a in best["assignments"]],"expected_revisions":versions,
                   "pod_fingerprint":pod.fingerprint(),"model_version":self.version,
                   "reason":"all hard filters passed; maximum explainable node placement score, stable UUID tie-break",
                   "requires_locked_revalidation":True}
            rec["selection_fingerprint"] = digest(rec)
            result["recommendation"] = rec
        else:
            result["reason"] = "no_complete_single_node_placement"
        return result


_default,_default_lock = None,threading.Lock()


def default_service():
    global _default
    with _default_lock:
        if _default is None:
            _default = SuitabilityService()
    return _default
