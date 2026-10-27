"""Regenerate thesis tables/figures from preserved evidence, without rerunning ML.

CSV is emitted as scientific experiment data; plots are standalone PNG/SVG.
All source files are indexed with checksums. No cluster/network access.
"""
from collections import Counter, defaultdict
from pathlib import Path
import argparse
import csv
import json
import math
import os
import platform
import statistics
import subprocess

# Keep Matplotlib caches in the workspace, including restricted desktop runs.
_cache=Path(__file__).resolve().parents[1]/".tmp/matplotlib-round8"
_cache.mkdir(parents=True,exist_ok=True)
os.environ.setdefault("MPLCONFIGDIR",str(_cache))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
import numpy as np
import sklearn

from .profiles import ROOT
from .round8 import OUT, CLASS_ZH, sha, write_csv, write_json

LABELS = ["training_preferred","inference_preferred","balanced","unavailable"]
COLORS = {"training_preferred":"#427ab3","inference_preferred":"#e69636","balanced":"#3b9d84","unavailable":"#8b8f98","pending_verification":"#af73b2"}
PROTOCOLS = ["known_states","A_ampere","A_ada","B_multi_architecture","C_blackwell_holdout","D_a10_diagnostic"]
PZH = ["已知型号新状态","同架构 Ampere","同架构 Ada","多架构未见型号","整架构 Blackwell 留出","A10 历史诊断"]


class Evidence:
    def __init__(self):
        self.files = {}

    def source(self,path,kind):
        p = ROOT/path
        self.files[path] = {"path":path,"sha256":sha(p),"evidence_kind":kind,"bytes":p.stat().st_size}
        return p

    def json(self,path,kind):
        return json.loads(self.source(path,kind).read_text(encoding="utf-8"))

    def csv(self,path,kind):
        with self.source(path,kind).open(encoding="utf-8-sig",newline="") as f:
            return list(csv.DictReader(f))


def save_figure(fig,path):
    fig.savefig(str(path)+".png",dpi=190,bbox_inches="tight",facecolor="white")
    fig.savefig(str(path)+".svg",bbox_inches="tight",facecolor="white")
    plt.close(fig)


def configure_plots():
    plt.rcParams.update({"font.family":"sans-serif","font.sans-serif":["Microsoft YaHei","Noto Sans CJK SC","DejaVu Sans"],
                         "axes.unicode_minus":False,"svg.fonttype":"path","font.size":10,
                         "axes.spines.top":False,"axes.spines.right":False})


