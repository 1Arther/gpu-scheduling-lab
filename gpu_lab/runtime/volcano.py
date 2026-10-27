"""rf-job/v1 transport adapter. Go owns ALL reservation and release operations.

Static capabilities come from the frozen, audited catalog. Dynamic memory comes
only from registered logical capacity minus allocated quota, never NVML.
Legacy RF task counters count vGPU allocations; distinct Pod counts are separate.
"""
from copy import deepcopy
from dataclasses import replace
import math
import re

from .pods import parse_pod
from .schema import HAMI_QUOTA,MEMORY_REFERENCE_MAPPING

RF_SCHEMA = "rf-job/v1"


def _model_key(value):
    text = str(value).upper()
    text = re.sub(r"\bNVIDIA\b|\bGEFORCE\b", "", text)
    return re.sub(r"[^A-Z0-9]", "", text)


class VolcanoRFAdapter:
    def __init__(self,service):
        self.service = service
        self.model_keys = {}
        for key,device in service.catalog.items():
            normalized = _model_key(device.record["model"])
            # Ambiguous PCIe/SXM variants are deliberately NOT guessed.
            self.model_keys.setdefault(normalized,[]).append(key)

    def snapshot(self,raw):
        required = ("registered_memory_mib","allocated_memory_mib","allocated_compute_quota","allocated_vgpu_count",
                    "training_vgpu_count","inference_vgpu_count","training_pod_count","inference_pod_count","max_instances")
        if not isinstance(raw,dict):raise ValueError("gpu must be a mapping")
        for field in required:
            v = raw.get(field)
            if isinstance(v,bool) or not isinstance(v,int) or v < 0:raise ValueError("invalid_gpu_field:"+field)
        cap,used = raw["registered_memory_mib"],raw["allocated_memory_mib"]
        if not cap or used > cap or raw["allocated_compute_quota"] > 100:raise ValueError("invalid_logical_ledger")
        typed = raw["training_vgpu_count"]+raw["inference_vgpu_count"]
        if typed > raw["allocated_vgpu_count"]:raise ValueError("task_counts_exceed_allocations")
        u=raw.get("unrestricted_vgpu_count",0)
        if isinstance(u,bool) or not isinstance(u,int) or not 0<=u<=raw["allocated_vgpu_count"]:
            raise ValueError("invalid_unrestricted_count")
        if raw["training_pod_count"]>raw["training_vgpu_count"] or raw["inference_pod_count"]>raw["inference_vgpu_count"]:
            raise ValueError("distinct_pod_count_exceeds_vgpu_count")
        matches = self.model_keys.get(_model_key(raw.get("model","")),[])
        device_id = matches[0] if len(matches) == 1 else "unknown_nvidia_device"
        hardware = self.service.catalog[device_id].values() if device_id in self.service.catalog else {
            "memory_capacity_mib":cap,"memory_bandwidth_gbps":None,"fp32_tflops":None,"fp16_dense_tflops":None,
            "bf16_dense_tflops":None,"int8_dense_tops":None,"max_board_power_w":None}
        if raw.get("hardware") is not None and raw["hardware"] != hardware:
            raise ValueError("static_hardware_not_equal_to_pinned_catalog")
        # Memory oversubscription/mismatched variants must not be disguised by
        # reference normalization. This narrow tolerance covers nominal-GB vs
        # binary-MiB and driver reservation differences in the frozen catalog.
        if device_id in self.service.catalog and not .85 <= cap/hardware["memory_capacity_mib"] <= 1.15:
            raise ValueError("registered_capacity_not_compatible_with_catalog_variant")
        return {"uuid":raw.get("uuid"),"node":raw.get("node"),"model":raw.get("model"),"card_type":raw.get("model"),
                "device_id":device_id,"vendor":"NVIDIA","backend":"CUDA","plugin":"volcano-vgpu",
                "healthy":raw.get("healthy") is True,"managed":True,"sharing_mode":raw.get("sharing_mode","hami-core"),
                "hardware":deepcopy(hardware),"logical_memory_capacity_mib":cap,"max_instances":raw["max_instances"],
                "model_memory_mapping":MEMORY_REFERENCE_MAPPING,
                "quota_semantics":HAMI_QUOTA,"revision":0,"node_labels":{},
                "dynamic":{"free_memory_mib":cap-used,"free_compute_quota":100-raw["allocated_compute_quota"],
                           "allocated_vgpu_count":raw["allocated_vgpu_count"],"running_training_count":raw["training_vgpu_count"],
                           "running_inference_count":raw["inference_vgpu_count"],"running_unknown_count":raw["allocated_vgpu_count"]-typed,
                           "running_training_pod_count":raw["training_pod_count"],"running_inference_pod_count":raw["inference_pod_count"],
                           "unrestricted_vgpu_count":raw.get("unrestricted_vgpu_count",0)},
                "resource_source":"Volcano logical reservation ledger; no NVML measurements",
                "hardware_source":"frozen catalog" if device_id in self.service.catalog else "unknown SKU; registered capacity only"}

    def rank(self,payload):
        base={"schema_version":RF_SCHEMA,"model_version":self.service.version,"supervision":"synthetic",
              "logical_allocation_only":True,"real_cuda_success_guarantee":False,"request_id":payload.get("request_id"),
              "snapshot_token":payload.get("snapshot_token"),"assignments":[],"score":0.0}
        try:
            if payload.get("schema_version") != RF_SCHEMA:raise ValueError("schema_version_mismatch")
            for key in ("request_id","snapshot_token"):
                if not isinstance(payload.get(key),str) or not payload[key]:raise ValueError("missing_"+key)
            if payload.get("model_version") != self.service.version:raise ValueError("model_version_mismatch")
            if payload.get("memory_unit_mib") != 1:raise ValueError("MVP_requires_one_MiB_per_memory_unit")
            enabled=payload.get("rf_prediction_enabled",True)
            if not isinstance(enabled,bool):raise ValueError("rf_prediction_enabled_must_be_boolean")
            base['rf_prediction_enabled']=enabled
            pod=parse_pod(payload.get("pod"))
            gpus=[self.snapshot(g) for g in payload.get("gpus",[])]
            if len({g["node"] for g in gpus}) > 1:raise ValueError("one_node_per_advice_request")
            result=self.service.rank_gpus(pod,gpus,rf_prediction_enabled=enabled)
            rec=result.get("recommendation")
            if result["status"]=="invalid_input":raise ValueError(";".join(result.get("errors",[])))
            classifications=[]
            for g in gpus:
                scores={task:self.service._score(replace(pod,task_type=task),g,pod.requests[0],"dynamic_rf",rf_prediction_enabled=enabled)
                        for task in ("training","inference")} if pod.requests else {}
                classifications.append({"gpu_uuid":g["uuid"],"snapshot":g,"classification":self.service.classify_gpu(g),
                                        "request_specific_scores":scores,"score_scope":"first requested container, logical resources"})
            if rec is None:return {**base,"status":"unschedulable","classifications":classifications,"details":result}
            score=float(rec["score"])
            if not math.isfinite(score) or not 0 <= score <= 1:raise ValueError("score_out_of_range")
            return {**base,"status":"ranked","score":100*score,"assignments":rec["assignments"],
                    "classifications":classifications,"details":result,"model_ready":self.service.model is not None}
        except (ValueError,TypeError,KeyError) as exc:
            return {**base,"status":"invalid_input","errors":[str(exc)]}
