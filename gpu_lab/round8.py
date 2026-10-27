"""Frozen RF experiments and pending-approval manifests. CPU/offline only.

No cluster client, subprocess deployment, model fitting or policy editing here.
The process-local GPUPool is a fixture, never the real Volcano ledger.
"""
from copy import deepcopy
from pathlib import Path
import argparse
import csv
import hashlib
import json

import yaml

from .profiles import ROOT
from .runtime.pool import GPUPool
from .runtime.schema import gpu_errors, model_state
from .runtime.service import SuitabilityService
from .runtime.volcano import VolcanoRFAdapter

OUT = ROOT / "results/round8-20261020"
PLAN = ROOT / "k8s/round8-pending-approval"
MODEL_SHA = "ee35cfabe30fffc28ff0be09d3738587d4f9bffbe26386e2a9fae5c5b9e80748"
IMAGE = "docker.io/pytorch/pytorch@sha256:923f687790bec78081c357e71dcd5dcef80b0cc00f6c34484902a5e83362c854"
CLASS_ZH = {"balanced":"训推一体", "training_preferred":"训练优先", "inference_preferred":"推理优先",
            "unavailable":"不满足参考负载", "pending_verification":"待验证"}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf-8")


def write_csv(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        raise ValueError("Refuse an empty evidence table")
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows({k: json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v for k,v in r.items()} for r in rows)


def raw_4090(uuid="SIM-round8-4090-0"):
    return dict(uuid=uuid,node="t3dgq",model="NVIDIA-NVIDIA GeForce RTX 4090",registered_memory_mib=24564,
                allocated_memory_mib=0,allocated_compute_quota=0,allocated_vgpu_count=0,training_vgpu_count=0,
                inference_vgpu_count=0,training_pod_count=0,inference_pod_count=0,max_instances=10,healthy=True,
                sharing_mode="hami-core")


def fixture_gpu(service,uuid="SIM-round8-4090-0"):
    g = VolcanoRFAdapter(service).snapshot(raw_4090(uuid))
    # Node labels are outside the per-GPU transport snapshot. Supply the
    # explicit, historical node selector in this offline fixture only.
    g["node_labels"] = {"kubernetes.io/hostname":"t3dgq"}
    return g


def pod(name,task,memory,cores, *, live=False):
    resources = {"volcano.sh/vgpu-number":"1","volcano.sh/vgpu-memory":str(memory),"volcano.sh/vgpu-cores":str(cores)}
    p = {"apiVersion":"v1","kind":"Pod","metadata":{"name":name,"namespace":"gpu-rf-tests",
         "labels":{"gpu-lab/experiment":"round8-dynamic","gpu-lab/workload-type":task},
         "annotations":{"scheduling.k8s.io/group-name":name}},
         "spec":{"schedulerName":"volcano-rf-test","runtimeClassName":"nvidia","restartPolicy":"Never",
                 "automountServiceAccountToken":False,"activeDeadlineSeconds":900,"terminationGracePeriodSeconds":5,
                 "nodeSelector":{"kubernetes.io/hostname":"t3dgq"},
                 "tolerations":[{"key":k,"operator":"Exists","effect":"NoSchedule"} for k in
                                ("node-role.kubernetes.io/control-plane","node-role.kubernetes.io/master")],
                 "containers":[{"name":"sleep","image":IMAGE,"imagePullPolicy":"Never","command":["/bin/sleep","600"],
                                 "resources":{"requests":{**resources,"cpu":"100m","memory":"128Mi"},
                                              "limits":{**resources,"cpu":"500m","memory":"256Mi"}},
                                 "securityContext":{"allowPrivilegeEscalation":False,"capabilities":{"drop":["ALL"]}}}]}}
    if not live:
        p["metadata"]["uid"] = "SIM-"+name
    return p


def allocate(pool,p):
    advice = pool.service.rank_gpus(p,pool.snapshot())
    if advice["recommendation"] is None:
        raise AssertionError(advice)
    reservation = pool.reserve(p,advice["recommendation"])
    # This is ONLY the Python fixture's commit, not a native Volcano Allocate.
    pool.commit(p["metadata"]["uid"],[a["gpu_uuid"] for a in reservation["assignments"]])
    return {"pod":p,"advice":advice,"reservation":reservation,"native_allocate_uuid":None,
            "device_plugin_consumed_uuid":None,"scope":"offline Python ledger; synthetic UUID/UID"}


def state_record(service,pool,stage):
    g = pool.snapshot()[0]
    c = service.classify_gpu(g)
    rows = {}
    for task in ("training","inference"):
        ranking = service.rank_gpus(pod("diagnostic-"+stage+task,task,64,1),[g])
        rows[task] = ranking
    return {"stage":stage,"gpu":g,"model_features":model_state(g),"classification":c,"probe_scores":rows,
            "data_kind":"synthetic_resource_state_frozen_RF_prediction","real_cluster_executed":False}


def run_path(service):
    g = fixture_gpu(service)
    pool = GPUPool(service,[g])
    states = [state_record(service,pool,"initial")]
    requests = [pod("rf-v8-compute","training",1000,60),pod("rf-v8-memory","inference",17564,5)]
    events = []
    for p,stage in zip(requests,("after_compute","after_memory")):
        events.append(allocate(pool,p))
        states.append(state_record(service,pool,stage))
        resource=p["spec"]["containers"][0]["resources"]["limits"]
        states[-1]["action"]={"kind":"allocate","pod_uid":p["metadata"]["uid"],
            "task_type":p["metadata"]["labels"]["gpu-lab/workload-type"],
            "allocated_memory_delta_mib":int(resource["volcano.sh/vgpu-memory"]),
            "allocated_core_delta":int(resource["volcano.sh/vgpu-cores"])}
    for p,stage in zip(requests,("release_compute","release_all")):
        events.append(pool.release(p["metadata"]["uid"]))
        states.append(state_record(service,pool,stage))
        resource=p["spec"]["containers"][0]["resources"]["limits"]
        states[-1]["action"]={"kind":"release","pod_uid":p["metadata"]["uid"],
            "task_type":p["metadata"]["labels"]["gpu-lab/workload-type"],
            "allocated_memory_delta_mib":-int(resource["volcano.sh/vgpu-memory"]),
            "allocated_core_delta":-int(resource["volcano.sh/vgpu-cores"])}
    assert states[0]["gpu"]["dynamic"] == states[-1]["gpu"]["dynamic"]
    assert all(not gpu_errors(s["gpu"]) for s in states)
    return {"states":states,"events":events,"ledger_restored":True,"native_allocate_tested_this_round":False}


def flat_state(s):
    c,g = s["classification"],s["gpu"]
    row = {"stage":s["stage"],"uuid":g["uuid"],"action":s.get("action",{"kind":"initial"}),**g["dynamic"],"model_reference_free_memory_mib":s["model_features"]["free_memory_mib"],
           "raw_label":c["raw_rf"]["label"],"label":c["label"],"label_zh":CLASS_ZH[c["label"]],
           "training_adapted":c["training_adapted"],"inference_adapted":c["inference_adapted"],
           "prediction_status":c["prediction_status"],"raw_rf":c["raw_rf"],"reasons":c["reasons"],"model_version":c["model_version"],
           "data_kind":s["data_kind"],"native_allocate_uuid":None,"plugin_consumed_uuid":None,
           "score_scope":"same hypothetical 64 MiB /1 core diagnostic Pod, not reference performance or primary Pod score"}
    for task,r in s["probe_scores"].items():
        score = r["eligible_gpus"][0]
        row.update({task+"_score":score["score"],task+"_rf_contribution":score["score_decomposition"]["applied_rf_contribution"],
                    task+"_heuristic_contribution":score["score_decomposition"]["heuristic_contribution"]})
    return row


def run_controlled_fixture(service):
    """Prove conditional placement feasibility, not RF's causal benefit."""
    gpus = [fixture_gpu(service,f"SIM-round8-4090-{i}") for i in range(4)]
    pool = GPUPool(service,gpus)
    events = []
    for i in range(3):
        events.append(allocate(pool,pod(f"rf-v8-blocker-{i}","training",64,100)))
    blocked = [e["advice"]["recommendation"]["gpu_uuid"] for e in events]
    assert len(set(blocked)) == 3
    target = next(g["uuid"] for g in pool.snapshot() if g["dynamic"]["free_compute_quota"] == 100)
    steps = []
    def capture(stage,probe=None):
        view = next(g for g in pool.snapshot() if g["uuid"] == target)
        steps.append({"stage":stage,"gpu":view,"classification":service.classify_gpu(view),"observer_event":probe})
    capture("initial")
    a,b = pod("rf-v8-compute","training",1000,60),pod("rf-v8-memory","inference",17564,5)
    for p,stage in ((a,"after_compute"),(b,"after_memory")):
        event = allocate(pool,p)
        assert event["advice"]["recommendation"]["gpu_uuid"] == target
        assert len(event["advice"]["eligible_gpus"]) == 1
        events.append(event)
        capture(stage)
    for index,stage in enumerate(("after_memory","release_compute","release_all")):
        if index:
            events.append(pool.release((a,b)[index-1]["metadata"]["uid"]))
            capture(stage)
        before = pool.snapshot()
        probe = pod(f"rf-v8-observer-{index}","inference",64,1)
        event = allocate(pool,probe)
        assert event["advice"]["recommendation"]["gpu_uuid"] == target
        capture(stage+"_observer_allocated",event)
        released = pool.release(probe["metadata"]["uid"])
        # Revisions legitimately increase; compare resources and counters.
        assert [g["dynamic"] for g in before] == [g["dynamic"] for g in pool.snapshot()]
        events.extend([event,released])
    for i in range(3):
        events.append(pool.release("SIM-rf-v8-blocker-"+str(i)))
    assert all(g["dynamic"]["free_memory_mib"] == 24564 and g["dynamic"]["free_compute_quota"] == 100
               and g["dynamic"]["allocated_vgpu_count"] == 0 for g in pool.snapshot())
    return {"target_uuid":target,"blocker_uuids":blocked,"steps":steps,"events":events,"all_restored":True,
            "evidence_kind":"Python fixture using native-compatible hard rules; not new Go or cluster execution",
            "placement_reason":"unique hard-fit device after full-core blockers; not independent RF UUID benefit"}


def phase_grid(service):
    rows = []
    for mem in sorted(set(range(1000,24565,1000))|{4096,6000,7000,8192,24564}):
        for q in range(5,101,5):
            raw = raw_4090()
            raw.update(allocated_memory_mib=24564-mem,allocated_compute_quota=100-q,
                       allocated_vgpu_count=1,training_vgpu_count=1,training_pod_count=1,
                       unrestricted_vgpu_count=1 if q==100 else 0)
            g = VolcanoRFAdapter(service).snapshot(raw)
            c = service.classify_gpu(g)
            rows.append({"free_memory_mib":mem,"free_compute_quota":q,"allocated_vgpu_count":1,
                         "running_training_count":1,"running_inference_count":0,"label":c["label"],
                         "raw_label":c["raw_rf"]["label"],"prediction_status":c["prediction_status"],
                         "training_adapted":c["training_adapted"],"inference_adapted":c["inference_adapted"],
                         "unrestricted_vgpu_count":raw["unrestricted_vgpu_count"],"reasons":c["reasons"],
                         "data_kind":"synthetic_grid_frozen_RF"})
    return rows


def prepare_manifests():
    PLAN.mkdir(parents=True,exist_ok=True)
    requests = [(f"blocker-{i}","training",64,100) for i in range(3)] + [
        ("compute","training",1000,60),("memory","inference",17564,5)] + [
        (f"observer-{i}","inference",64,1) for i in range(3)]
    for name,task,mem,cores in requests:
        p = pod("rf-v8-"+name,task,mem,cores,live=True)
        pg = {"apiVersion":"scheduling.volcano.sh/v1beta1","kind":"PodGroup",
              "metadata":{"name":p["metadata"]["name"],"namespace":"gpu-rf-tests","labels":{"gpu-lab/experiment":"round8-dynamic"}},
              "spec":{"minMember":1,"queue":"rf-v7-queue"}}
        (PLAN/(name+".yaml")).write_text("# PENDING APPROVAL. Apply sequentially; never apply this whole directory.\n"+
                                           yaml.safe_dump_all([pg,p],sort_keys=False),encoding="utf-8")
    # Exact simultaneous peak: 3 blockers + compute + memory + ONE observer.
    write_json(PLAN/"quota-spec-merge-patch.json",{"spec":{"hard":{"pods":"6","requests.volcano.sh/vgpu-number":"6",
               "requests.volcano.sh/vgpu-memory":"18820","requests.volcano.sh/vgpu-cores":"366"}}})
    write_json(PLAN/"execution-gate.json",{"approved":False,"real_cluster_operations_allowed":False,
        "target_node":"t3dgq","target_uuid":"discover unique unblocked UUID; never pre-write assignment annotations",
        "max_simultaneous_pods":6,"peak_memory_mib":18820,"peak_compute_quota":366,
        "needs_new_authorization":["coordinated submission window","backup and temporary quota increase",
          "temporary stop original scheduler","start existing RF deployment","3 full-core logical blockers",
          "compute/memory/observer sleep Pods and own cleanup","restore original scheduler and quota"],
        "not_authorized_this_round":True})


def run(out=OUT,grid=True):
    out = Path(out)
    bundle = ROOT/"models/pod-gpu-rf-v1"
    before = {p.name:sha(p) for p in bundle.iterdir() if p.is_file()}
    assert before["random_forest.joblib"] == MODEL_SHA
    service = SuitabilityService()
    assert service.model is not None, service.model_error
    path = run_path(service)
    write_json(out/"dynamic-path.json",path)
    write_csv(out/"dynamic-path.csv",[flat_state(s) for s in path["states"]])
    controlled = run_controlled_fixture(service)
    write_json(out/"same-gpu-control-fixture.json",controlled)
    if grid:
        write_csv(out/"phase-grid.csv",phase_grid(service))
    prepare_manifests()
    after = {p.name:sha(p) for p in bundle.iterdir() if p.is_file()}
    assert before == after
    write_json(out/"frozen-artifact-audit.json",{"model_version":service.version,"before":before,"after":after,
               "identical":True,"trained":False,"network_or_cluster_access":False,
               "mapping":path["states"][0]["classification"]["model_feature_mapping"]})
    print(json.dumps({"output":str(out),"classes":[s["classification"]["label"] for s in path["states"]],
                      "control_restored":controlled["all_restored"],"model_unchanged":before==after},ensure_ascii=False))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output",type=Path,default=OUT)
    p.add_argument("--skip-grid",action="store_true")
    args = p.parse_args()
    run(args.output,not args.skip_grid)