def model_tables(e,out):
    rows,perclass,splits,permodel,ablation = [],[],[],[],[]
    def add(round_id,protocol,split,m,source):
        row = {"round":round_id,"protocol":protocol,"split":split,"samples":m["samples"],"accuracy":m["accuracy"],
               "macro_f1_fixed_four":m["macro_f1"],"macro_f1_observed":m.get("macro_f1_observed_classes"),
               "class_counts":m.get("class_counts"),"truth_kind":"synthetic_theory_labels", "source":source}
        rows.append(row)
        pc = m.get("per_class_precision_recall_f1_support",m.get("per_class",{}))
        for label in LABELS:
            v = pc[label]
            perclass.append({"round":round_id,"protocol":protocol,"split":split,"label":label,
                             "precision":v["precision"],"recall":v["recall"],"f1":v["f1-score"],"support":v["support"],"source":source})
    p = "results/offline-v1/metrics.json"
    v1 = e.json(p,"synthetic_classifier_evaluation")
    for split,m in v1["classification"].items():add(1,"known_states_legacy",split,m,p)
    audit=v1["split_audit"]
    splits.append({"round":1,"protocol":"known_states_legacy","passed":audit["passed"],"model_disjoint":False,
                   "sizes":audit["sizes"],"all_overlap_counts_zero":all(v==0 for v in audit["overlaps"].values()),
                   "model_groups":"same 3 profiles; episode split"})
    v2=e.csv("results/offline-v2/classification-comparison.csv","synthetic_rule_fitting_comparison")
    write_csv(out/"round2-classifier-baselines.csv",v2)
    for proto in ["A-known-models","B-unseen-models"]:
        p = f"results/offline-v3-release/{proto}-metrics.json"
        d = e.json(p,"synthetic_classifier_evaluation")
        for split in ("train","validation","test"):add(3,proto,split,d[split],p)
        manifest = e.json(f"results/offline-v3-release/{proto}-split-manifest.json","split_audit")
        splits.append({"round":3,"protocol":proto,"passed":manifest["passed"],"model_disjoint":manifest["model_disjoint"],
                       "sizes":manifest["sizes"],"all_overlap_counts_zero":all(v==0 for v in manifest["overlaps"].values()),
                       "model_groups":manifest["model_groups"]})
    manifests = e.json("results/offline-v4/split-manifests.json","split_audit")
    v4 = {}
    for proto in PROTOCOLS:
        p = f"results/offline-v4/{proto}/metrics.json"
        d = e.json(p,"synthetic_classifier_evaluation")
        v4[proto] = d
        for split,m in d["selected_metrics"].items():
            add(4,proto,split,m,p)
            rows[-1].update(selected_features=d["selected_feature_set"],
                accepted_test_fraction=d["guarded_test"]["accepted_state_fraction"] if split=="test" else None,
                accepted_test_errors=d["guarded_test"]["accepted_guarded_errors"] if split=="test" else None)
        m = manifests[proto]
        splits.append({"round":4,"protocol":proto,"passed":m["passed"],"model_disjoint":m["model_disjoint"],
                       "sizes":m["sizes"],"all_overlap_counts_zero":all(v==0 for k,v in m["overlaps"].items() if not k.endswith("model_group")),
                       "model_groups":m["model_groups"],"device_ids":m["device_ids"]})
        permodel.extend({**r,"source":p,"truth_kind":"synthetic_theory_labels"} for r in d["per_model"])
        ablation.extend(e.csv(f"results/offline-v4/{proto}/ablations.csv","validation_selected_feature_ablation"))
    assert all(s["passed"] and s["all_overlap_counts_zero"] for s in splits)
    write_csv(out/"classification-all-splits.csv",rows)
    write_csv(out/"classification-per-class.csv",perclass)
    write_csv(out/"classification-per-model.csv",permodel)
    write_csv(out/"split-audit.csv",splits)
    write_csv(out/"feature-ablations.csv",ablation)
    return rows,v4,ablation,permodel


def simulation_tables(e,out):
    source="results/offline-v2/simulation-metrics.csv"
    raw=e.csv(source,"discrete_event_simulation_synthetic_execution_model")
    published=e.csv("results/offline-v2/simulation-summary.csv","simulation_aggregated_history")
    groups=defaultdict(list)
    traces=defaultdict(set)
    for r in raw:
        if float(r["interference"]) != 1.0:continue
        groups[(r["scenario"],r["strategy"])].append(r)
        traces[(r["scenario"],r["trace_seed"])].add(r["trace_sha256"])
    assert all(len(s)==1 for s in traces.values())
    paired=defaultdict(list)
    for runs in groups.values():
        for r in runs:paired[(r["scenario"],r["trace_seed"])].append(r)
    for runs in paired.values():
        assert {r["strategy"] for r in runs}=={"binpack","static_rf","dynamic_rf"}
        assert len(runs)==3
        for key in ("execution_seed","observation_horizon_s","submitted_pods","trace_sha256"):
            assert len({r[key] for r in runs})==1,(key,runs)
    fields=["mean_wait_s","training_mean_jct_s","inference_pod_response_p95_s","reserved_compute_utilization",
            "reserved_memory_utilization","completed_within_horizon","completed_total_after_drain","rejected_pods"]
    rows=[]
    for (scenario,strategy),runs in sorted(groups.items()):
        assert len(runs)==3
        record={"scenario":scenario,"strategy":strategy,"seeds":3,"interference":1.0,"source":source,
                "data_kind":"synthetic_DES","interval_kind":"sample_std_not_confidence_interval"}
        history=next(r for r in published if r["scenario"]==scenario and r["strategy"]==strategy and float(r["interference"])==1.)
        for f in fields:
            values=[float(r[f]) for r in runs]
            record[f+"_mean"]=statistics.mean(values)
            record[f+"_std"]=statistics.stdev(values)
            assert math.isclose(record[f+"_mean"],float(history[f+"_mean"]),rel_tol=1e-10,abs_tol=1e-10)
        rows.append(record)
    write_csv(out/"simulation-main.csv",rows)
    comparisons=[]
    for scenario in sorted({r["scenario"] for r in rows}):
        b=next(r for r in rows if r["scenario"]==scenario and r["strategy"]=="binpack")
        d=next(r for r in rows if r["scenario"]==scenario and r["strategy"]=="dynamic_rf")
        comparisons.append({"scenario":scenario,**{f+"_relative_reduction":(b[f+"_mean"]-d[f+"_mean"])/b[f+"_mean"] for f in fields[:3]},
                            "interpretation":"synthetic scenario only; negative = worse; three paired traces; no significance claim"})
    write_csv(out/"simulation-relative-changes.csv",comparisons)
    return rows,comparisons


