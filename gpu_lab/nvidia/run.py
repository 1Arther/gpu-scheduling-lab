"""Local CPU experiment: python -m gpu_lab.nvidia.run --output NEW_DIRECTORY."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import platform
import shutil
import time

from ..profiles import ROOT
os.environ.setdefault("MPLCONFIGDIR",str(ROOT/".tmp/matplotlib"))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import sklearn

from ..comparison import write_csv
from ..crossgpu.catalog import STATIC
from ..crossgpu.data import audit, generate
from ..crossgpu.theory import CONTRACT, LABELS, UNKNOWN
from ..dataset import write_jsonl
from ..run import save_json, sha256
from .data import ADDITIONS, PLAN_PATH, assemble_catalog, load_plan, split_protocol
from .demos import demonstrations, regression
from .features import FEATURE_SETS
from .model import metrics, select_models


def historical_snapshot():
    return {str(f.relative_to(ROOT)):sha256(f) for d in sorted((ROOT/"results").glob("offline-v[123]*")) if d.is_dir()
            for f in sorted(d.rglob("*")) if f.is_file()}


def export_catalog(devices, source, out):
    save_json(out/"nvidia-hardware.json",{"devices":[d.record for d in devices.values()],"verified_on":source["verified_on"],
               "real_performance_label_source":"unknown","mixed_precision_model_policy":"all three low-precision fields retained but masked; common FP32 reference only"})
    write_csv(out/"nvidia-hardware.csv",[{"device_id":d.device_id,"model":d.record["model"],"architecture":d.record["architecture"],
               "scope":d.record["hardware_scope"],**d.values(),"performance_label_source":"unknown"} for d in devices.values()])
    write_csv(out/"parameter-sources.csv",[{"device_id":d.device_id,"parameter":k,**v} for d in devices.values() for k,v in d.record["specifications"].items()])
    lines = ["# NVIDIA 官方规格来源审计", "", f"{len(devices)}款独立型号/配置。沿用第三轮12条NVIDIA记录，新增3款；A40的FP32等参数由本轮官方PDF补齐，原数据文件不修改。", "",
             "7项静态字段全部保留。共同模型有数值信息的静态列是显存容量、带宽、非Tensor FP32及功耗；3个Tensor低精度列在本轮统一屏蔽为null，以免不一致的累加精度、稀疏口径和缺失模式成为架构/型号的暗示。中位数填补+缺失指示仅在训练集拟合；全空列0是占位，不代表硬件性能为0。", "",
             "FP32非Tensor CUDA Core峰值与TF32/Tensor Core峰值分开；带宽gbps字段仍按原项目约定使用十进制GB/s。nominal GB换算为MiB仅为保守离线容量假设。FP16/BF16注明累加精度；不从不明Tensor总值推导。", "",
             "| 型号/配置 | 架构 | 关键参数完整 | 来源 |", "|---|---|---|---|"]
    for d in devices.values():
        urls = sorted({s["source_url"] for s in d.record["specifications"].values()})
        lines.append(f"| {d.record['model']} | {d.record['architecture']} | {d.eligible()} | "+" / ".join(f"[NVIDIA]({u})" for u in urls)+" |")
    lines += ["", "A100仅80GB SXM标准400W；H100仅80GB SXM，H200仅141GB SXM，未合并PCIe/SXM配置。RTX A6000与RTX6000 Ada是两个型号。RTX5080使用官方reference规格。B200未完成配置特定的完整审计，本轮不凑数纳入。", "",
              "RTX A6000的309.7 Tensor数字带稀疏注脚，RTX6000 Ada的1457是稀疏FP8，都没有冒充dense FP16。A40: FP16为FP16累加，BF16为FP32累加；数据库保存149.7的非稀疏值，但统一模型中屏蔽。", "",
              "来源为人工核实的官方文档URL、定位、单位及口径，非真实性能测量，也非下载的不可变网页快照。所有性能标签仍为unknown；监督实验另标synthetic。"]
    (out/"source-audit.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    save_json(out/"source-audit.json",{"sources":source["sources"],"devices":len(devices),"architectures":dict(Counter(d.record["architecture"] for d in devices.values())),
               "unknown_per_parameter":{k:sum(d.values()[k] is None for d in devices.values()) for k in STATIC},
               "masked_per_parameter":{k:sum(d.values(comparable=True)[k] is None for d in devices.values()) for k in STATIC},"measured_labels":0,"public_benchmark_labels":0})


def confusion_plot(m, title, path):
    cm = np.asarray(m["confusion_matrix_true_rows_predicted_columns"])
    fig, ax = plt.subplots(figsize=(7,5.5))
    ax.imshow(cm,cmap="Blues")
    ax.set(xticks=range(4),yticks=range(4),xticklabels=["train","infer","both","neither"],yticklabels=["train","infer","both","neither"],
           xlabel="RF prediction",ylabel="Synthetic theoretical label",title=title+"\nSYNTHETIC labels only")
    for i in range(4):
        for j in range(4):
            ax.text(j,i,str(cm[i,j]),ha="center",va="center",color="white" if cm[i,j]>cm.max()/2 else "black")
    fig.tight_layout(); fig.savefig(path,dpi=160); plt.close(fig)


def evaluate(name, splits, models, selected, comparisons, devices, plan, out):
    folder = out/name
    folder.mkdir(exist_ok=True)
    tests, rows = {}, splits["test"]
    for variant,model in models.items():
        tests[variant] = metrics(rows,model.raw_predict([r["features"] for r in rows]))
    model = models[selected]
    raw = model.raw_predict([r["features"] for r in rows])
    guarded = [model.predict_device(devices[r["device_id"]],r["dynamic_state"],raw_prediction=p) for r,p in zip(rows,raw)]
    selected_metrics = {split:metrics(batch,model.raw_predict([r["features"] for r in batch])) for split,batch in splits.items()}
    by_model, predictions, errors = [], [], []
    for r,p,g in zip(rows,raw,guarded):
        record = {"sample_id":r["sample_id"],"device_id":r["device_id"],"true_label":r["label"],"truth_kind":"synthetic",
                  "predicted_label":p["label"],"guarded_label":g["label"],"raw_rf":p,"guard":g,"features":r["features"],"dynamic_state":r["dynamic_state"]}
        predictions.append(record)
        if r["label"] != p["label"]:
            errors.append({"sample_id":r["sample_id"],"device_id":r["device_id"],"true_label":r["label"],"predicted_label":p["label"],
                           **r["features"],"guarded_label":g["label"],"guard_reasons":";".join(g["reasons"])})
    for device_id in sorted({r["device_id"] for r in rows}):
        mask = [i for i,r in enumerate(rows) if r["device_id"] == device_id]
        m = metrics([rows[i] for i in mask],[raw[i] for i in mask])
        accepted = [i for i in mask if guarded[i]["label"] != UNKNOWN]
        config = plan["protocols"][name]
        previous = device_id in config.get("diagnostic_test_models",[]) or config["kind"] in ("known_model_unseen_episode","previously_observed_model_diagnostic")
        by_model.append({"protocol":name,"device_id":device_id,"architecture":devices[device_id].record["architecture"],
                         "evaluation_status":"previously_observed_or_known_diagnostic" if previous else "fresh_model_held_out_this_round",
                         "samples":m["samples"],"accuracy":m["accuracy"],"macro_f1":m["macro_f1"],"observed_macro_f1":m["macro_f1_observed_classes"],
                         "errors":m["misclassified"],"true_class_counts":json.dumps(m["class_counts"],sort_keys=True),
                         "accepted_states":len(accepted),"accepted_state_fraction":len(accepted)/len(mask),
                         "accepted_guarded_errors":sum(guarded[i]["label"] != rows[i]["label"] for i in accepted),
                         "accepted_raw_errors":sum(raw[i]["label"] != rows[i]["label"] for i in accepted)})
    accepted = [i for i,g in enumerate(guarded) if g["label"] != UNKNOWN]
    coverage = {"samples":len(rows),"accepted_states":len(accepted),"accepted_state_fraction":len(accepted)/len(rows),
                "accepted_guarded_errors":sum(guarded[i]["label"] != rows[i]["label"] for i in accepted),
                "accepted_raw_errors":sum(raw[i]["label"] != rows[i]["label"] for i in accepted),
                "models_with_any_provisional_prediction":sum(r["accepted_states"]>0 for r in by_model),"test_models":len(by_model),
                "models_with_any_provisional_fraction":sum(r["accepted_states"]>0 for r in by_model)/len(by_model),
                "not_real_confidence_or_performance":True}
    write_jsonl(folder/"test-predictions.jsonl",predictions)
    write_csv(folder/"test-errors.csv",errors,fieldnames=["sample_id","device_id","true_label","predicted_label",*rows[0]["features"],"guarded_label","guard_reasons"])
    write_csv(folder/"per-model.csv",by_model)
    result = {"selected_feature_set":selected,"selection_scope":"validation only, frozen before all protocol test evaluations",
              "selected_metrics":selected_metrics,"test_feature_ablations":tests,"per_model":by_model,"guarded_test":coverage}
    save_json(folder/"metrics.json",result)
    confusion_plot(selected_metrics["test"],name,folder/"confusion.png")
    write_csv(folder/"confusion.csv",[{"true_label":label,**dict(zip(LABELS,selected_metrics["test"]["confusion_matrix_true_rows_predicted_columns"][i]))} for i,label in enumerate(LABELS)])
    ablations = [{"protocol":name,**{k:v for k,v in c.items() if k != "leaf_candidates"},
                  "test_accuracy":tests[c["feature_set"]]["accuracy"],"test_macro_f1":tests[c["feature_set"]]["macro_f1"],
                  "test_observed_macro_f1":tests[c["feature_set"]]["macro_f1_observed_classes"],"selected_on_validation":c["feature_set"] == selected} for c in comparisons]
    write_csv(folder/"ablations.csv",ablations)
    return result, by_model, ablations


def write_report(out, results, per_model, ablations, demos, reg, dataset, historic_count):
    lines = ["# 第四轮：NVIDIA 跨型号 RandomForest 适配性分类", "",
             "仅本机CPU训练和离散事件仿真，未连接Kubernetes、未修改Volcano、未运行GPU负载。所有准确率均以**合成理论标签**为真值，不是真实GPU性能分类准确率。", "",
             f"15款NVIDIA独立型号/配置；{dataset['samples']}条合成资源状态。前三轮 {historic_count} 个结果文件经SHA256核对内容未变。旧代码不修改，新增 gpu_lab/nvidia 包，复用第三轮账本、数据生成和标签合同。", "",
             "## 固定测试与标签", "",
             "在生成本轮标签和查看本轮预测前保存 data/round4-plan.json。新独立型号为RTX A6000、RTX6000 Ada、RTX5080；它们不进入任何协议的训练或验证，包括已知状态实验。A10是已看过的诊断案例；H200与RTX5090也属历史已观察型号，相关结果明确标为诊断。跨协议可能重复测试相同新型号，因此不是多个相互独立的重复验证；所有协议同时冻结后才一次性评估。", "",
             "| 协议 | 意义 |", "|---|---|",
             "| known_states | 历史12款型号的新状态，按整条轨迹60/20/20划分 |",
             "| A_ampere / A_ada | 同架构、按型号完全隔离，分别测试新RTX A6000/RTX6000 Ada |",
             "| B_multi_architecture | 多架构训练，测试三个新型号及H200历史诊断；不是整个架构未见 |",
             "| C_blackwell_holdout | 训练与验证完全无Blackwell，测试5080/5090；严格架构外推，5090为历史诊断 |",
             "| D_a10_diagnostic | 完全不拟合A10的诊断回归，不能称为新盲测 |", "",
             "同一协议中型号分组无泄漏（known_states按轨迹分组）；样本ID、轨迹ID和特征哈希均检查不交叉。Hopper和Blackwell各只有两个型号，不足以在单一架构内划分三个互斥型号集合，不硬凑同架构实验。", "",
             "标签合同原样沿用第三轮：IEEE FP32非Tensor；training8192MiB、最低20配额、2TFLOP/8GB、0.2s与5次/s；inference4096MiB、最低10配额、0.06TFLOP/2.5GB、0.01s与100次/s。理论时间=max(work/(FP32*0.35*q),traffic/(BW*0.55*q))，每卡100个假想份额、10实例。系数与配额线性缩放是合成假设，不是实测规律。分类取两目标组合，训推一体表示分别满足而非同时可运行。", "",
             "未改标签门槛，未增加厂商定位标签，未把RF预测回填为真值。缺失类按真实分布报告。Ampere部分设备FP32不足以满足训练合同，部分设备根本不会出现训练优先类。", "",
             "## 特征与模型选择", "",
             "基线为7静态+5动态+2比例的14列接口。3个低精度Tensor列因口径差异统一屏蔽，数据库保留原值。有效静态列为容量、带宽、非Tensor FP32和功耗；不将型号/厂商/架构字符串输入RF。", "",
             "增加effective_fp32=FP32*q、effective_bandwidth=BW*q、compute_bandwidth=FP32*1000/BW（FLOP/byte）。只由输入计算，不读取标签、参考合同阈值或执行时间。它们与本轮理论标签假设一致，收益表示更容易拟合已知理论规律，不能证明实际性能线性缩放。", "",
             "8组特征×2个叶节点大小{2,5}；120树、max_features0.85、seed20261017、CPU单线程。各特征组只在验证集选叶节点；再按验证集中实际出现类别的macro-F1、Accuracy、较少特征选主模型。始终同时报告固定四类Macro-F1，以免缺失类别改变解释。没有利用测试集选择特征或阈值。参数选择不使用任何测试标签。", "",
             "## 模型结果（合成标签）", "",
             "| 协议 | 选中特征 | 测试样本 | Accuracy | 四类Macro-F1 | 实际类别Macro-F1 |", "|---|---|---|---|---|---|"]
    for name,r in results.items():
        m = r["selected_metrics"]["test"]
        lines.append(f"| {name} | {r['selected_feature_set']} | {m['samples']} | {m['accuracy']:.4f} | {m['macro_f1']:.4f} | {m['macro_f1_observed_classes']:.4f} |")
    lines += ["", "known_states的训练/验证/测试指标、各协议四类Precision/Recall/F1/Support、混淆矩阵见各metrics.json和confusion.csv/png。所有原始预测都计入测试指标，包括最终因可信度不足而待验证的状态。", "",
              "| 协议/型号 | 架构 | 状态 | Accuracy | 四类F1 | 接受状态比例 | 接受后错误 |", "|---|---|---|---|---|---|---|"]
    for row in per_model:
        lines.append(f"| {row['protocol']} / {row['device_id']} | {row['architecture']} | {row['evaluation_status']} | {row['accuracy']:.4f} | {row['macro_f1']:.4f} | {row['accepted_state_fraction']:.3f} | {row['accepted_guarded_errors']} |")
    lines += ["", "每款型号的类别数量在per-model.csv中；所有错误样本含硬件/资源特征保存在各协议test-errors.csv，全部预测与待验证原因保存test-predictions.jsonl。不把无训练优先真值的单型号F1=0误读为该类被证实无效。", "",
              "## 特征消融", "", "各行参数均独立在同一验证集选取。下表为B协议，主模型在测试前选定，不能按下表测试分数改选。", "",
              "| 组合 | 验证Acc | 验证实际类F1 | 测试Acc | 测试四类F1 | 验证选中 |", "|---|---|---|---|---|---|"]
    for row in ablations:
        if row["protocol"] == "B_multi_architecture":
            lines.append(f"| {row['feature_set']} | {row['validation_accuracy']:.4f} | {row['validation_observed_macro_f1']:.4f} | {row['test_accuracy']:.4f} | {row['test_macro_f1']:.4f} | {row['selected_on_validation']} |")
    lines += ["", "absolute_only移除两个剩余比例；relative_memory只保留显存比例而去掉总/剩余显存绝对值；no_static_capacity仅移除物理总容量。参考负载本身有绝对MiB下限，因此比例不能等价代替绝对显存。有效算力/带宽代理把乘法交互显式提供给轴对齐RF；比例项和算力/带宽比是否有帮助要结合逐协议消融结果，不能假设必然提升。", "",
              "## 相关验证证据与未知处理", "",
              "取消第三轮全局缺类开关。先校验输入/配额/软件后端及资源约束，再检查三项关键静态能力是否超出训练范围。相关验证群体优先使用同架构且三个关键能力均在2倍距离内的验证设备；无此群体时要求至少两款数值邻近的验证设备。架构只用于可选验证证据路由，不进入RF。未知架构仍可走数值邻域。", "",
              "对训练、推理两个预测符号分别检查：相关验证群体中该预测符号至少20个样本、3条轨迹、正确比例至少0.90。硬约束已否定的目标直接为False，不要求无关类别存在。所有阈值在计划中冻结，不随测试效果调整。RF概率未用于质量开关，更不等于真实可信度。相关合成状态有相关性，因此支持量与正确率也不是独立性能置信区间。", "",
              "数值超出训练范围仍待验证；例如16GB的5080小于本次其余训练卡的最小容量，不能因为原始RF答对就宣布已验证。缺失关键字段、未知配额/后端同样保守拒绝。待验证保留raw_rf和0.8有效能力因子的确定性理论回退。调度适配器先执行显存/算力/实例/后端过滤，随后按保守理论分数与packing排序；不会用分类结果批准超额Pod。", "",
              "| 协议 | 可暂定型号数/总数 | 接受状态数/总数 | 接受后错误 |", "|---|---|---|---|"]
    for name,r in results.items():
        g = r["guarded_test"]
        lines.append(f"| {name} | {g['models_with_any_provisional_prediction']}/{g['test_models']} | {g['accepted_states']}/{g['samples']} | {g['accepted_guarded_errors']} |")
    lines += ["", "该统计中接受表示最终返回四类之一。原始RF错误数与硬约束修正后的错误数分别保留，防止用过滤隐藏分类器错误。B汇总同时含H200历史诊断，独立新型号请看逐型号表三条fresh行。未知演示没有真实性能标签，不统计真实性能准确率。", "",
              "## 动态演示与兼容回归", "",
              "使用known_states模型演示4090、5090、L40，各5步：初始、占用1000MiB/60配额、再占用大部分显存/5配额、释放计算份额、完全释放。每步包含静态硬件、动态状态、训练/推理预测、四类概率近似、证据和待验证原因。静态硬件哈希不变。四类概率是两个RF边际估计的乘积近似，非校准联合概率。", "",
              "11卡三策略相同60Pod/seed101，旧独立执行模型seed707；三种策略不改资源和释放语义。待验证时的兼容概率字段承载确定性排序分数，明确标为非概率。下面只证明兼容，不能作为调度收益结论。", "",
              "| 策略 | 完成 | 平均等待s | 训练JCTs | 推理PodP95s |", "|---|---|---|---|---|"]
    for m in reg["summary"]:
        lines.append(f"| {m['strategy']} | {m['completed_total_after_drain']} | {m['mean_wait_s']:.3f} | {m['training_mean_jct_s']:.3f} | {m['inference_pod_response_p95_s']:.3f} |")
    lines += ["", "所有策略无超分配，结束后显存和配额全部恢复；Pod Label解析、动态变更后重新预测均已检查。未使用该回归结果反向调模型。", "",
              "## 结论的使用范围", "",
              "本轮可以检验数值硬件特征对合成理论标签的跨型号近似；新增型号数量只有3，存在单一厂商、固定合同、少数验证型号及合成轨迹相关性限制。即使测试高分也不能声称真实训练/推理任务、不同精度或实际vGPU干扰已验证。A10诊断与第三轮50.85%使用不同训练覆盖/拆分状态，只能诊断改进，不能作为受控独立盲测提升率。", "",
              "## 复现", "", "在gpu-scheduling-lab目录：", "", "```powershell",
              "$env:TEMP=(Join-Path (Get-Location) '.tmp'); $env:TMP=$env:TEMP", "$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'",
              ".\\.venv\\Scripts\\python.exe -m gpu_lab.nvidia.run --output results/offline-v4-replay",
              ".\\.venv\\Scripts\\python.exe -m pytest -q --basetemp .tmp/pytest-round4-replay", "```", "",
              "依赖沿用requirements.txt，不需要CUDA。计划、来源、代码/历史结果哈希和模型选择均保存。fresh测试型号始终不参与拟合/验证。", "",
              "## 文件索引", "",
              "- [硬件CSV](nvidia-hardware.csv) / [硬件JSON](nvidia-hardware.json) / [逐参数来源](parameter-sources.csv) / [审计说明](source-audit.md)",
              "- [冻结实验计划](frozen-plan.json) / [全部划分](split-manifests.json) / [测试前选择冻结](selection-frozen-before-test.json)",
              "- [逐型号结果](per-model.csv) / [完整特征消融](ablations.csv) / [未知型号示例](unknown-demo.json)",
              "- [动态分类示例](dynamic-demo.json) / [仿真回归](simulation-regression.json)",
              "- 各协议目录内selected-model.joblib、model-metadata.json、metrics.json、confusion.png/csv、test-errors.csv和test-predictions.jsonl。"]
    (out/"report.md").write_text("\n".join(lines)+"\n",encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=ROOT/"results/offline-v4")
    args = parser.parse_args()
    out = args.output.resolve()
    if out.exists() and any(out.iterdir()):
        parser.error("Use a new empty output directory; no historical outputs overwritten")
    if any(d.resolve() == out or d.resolve() in out.parents for d in (ROOT/"results").glob("offline-v[123]*")):
        parser.error("Cannot output into historical rounds")
    out.mkdir(parents=True,exist_ok=True)
    historic, start = historical_snapshot(), time.perf_counter()
    plan = load_plan()
    shutil.copy2(PLAN_PATH,out/"frozen-plan.json")
    devices, source = assemble_catalog(out/"source-records.json")
    export_catalog(devices,source,out)
    save_json(out/"reference-contract.json",CONTRACT)
    rows,dataset = generate(devices,seed=plan["seed"],episodes=plan["episodes_per_device"],steps=plan["steps"])
    write_jsonl(out/"synthetic-states.jsonl",rows)
    save_json(out/"dataset-summary.json",dataset)
    splits, all_models, selected, comparisons, manifests = {},{},{},{},{}
    for name,config in plan["protocols"].items():
        batch = split_protocol(rows,config,plan["seed"])
        splits[name] = batch
        manifest = audit(batch,model_disjoint=config["kind"] != "known_model_unseen_episode")
        manifests[name] = {**manifest,"device_ids":{k:sorted({r["device_id"] for r in v}) for k,v in batch.items()},
                           "sample_ids":{k:[r["sample_id"] for r in v] for k,v in batch.items()},"protocol":config}
        assert not set(plan["fresh_test_models"]) & {r["device_id"] for r in batch["train"]+batch["validation"]}
        folder = out/name; folder.mkdir()
        for split,data in batch.items():
            write_jsonl(folder/f"{split}.jsonl",data)
        print(f"VALIDATION SELECTION {name}: train={len(batch['train'])}, validation={len(batch['validation'])}",flush=True)
        models,choice,comparison = select_models(batch,devices,plan)
        all_models[name],selected[name],comparisons[name] = models,choice,comparison
        for variant,model in models.items():
            model.save(folder/f"{variant}.joblib")
        models[choice].save(folder/"selected-model.joblib")
        save_json(folder/"model-metadata.json",models[choice].metadata)
        save_json(folder/"validation-selection.json",comparison)
        print(f"SELECTED {name}: {choice}",flush=True)
    save_json(out/"split-manifests.json",manifests)
    save_json(out/"selection-frozen-before-test.json",{"plan_sha256":sha256(PLAN_PATH),"selected_feature_sets":selected,
               "validation_comparisons":comparisons,"all_test_evaluations_begin_after_this_file":True,
               "model_sha256":{name:sha256(out/name/"selected-model.joblib") for name in selected}})
    results,per_model,ablations = {},[],[]
    for name in splits:
        result,per,abl = evaluate(name,splits[name],all_models[name],selected[name],comparisons[name],devices,plan,out)
        results[name] = result; per_model.extend(per); ablations.extend(abl)
        print("TEST",name,{k:result["selected_metrics"]["test"][k] for k in ("accuracy","macro_f1")},flush=True)
    write_csv(out/"per-model.csv",per_model)
    write_csv(out/"ablations.csv",ablations)
    save_json(out/"metrics.json",results)
    # Known-state model serves the already-known physical fleet. Unknown examples
    # use the truly model-disjoint mixed-architecture experiment instead.
    demos = demonstrations(all_models["known_states"][selected["known_states"]],devices)
    from ..crossgpu.features import initial_state
    unknown_model = all_models["B_multi_architecture"][selected["B_multi_architecture"]]
    for row in demos["unknown"]:
        d = devices[row["device_id"]]
        row["prediction"] = unknown_model.predict_device(d,initial_state(d))
        row["model_protocol"] = "B_multi_architecture"
    save_json(out/"dynamic-demo.json",demos["dynamic"])
    save_json(out/"unknown-demo.json",demos["unknown"])
    save_json(out/"fallback-ranking-demo.json",demos["fallback_pod_ranking"])
    reg = regression(all_models["known_states"][selected["known_states"]],devices)
    save_json(out/"simulation-regression.json",{k:v for k,v in reg.items() if k != "events"})
    write_csv(out/"simulation-regression.csv",reg["summary"])
    for name,events in reg["events"].items():
        write_jsonl(out/f"regression-{name}-events.jsonl",events)
    assert historical_snapshot() == historic
    save_json(out/"historical-results-preserved.json",{"unchanged":True,"files":len(historic),"sha256":historic})
    write_report(out,results,per_model,ablations,demos,reg,dataset,len(historic))
    sources = sorted((ROOT/"gpu_lab").rglob("*.py"))+sorted((ROOT/"tests").glob("*.py"))+[PLAN_PATH,ADDITIONS]
    save_json(out/"run-manifest.json",{"seed":plan["seed"],"python":platform.python_version(),"sklearn":sklearn.__version__,"elapsed_s":time.perf_counter()-start,
               "source_sha256":{str(f.relative_to(ROOT)):sha256(f) for f in sources},"offline_cpu_only":True,"real_performance_samples":0,"test_used_for_selection":False})
    print(json.dumps({"output":str(out),"samples":len(rows),"historical_files_unchanged":len(historic)},ensure_ascii=False),flush=True)


if __name__ == "__main__":
    main()