def native_and_real(e,out):
    decisions=e.csv("results/rf-ablation-20261019/linux-final/decisions.csv","real_Go_fake_client_frozen_RF_HTTP")
    summary=e.json("results/rf-ablation-20261019/linux-final/summary.json","real_Go_fake_client_frozen_RF_HTTP")
    families=[]
    for family in sorted({r["family"] for r in decisions}):
        subset=[r for r in decisions if r["family"]==family]
        families.append({"family":family,"cases":len(subset),**{k:sum(r[k]=="True" for r in subset) for k in
                        ("new_strategy_vs_binpack_changed","heuristic_vs_binpack_changed","rf_independent_uuid_changed","same_node_rf_uuid_changed")},
                         "kind":"native_Go_fake_client; not real Kubernetes or performance"})
    assert len(decisions)==180 and sum(r["rf_independent_uuid_changed"]=="True" for r in decisions)==37
    assert all(r["all_candidates_native_hard_fit"]=="True" and r["ledger_restored_all_modes"]=="True" for r in decisions)
    write_csv(out/"native-rf-ablation-families.csv",families)
    allocations=e.csv("results/round7-live-window-20261019/verified/allocations.csv","real_Kubernetes_sleep_logical_allocation")
    real=e.json("results/round7-live-window-20261019/verified/summary.json","real_Kubernetes_sleep_logical_allocation")
    assert len(allocations)==6
    assert all(r["allocate_uuid"]==r["plugin_consumed_uuid"] for r in allocations)
    assert all(r["rf_uuid"]==r["allocate_uuid"] for r in allocations if r["rf_uuid"])
    for file in ["candidate-scores.csv","native-resource-transitions.csv","release-checks.csv"]:
        rows=e.csv("results/round7-live-window-20261019/verified/"+file,"real_Kubernetes_preserved_evidence")
        write_csv(out/("real-"+file),rows)
    onoff=e.json("results/round7-live-window-20261019/verified/rf-on-off-comparison.json","real_Kubernetes_RF_on_off")
    write_csv(out/"real-six-pods.csv",allocations)
    write_json(out/"real-experiment-summary.json",real)
    write_json(out/"real-on-off.json",onoff)
    # Round 6 main integration and fallback are retained separately from ablation.
    for p in ["results/integration-v6/verification-summary.json","results/integration-v6/source-version.json",
              "results/integration-v6/runtime-code-version.json",
              "results/rf-ablation-20261019/linux-final/execution.json",
              "results/round7-live-window-20261019/final-readonly-verification.json"]:
        e.source(p,"historical_native_or_live_verification_record")
    return families,summary,allocations,real,onoff


def source_tables(e,out):
    repo=ROOT/"scheduler/volcano-rf"
    git=["git","-c","safe.directory="+repo.as_posix(),"-C",str(repo)]
    def run(args):return subprocess.check_output(git+args,text=True,encoding="utf-8").strip()
    head=run(["rev-parse","HEAD"])
    base="20396e2d4742929feb761aca80424d2f487b7a8d"
    diffs=run(["diff","--name-status",base,"HEAD","--","*.go"])
    changes=[]
    for line in diffs.splitlines():
        status,file=line.split("\t",1)
        relative="scheduler/volcano-rf/"+file
        changes.append({"status":status,"file":file,"sha256":sha(e.source(relative,"actual_custom_Volcano_Go_source")),
                        "scope":"test" if file.endswith("_test.go") else "runtime"})
    write_csv(out/"volcano-modified-files.csv",changes)
    assertions=[("utils.go","resourcereqs","resource request parsing"),("utils.go","checkGPUtype","GPU TYPE allow/deny only"),
        ("utils.go","canFitForOrder","instances, memory, core and full-core exclusivity"),
        ("utils.go","checkNodeGPUSharingPredicateAndScore","native device selection"),
        ("device_info.go","FilterNode","node admissibility"),("device_info.go","ScoreNode","node scoring"),
        ("device_info.go","Allocate","native Allocate entry"),("device_info.go","SubResource","native release"),
        ("rf_job.go","rfFits","native hard check + type restriction"),("rf_job.go","allocateRF","plan revalidation"),
        ("rf_job.go","commitNativePlan","native quota update and UUID output annotations"),
        ("hamicore.go","TryAddPod","speculative quota increments; caller enforces hard admission"),("hamicore.go","AddPod","native accounting"),
        ("hamicore.go","SubPod","native accounting release")]
    locations=[]
    for file,func,meaning in assertions:
        p="scheduler/volcano-rf/pkg/scheduler/api/devices/nvidia/vgpu/"+file
        lines=e.source(p,"actual_custom_Volcano_Go_source").read_text(encoding="utf-8").splitlines()
        matches=[i+1 for i,l in enumerate(lines) if l.startswith("func ") and (" "+func+"(" in l or l.startswith("func "+func+"("))]
        assert len(matches)==1,(func,matches)
        locations.append({"file":p,"function":func,"line":matches[0],"role":meaning})
    write_csv(out/"source-call-chain.csv",locations)
    # No user UUID selector in this audited vGPU path. GPUIndex is just a constant here.
    vgpu=repo/"pkg/scheduler/api/devices/nvidia/vgpu"
    selector_terms=["nvidia.com/use-gpuuuid","nvidia.com/nouse-gpuuuid"]
    found={term:[p.name for p in vgpu.glob("*.go") if not p.name.endswith("_test.go") and term in p.read_text(encoding="utf-8")]
           for term in selector_terms}
    assert not any(found.values())
    gpuindex=[p.name for p in vgpu.glob("*.go") if not p.name.endswith("_test.go") and "GPUIndex" in p.read_text(encoding="utf-8")]
    # Includes orderedGPUIndexes identifiers; exact constant usages are documented separately.
    write_json(out/"uuid-selection-source-audit.json",{"commit":head,"queried_UUID_selector_terms":found,
        "vgpu_GPUIndex_constant":"declared in type.go; not read as vGPU request selector",
        "gpushare_GPUIndex":"different allocation path; not usable as this vGPU input",
        "UUID_annotations":"devices-to-allocate and vgpu-ids-new are allocator outputs, not request inputs",
        "safe_control":"three full-core sleep logical reservations; identify remaining UUID; positive-core pods have unique hard-fit",
        "scope":"local deployed-source-derived RF worktree; no new deployment verification this round"})
    bundle=e.json("models/pod-gpu-rf-v1/manifest.json","frozen_model_manifest")
    e.source("models/pod-gpu-rf-v1/reference-contract.json","synthetic_reference_contract")
    e.source("models/pod-gpu-rf-v1/source-records.json","public_hardware_specifications_with_original_URLs")
    e.source("models/pod-gpu-rf-v1/scoring-policy.json","frozen_scoring_weights")
    for p in ["results/offline-v3-release/hardware-catalog.csv","results/offline-v3-release/parameter-sources.csv",
              "results/offline-v4/dataset-summary.json"]:e.source(p,"historical_hardware_or_synthetic_dataset_metadata")
    versions={"volcano_branch":run(["branch","--show-current"]),"volcano_commit":head,"imported_base_commit":base,
              "volcano_worktree_status":run(["status","--porcelain"]),"frozen_model":bundle,
              "cpu_only":True,"python":platform.python_version(),"sklearn":sklearn.__version__,"matplotlib":matplotlib.__version__,
              "new_cluster_query_this_round":False,"historical_cluster_snapshot_date":"2026-10-19",
              "model_sha256":sha(ROOT/"models/pod-gpu-rf-v1/random_forest.joblib"),
              "round8_code_version":"round8-offline-20261020-v1",
              "round8_code_sha256":{p:sha(ROOT/p) for p in ("gpu_lab/round8.py","gpu_lab/thesis.py","tests/test_round8.py")}}
    write_json(out/"versions.json",versions)
    units={"memory_capacity_mib":"MiB (historical catalog reference)","memory_bandwidth_gbps":"GB/s",
           "fp32_tflops":"dense ordinary/vector TFLOPS","fp16_dense_tflops":"dense TFLOPS; masked",
           "bf16_dense_tflops":"dense TFLOPS; masked","int8_dense_tops":"dense TOPS; masked","max_board_power_w":"W",
           "free_memory_mib":"model reference MiB; original ledger MiB retained separately",
           "free_compute_quota":"logical quota /100","allocated_vgpu_count":"instances",
           "running_training_count":"typed vGPU instances","running_inference_count":"typed vGPU instances",
           "free_memory_ratio":"dimensionless","free_compute_quota_ratio":"dimensionless",
           "effective_fp32_tflops":"FP32 × synthetic quota/100 proxy"}
    write_csv(out/"frozen-feature-order.csv",[{"index":i,"feature":name,"unit_and_semantics":units[name],
         "label_source":"synthetic_theory","identity_string_feature":False} for i,name in enumerate(bundle["feature_order"])])
    return versions,changes


def figures(out,v4,sim,families,allocations,onoff,ablation):
    configure_plots()
    figs=out/"figures"
    figs.mkdir(exist_ok=True)
    dynamic=json.loads((out/"dynamic-path.json").read_text(encoding="utf-8"))["states"]
    names=["初始","计算申请后","显存申请后","释放计算申请","全部释放"]
    fig,axs=plt.subplots(2,1,figsize=(10,6),sharex=True,height_ratios=[2,1])
    x=np.arange(5)
    axs[0].plot(x,[s["gpu"]["dynamic"]["free_memory_mib"]/24564*100 for s in dynamic],"o-",label="逻辑剩余显存比例")
    axs[0].plot(x,[s["gpu"]["dynamic"]["free_compute_quota"] for s in dynamic],"s-",label="剩余算力配额比例")
    axs[0].set(ylabel="剩余比例 (%)",ylim=(-5,110),title="冻结 RF：同一 RTX 4090 的合成逻辑资源变化")
    axs[0].legend(loc="lower left");axs[0].grid(alpha=.2)
    for i,s in enumerate(dynamic):
        c=s["classification"]["label"]
        axs[1].bar(i,1,color=COLORS[c],width=.85)
        axs[1].text(i,.5,CLASS_ZH[c],ha="center",va="center",color="white",fontsize=9)
    axs[1].set(yticks=[],xticks=x,xticklabels=names,ylabel="RF 类别")
    fig.text(.5,.005,"仅冻结模型离线推理；不代表真实训练/推理性能或本轮真实 Pod 执行",ha="center",fontsize=9)
    fig.tight_layout(rect=(0,.04,1,1));save_figure(fig,figs/"01-dynamic-transition")
    if (out/"phase-grid.csv").exists():
        with (out/"phase-grid.csv").open(encoding="utf-8-sig") as f:grid=list(csv.DictReader(f))
        fig,ax=plt.subplots(figsize=(9,5))
        for label in LABELS+["pending_verification"]:
            rows=[r for r in grid if r["label"]==label]
            if rows:ax.scatter([int(r["free_memory_mib"])/1024 for r in rows],[int(r["free_compute_quota"]) for r in rows],
                              c=COLORS[label],s=20,marker="s",label=CLASS_ZH[label])
        ax.set(xlabel="逻辑剩余显存 (GiB)",ylabel="剩余算力配额 /100",title="4090 合成状态网格：固定 1 个训练 vGPU（0配额不限流触发待验证）")
        ax.legend(loc="upper left",bbox_to_anchor=(1.01,1));save_figure(fig,figs/"02-resource-phase-map")
    fig,ax=plt.subplots(figsize=(10,5))
    x=np.arange(6)
    ax.bar(x-.25,[v4[p]["selected_metrics"]["test"]["accuracy"] for p in PROTOCOLS],.25,label="原始 RF Accuracy")
    ax.bar(x,[v4[p]["selected_metrics"]["test"]["macro_f1"] for p in PROTOCOLS],.25,label="固定四类 Macro-F1")
    ax.bar(x+.25,[v4[p]["guarded_test"]["accepted_state_fraction"] for p in PROTOCOLS],.25,label="可暂定输出状态比例")
    ax.set(xticks=x,xticklabels=PZH,ylim=(0,1.17),ylabel="比例",title="第四轮：合成理论标签评价与安全门覆盖率")
    ax.tick_params(axis="x",labelsize=8);ax.legend(ncol=3,loc="upper center")
    fig.text(.5,.01,"缺类会降低固定四类 F1；A10 为历史诊断；各协议不是相互独立的重复试验",ha="center",fontsize=8)
    fig.tight_layout(rect=(0,.04,1,1));save_figure(fig,figs/"03-classification-protocols")
    fig,axs=plt.subplots(1,3,figsize=(13,4.8))
    for ax,p in zip(axs,["known_states","A_ada","B_multi_architecture"]):
        cm=np.array(v4[p]["selected_metrics"]["test"]["confusion_matrix_true_rows_predicted_columns"])
        ax.imshow(cm,cmap="Blues")
        for i in range(4):
            for j in range(4):ax.text(j,i,str(cm[i,j]),ha="center",va="center",color="white" if cm[i,j]>cm.max()*.5 else "black")
        ax.set(xticks=range(4),yticks=range(4),xticklabels=["训优","推优","一体","不足"],yticklabels=["训优","推优","一体","不足"],
               xlabel="RF 预测",ylabel="合成理论标签",title=p)
    fig.suptitle("混淆矩阵：不是实测 GPU 性能标签");fig.tight_layout();save_figure(fig,figs/"04-confusion-matrices")
    scenarios=sorted({r["scenario"] for r in sim})
    fig,axs=plt.subplots(2,3,figsize=(15,8))
    metrics=[("mean_wait_s","平均等待（合成秒）"),("training_mean_jct_s","训练 Pod 完成时间（合成秒）"),
             ("inference_pod_response_p95_s","推理批次 Pod 响应 P95（合成秒）"),
             ("reserved_memory_utilization","逻辑显存预留率"),("reserved_compute_utilization","逻辑算力预留率"),
             ("completed_within_horizon","600 合成秒内完成 Pod 数")]
    for ax,(key,title) in zip(axs.flat,metrics):
        for i,strategy in enumerate(["binpack","static_rf","dynamic_rf"]):
            selected=[next(r for r in sim if r["scenario"]==sc and r["strategy"]==strategy) for sc in scenarios]
            ax.bar(np.arange(4)+(i-1)*.24,[r[key+"_mean"] for r in selected],.24,
                   yerr=[r[key+"_std"] for r in selected],label=strategy,capsize=2)
        ax.set(title=title,xticks=range(4),xticklabels=["突发混合","显存密集","平稳混合","训练为主"])
        ax.tick_params(axis="x",labelsize=8)
    axs[0,0].legend(fontsize=8)
    fig.suptitle("第二轮离散事件仿真：3 个相同种子/任务序列，柱为均值，误差线为样本标准差")
    fig.tight_layout();save_figure(fig,figs/"05-synthetic-scheduling")
    fig,ax=plt.subplots(figsize=(9,5))
    x=np.arange(len(families))
    for i,(key,label) in enumerate([("new_strategy_vs_binpack_changed","新策略 vs Binpack"),
                                    ("heuristic_vs_binpack_changed","无 RF 启发式 vs Binpack"),
                                    ("rf_independent_uuid_changed","RF on/off 独立 UUID 变化")]):
        ax.bar(x+(i-1)*.24,[r[key] for r in families],.24,label=label)
    ax.set(xticks=x,xticklabels=[r["family"]+f"\nN={r['cases']}" for r in families],ylabel="最终 UUID 改变组数",
           title="真实 Volcano Go + fake-client + RF HTTP：180 组配对消融")
    ax.tick_params(axis="x",labelsize=8);ax.legend(fontsize=8)
    fig.text(.5,.01,"37/180 为学习项独立贡献；其余策略差异不能都归因于 RF。非真实集群、非性能收益。",ha="center",fontsize=9)
    fig.tight_layout(rect=(0,.04,1,1));save_figure(fig,figs/"06-native-rf-ablation")
    fig,axs=plt.subplots(1,2,figsize=(11,4.8),width_ratios=[1.6,1])
    matrix=np.array([[bool(r["rf_uuid"]) and r["rf_uuid"]==r["allocate_uuid"],r["allocate_uuid"]==r["plugin_consumed_uuid"],True] for r in allocations],dtype=float)
    matrix[5,0]=np.nan
    axs[0].imshow(np.ma.masked_invalid(matrix),cmap="Greens",vmin=0,vmax=1,aspect="auto")
    for i in range(6):
        for j in range(3):axs[0].text(j,i,"不适用" if math.isnan(matrix[i,j]) else "已核对",ha="center",va="center",
                                     color="black" if math.isnan(matrix[i,j]) else "white")
    axs[0].set(xticks=range(3),xticklabels=["RF→Allocate","Allocate→Plugin","配额增量"],yticks=range(6),yticklabels=[r["case"] for r in allocations],
               title="真实 Kubernetes：6 个 sleep Pod")
    with (out/"real-candidate-scores.csv").open(encoding="utf-8-sig") as f:real_scores=list(csv.DictReader(f))
    heuristics=[float(next(r for r in real_scores if r["case"]==case and r["selected"]=="True")["heuristic_contribution"])
                for case in ("control-rf-on","control-rf-off")]
    assert math.isclose(*heuristics)
    axs[1].bar([0,1],heuristics,label="相同启发式贡献",color="#829bae")
    axs[1].bar([0,1],[onoff["rf_term_on"],onoff["rf_term_off"]],bottom=heuristics,label="RF 贡献",color="#3b9d84")
    axs[1].set(xticks=[0,1],xticklabels=["RF on","RF off"],ylabel="冻结策略得分",title="同型号同状态对照：UUID 未变")
    axs[1].legend(fontsize=8)
    fig.suptitle("第七轮仅证实逻辑分配链路；未验证 CUDA 或真实性能")
    fig.tight_layout();save_figure(fig,figs/"07-real-allocation-evidence")
    selected=[r for r in ablation if r["protocol"]=="B_multi_architecture"]
    fig,ax=plt.subplots(figsize=(11,5))
    ax.plot(range(len(selected)),[float(r["validation_macro_f1"]) for r in selected],"o-",label="验证集四类 Macro-F1（模型选择）")
    ax.plot(range(len(selected)),[float(r["test_macro_f1"]) for r in selected],"s--",label="测试集四类 Macro-F1（最终报告）")
    ax.set(xticks=range(len(selected)),xticklabels=[r["feature_set"] for r in selected],ylabel="Macro-F1",ylim=(0,1.08),
           title="跨型号 B 协议特征消融：选择依据是验证集，标签仍为理论合成")
    ax.tick_params(axis="x",rotation=25,labelsize=8);ax.legend(fontsize=8)
    fig.tight_layout();save_figure(fig,figs/"08-feature-ablation")
    flowchart(figs)


def flowchart(figs):
    texts=["Pod Label + vGPU 请求\ntraining / inference；整数 MiB / quota / count",
        "Volcano 当前逻辑账本快照 + 冻结硬件画像\n原始硬件不变；剩余配额随分配/释放变化",
        "原生硬约束过滤\n健康 / 类型 / 显存 / 算力 / 实例 / 共享 / 同节点",
        "冻结 RF 两个二元适配预测 + 设备验证门\n四类展示；pending 使用确定性代理；概率≠可靠性",
        "Pod 类型对应软评分 → GPU 排序\nRF 0.30 + 硬件共享 0.45 + 装箱 0.15 + 平衡 0.10",
        "Go 校验响应、版本、UUID、状态指纹和当前资源\n超时/异常回退；状态改变重试；不得覆盖硬约束",
        "原生 Allocate → 原生配额账本 → UUID 注解\nDevice Plugin 消费设备 → kubelet 启动 sleep",
        "Pod 释放 → 原生 SubResource → 配额恢复\n下一 Pod 再获取快照、重新调用 RF"]
    fig,ax=plt.subplots(figsize=(10,11.8))
    ax.set(xlim=(0,10),ylim=(0,12));ax.axis("off")
    for i,text in enumerate(texts):
        y=11.05-i*1.4
        ax.add_patch(FancyBboxPatch((.6,y-.45),8.8,.95,boxstyle="round,pad=.12",facecolor="#edf5f7",edgecolor="#527d8a"))
        ax.text(5,y,text,ha="center",va="center",fontsize=10)
        if i<7:ax.add_patch(FancyArrowPatch((5,y-.58),(5,y-.85),arrowstyle="-|>",mutation_scale=15,color="#527d8a"))
    ax.text(5,.12,"账本为权威；Python 仅提供建议；逻辑配额不等于物理显存预分配",ha="center",fontsize=10)
    save_figure(fig,figs/"09-core-algorithm-flow")
    (figs/"09-core-algorithm-flow.mmd").write_text('''flowchart TD
 A[Pod Label 与 vGPU 请求] --> B[Volcano 当前逻辑资源与硬件快照]
 B --> C[原生硬约束过滤]
 C --> D[冻结 RF 二元适配预测与验证门]
 D --> E[分类展示与任务相关软评分]
 E --> F[Go 校验 UUID 与资源状态]
 F --> G[原生 Allocate 与 UUID 注解]
 G --> H[Device Plugin 消费 UUID 与 sleep 容器]
 H --> I[SubResource 释放逻辑配额]
 I --> B
 D -->|待验证| J[确定性保守排序]
 J --> F
 E -->|服务失败| J
 F -->|状态过期| B
''',encoding="utf-8")


def unified(e,out,classification,sim,families,real):
    rows=[]
    def add(round_id,experiment,kind,count,result,limit,source):
        rows.append(dict(round=round_id,experiment=experiment,evidence_kind=kind,sample_count=count,result=result,
                         boundary=limit,source=source,real_performance_claim=False))
    for r in classification:
        if r["split"]=="test":add(r["round"],r["protocol"],"synthetic_theory_classifier",r["samples"],
            f"Accuracy={r['accuracy']:.6f}; Macro-F1(4)={r['macro_f1_fixed_four']:.6f}","not empirical performance",r["source"])
    add(2,"三策略四场景主仿真","offline_DES",36,"4 scenarios ×3 seeds ×3 policies;120 Pods/trace",
        "independent synthetic execution model; no physical performance calibration","results/offline-v2/simulation-metrics.csv")
    add(5,"冻结 API 与资源账本","offline_Python_API",None,"classify/rank/commit/release + fallback/concurrency",
        "process-local fixture, not Kubernetes ledger","results/offline-v5-release")
    add(6,"180 组消融","native_Go_fake_client_RF_HTTP",180,"37/180 RF-only UUID changes; 176/180 full strategy vs Binpack",
        "all hard-fit; selected synthetic snapshots; change ≠ improvement","results/rf-ablation-20261019/linux-final/decisions.csv")
    add(7,"六个真实 sleep Pod","real_Kubernetes_logical_allocation",6,"native/plugin6/6;RF/native/plugin5/5;quota6/6;observed overalloc0;deleted6",
        "one4090 node;classes all balanced;on/off UUID unchanged;CUDA untested","results/round7-live-window-20261019/verified/summary.json")
    add(8,"同一4090动态四类轨迹","offline_frozen_RF_synthetic_logical_states",5,"balanced→training_preferred→unavailable→inference_preferred→balanced",
        "new real dynamic-class experiment NOT executed","results/round8-20261020/dynamic-path.json")
    write_csv(out/"unified-experiments.csv",rows)
    return rows


def run(out=OUT):
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    assert (out/"dynamic-path.json").exists(),"First run python -m gpu_lab.round8"
    e=Evidence()
    classification,v4,ablation,permodel=model_tables(e,out)
    sim,changes=simulation_tables(e,out)
    families,native,allocations,real,onoff=native_and_real(e,out)
    versions,files=source_tables(e,out)
    unified(e,out,classification,sim,families,real)
    figures(out,v4,sim,families,allocations,onoff,ablation)
    for p in ["gpu_lab/round8.py","gpu_lab/thesis.py","gpu_lab/runtime/service.py","gpu_lab/runtime/schema.py",
              "gpu_lab/runtime/volcano.py","gpu_lab/runtime/pool.py","tests/test_round8.py",
              "gpu_lab/execution.py","gpu_lab/simulation.py","gpu_lab/scoring.py","gpu_lab/crossgpu/theory.py",
              "gpu_lab/nvidia/model.py","gpu_lab/nvidia/features.py","gpu_lab/runtime/http_server.py",
              "docs/round8-thesis-experiments.md","k8s/round8-pending-approval/README.md",
              "results/round8-20261020/dynamic-path.json"]:
        e.source(p,"round8_analysis_or_unchanged_runtime_source")
    write_csv(out/"evidence-source-index.csv",list(e.files.values()))
    with (out/"phase-grid.csv").open(encoding="utf-8-sig") as f:grid=list(csv.DictReader(f))
    write_json(out/"material-validation.json",{"historical_simulation_recomputed_and_matches":True,
        "trace_hashes_identical_across_strategies":True,"all_split_sample_episode_feature_overlaps_zero":True,
        "ablation_cases":native["cases"],"rf_uuid_changes":native["rf_changed_uuid"],"real_native_plugin_matches":6,
        "real_rf_native_plugin_eligible":5,"real_dynamic_class_change_demonstrated":False,
        "new_cluster_access":False,"frozen_model_sha256":versions["model_sha256"],"source_files":len(e.files),
        "grid_states":len(grid),"grid_output_counts":dict(Counter(r["label"] for r in grid)),
        "grid_not_an_independent_accuracy_test":True})
    print(json.dumps({"output":str(out),"figures":len(list((out/"figures").glob("[0-9][0-9]-*.png"))),"sources":len(e.files)},ensure_ascii=False))


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output",type=Path,default=OUT)
    run(p.parse_args().output)
