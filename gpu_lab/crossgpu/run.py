"""Offline-only round 3: python -m gpu_lab.crossgpu.run --help."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import platform
import shutil
import time

from ..profiles import ROOT
os.environ.setdefault("MPLCONFIGDIR", str(ROOT / ".tmp/matplotlib"))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import sklearn
import numpy as np

from ..comparison import write_csv
from ..dataset import write_jsonl
from ..run import save_json, sha256
from .catalog import CATALOG_SOURCE, STATIC, compile_catalog
from .data import generate, split_known, split_unseen, audit
from .demos import dynamic_demo, static_demo, unknown_demo, regression
from .features import FEATURES
from .model import metrics, train
from .theory import CONTRACT, LABELS, UNKNOWN


def legacy_snapshot():
    return {str(f.relative_to(ROOT)): sha256(f)
            for d in sorted((ROOT / "results").glob("offline-v[12]*")) if d.is_dir()
            for f in sorted(d.rglob("*")) if f.is_file()}


def export_catalog(output, devices, source):
    save_json(output / "hardware-catalog.json", {"devices": [d.record for d in devices.values()],
              "source_file_sha256": sha256(CATALOG_SOURCE), "verified_on": source["verified_on"],
              "performance_labels": "unknown; no empirical labels inferred from specifications"})
    write_csv(output / "hardware-catalog.csv", [{"device_id": d.device_id, "vendor": d.record["vendor"],
              "model": d.record["model"], "architecture": d.record["architecture"], "device_type": d.record["device_type"],
              "software_backend": d.record["software_backend"], **d.values(), "eligible_for_synthetic_FP32": d.eligible(),
              "performance_label_source": "unknown"} for d in devices.values()])
    write_csv(output / "parameter-sources.csv", [{"device_id": d.device_id, "parameter": k, **v} for d in devices.values() for k, v in d.record["specifications"].items()])
    summary = {"devices": len(devices), "eligible_GPU_records": sum(d.eligible() for d in devices.values()),
               "vendors": dict(Counter(d.record["vendor"] for d in devices.values())),
               "missing_per_field": {k: sum(d.values()[k] is None for d in devices.values()) for k in STATIC},
               "model_masked_per_field": {k: sum(d.values(comparable=True)[k] is None for d in devices.values()) for k in STATIC},
               "sources": source["sources"], "performance_label_sources": {"measured": 0, "public_benchmark": 0, "unknown": len(devices)},
               "audit_scope": "manual official-source audit, not a downloaded immutable vendor-page snapshot; offline replay reads curated source-records.json",
               "important_limits": ["FP32 vector/general peaks only; AMD matrix FP32 excluded", "all nominal capacity GB -> conservative decimal MiB model, not live capacity",
                                    "unqualified sparse figures retained only as rejected observations", "unaligned low-precision accumulator/engine figures masked before RF",
                                    "MI250X multi-die package hypothetical pooled resources ignore locality", "no NPU unified supervision/execution model",
                                    "Huawei large-PDF evidence limited to official indexed excerpt; all seven numeric fields remain null"]}
    save_json(output / "source-audit.json", summary)
    lines = ["# 第三轮公开硬件来源审计", "", "规格事实与性能标签分开保存。下列记录不是 22 张实测 GPU，也不是 22 款全部参数齐全的设备。", "",
             f"共 {summary['devices']} 条设备记录；{summary['eligible_GPU_records']} 条具备共同 FP32 理论模型所需的容量、带宽和向量 FP32。真实训练/推理性能标签为 0。", "",
             "`hardware-catalog.csv` 是汇总；`parameter-sources.csv` 和 JSON 按参数保存原值、原单位、归一值、来源、定位、口径及屏蔽原因。带宽字段 gbps 按本项目约定实际单位为 GB/s，不是 Gbit/s。", "",
             "| 型号 | 厂商 | 理论原型输入齐全 | 缺失规格 | 来源 |", "|---|---|---|---|---|"]
    for d in devices.values():
        missing = ", ".join(k for k, v in d.values().items() if v is None) or "无"
        urls = sorted({s["source_url"] for s in d.record["specifications"].values()})
        lines.append(f"| {d.record['model']} | {d.record['vendor']} | {d.eligible()} | {missing} | " + " / ".join(f"[官方]({u})" for u in urls) + " |")
    lines += ["", "## 审计边界", "", *[f"- {x}" for x in summary["important_limits"]], "",
              "公开 dense 数值可以保存在数据库中，但不等于各家累加精度、运算单元、编译器和工作负载表现可直接比较。本轮统一标签只使用 FP32 参考合同；不将 FP16/INT8 峰值或厂商定位视为训练/推理真值。", "",
              "未下载完整网页快照。来源访问日期和定位保存在 source-records.json；网页未来变化时需要重新人工审计。"]
    (output / "source-audit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def evaluate_protocol(model, splits, output, protocol, devices):
    result = {}
    for name, rows in splits.items():
        predictions = model.raw_predict([r["features"] for r in rows])
        result[name] = metrics(rows, predictions)
        if name == "test":
            write_jsonl(output / f"{protocol}-test-predictions.jsonl", [{"sample_id": r["sample_id"], "device_id": r["device_id"],
                        "truth_source": "synthetic", "true_label": r["label"], **p} for r, p in zip(rows, predictions)])
            result["per_test_model"] = {device: metrics([r for r in rows if r["device_id"] == device],
                                                        [p for r, p in zip(rows, predictions) if r["device_id"] == device])
                                        for device in sorted({r["device_id"] for r in rows})}
            # All raw predictions are evaluated; abstention must not hide poor extrapolation accuracy.
            guarded = [model.predict_device(devices[r["device_id"]], r["dynamic_state"], raw_prediction=p) for r, p in zip(rows, predictions)]
            result["guarded_test"] = {"total": len(rows), "accepted": sum(p["label"] != UNKNOWN for p in guarded),
                                      "pending": sum(p["label"] == UNKNOWN for p in guarded),
                                      "accepted_coverage": sum(p["label"] != UNKNOWN for p in guarded) / len(rows),
                                      "reliability_scope": "synthetic domain checks only; no empirical accuracy claimed"}
            write_jsonl(output / f"{protocol}-test-guarded.jsonl", [{"sample_id": r["sample_id"], **p} for r, p in zip(rows, guarded)])
    save_json(output / f"{protocol}-metrics.json", result)
    matrix = np.asarray(result["test"]["confusion_matrix_true_rows_predicted_columns"])
    fig, ax = plt.subplots(figsize=(8, 6))
    ax.imshow(matrix, cmap="Blues")
    short = ["train", "infer", "both", "neither"]
    ax.set(xticks=range(4), yticks=range(4), xticklabels=short, yticklabels=short,
           xlabel="RF prediction", ylabel="Synthetic theoretical label", title=f"{protocol}: SYNTHETIC labels only")
    for i in range(4):
        for j in range(4):
            ax.text(j, i, str(matrix[i, j]), ha="center", va="center", color="white" if matrix[i, j] > matrix.max() / 2 else "black")
    fig.tight_layout()
    fig.savefig(output / f"{protocol}-confusion.png", dpi=180)
    plt.close(fig)
    write_csv(output / f"{protocol}-confusion.csv", [{"true_label": label, **dict(zip(LABELS, matrix[i].tolist()))} for i, label in enumerate(LABELS)])
    return result


def report(output, source_audit, dataset, protocols, dynamic, unknown, reg, preserved):
    lines = ["# 第三轮：跨型号 RandomForest 适配性原型", "",
             "本轮仅在本机 CPU 运行 Python、随机森林和离散事件仿真；未连接集群，未执行真实 GPU 训练或推理。", "",
             "**结论边界：这是公开硬件参数 + 合成资源状态 + 合成理论标签的监督学习原型，尚不具备经真实性能验证的跨厂商预测能力。** 模型学习的是声明的参考合同。没有测量标签和可比公开基准标签，不报告真实跨厂商准确率。", "",
             f"硬件库 {source_audit['devices']} 条、7 个厂商；{source_audit['eligible_GPU_records']} 条具有共同 FP32 原型所需参数，其余保留 null/待验证。合成状态 {dataset['samples']} 条。历史结果文件 {preserved} 个已核对 SHA256，内容未变。", "",
             "## 标签、资源和精度口径", "",
             "训练/推理类型由 Pod 的 `gpu-lab/workload-type: training|inference` 指定。RF 不识别任务类型，也不使用型号、厂商、架构字符串作为输入。", "",
             "两个布尔目标表示当前剩余资源是否分别满足以下假设负载。两者都满足为训推一体，只有训练满足为训练优先，只有推理满足为推理优先，都不满足为当前不满足参考负载要求。两者分别测试，**不代表可以同时容纳两个负载**。第五状态来自输入/适用范围检查，不伪造有监督真值。", "",
             "| 参考合同 | 精度 | 显存下限 | 配额下限 | 单次工作量/访问量 | 延迟上限/吞吐下限 |", "|---|---|---|---|---|---|",
             "| training | dense IEEE FP32 vector | 8192 MiB | 20/100 | 2 TFLOP / 8 GB | 0.2s / 5次/s |",
             "| inference | dense IEEE FP32 vector | 4096 MiB | 10/100 | 0.06 TFLOP / 2.5 GB | 0.01s / 100次/s |", "",
             "理论时间 `max(work/(FP32*0.35*q), traffic/(bandwidth*0.55*q))`。0.35/0.55 和带宽随份额线性变化都是人为声明的假设。8192/4096MiB 是参考合同要求，绝非通用训练/推理显存门槛，也不是具体模型测试结果。", "",
             "例如 L40 剩余约44GB、40%配额：训练理论时间约0.158s、推理约0.0132s，故按该合同为训练优先；剩余7GB、95%配额：训练未达8192MiB下限，推理约0.00554s，故为推理优先。这是资源与参考合同的关系，不是 L40 的硬件算力发生变化。", "",
             "14 个数值输入=7个静态特征+5个动态特征+2个比例。低精度规格若累加精度/引擎口径不一致则保留原数并屏蔽给模型；FP32是本轮标签依据。可选缺失使用训练集拟合中位数+缺失指示；整列缺失使用0占位+指示，0并非真实算力。关键容量/带宽/FP32缺失直接待验证。", "",
             "计算配额 namespace 为 `synthetic_device_share_100_v1`，只是假想的设备份额。不同厂商原生核数、百分比或后端资源名没有等价映射时拒绝统一预测。多芯粒 MI250X 的合成池不包含拓扑局部性。", "",
             "已有训练/推理计数作为输入保留，但当前参考标签只通过实例上限使用这些计数，不加入未经验证的训推干扰规律。因此不能声称RF已学会真实共享干扰。原仿真的干扰属于独立合成执行假设。", "",
             "## 训练和最终测试", "",
             "使用 multi-output RandomForestClassifier 同时预测两目标；180棵树、max_features=0.85；min_samples_leaf∈{2,5}仅依据验证集选择，训练后不使用测试集调参。A按整条状态轨迹划分，B按型号组划分。样本ID、轨迹ID和特征哈希跨集合均无重叠。B额外保证型号组无重叠。前两轮模型和原拆分完整保留，本轮另建多型号拆分。", "",
             "运行前曾以固定40棵树做一次实现冒烟检查并查看 A/B 汇总；正式180棵树与两个候选叶节点大小预先固定，未按该测试结果更改特征、分组或参数搜索空间。本轮仍属开发期评估；论文最终结论应再冻结新独立型号测试集。", "",
             "| 协议 | 数据集 | 样本 | Accuracy | Macro-F1 | 误分类 |", "|---|---|---|---|---|---|"]
    for protocol, result in protocols.items():
        for split in ("train", "validation", "test"):
            m = result[split]
            lines.append(f"| {protocol} | {split} | {m['samples']} | {m['accuracy']:.4f} | {m['macro_f1']:.4f} | {m['misclassified']} |")
    lines += ["", "上表全部以**合成理论规则**为真值，不能据此声称真实吞吐/延迟预测准确，也不能据此声称调度优于其他方法。完整 Precision/Recall、类别数量和混淆矩阵见各协议 metrics.json、confusion.csv/png。原始 RF 的所有测试预测都计入指标，待验证机制没有隐藏错误。", "",
              "B 测试型号固定 A10、MI300X、MTT S4000；验证型号 A30、MI210、H100；其他10个型号组用于训练。仅16款静态设备，数千个合成状态不等于数千款硬件。", "",
              "| B 未见型号 | 样本 | Accuracy | Macro-F1 |", "|---|---|---|---|"]
    for device, m in protocols["B-unseen-models"]["per_test_model"].items():
        lines.append(f"| {device} | {m['samples']} | {m['accuracy']:.4f} | {m['macro_f1']:.4f} |")
    lines += ["", "Macro-F1 固定按四类平均；没有真值也没有预测的类别F1按0处理，需结合support理解。B的验证集和测试集均没有训练优先真值，因此这次B测试不能完整验证四类泛化；B验证四类macro-F1的理论上限为0.75。这也触发缺少类别验证依据的待验证保护。RF树不能可靠外推训练范围外的数值，硬件类型少、类别不平衡和跨型号性能交互均限制B表现。A10的误分类也说明仅检查数值范围不足以证明可靠性。下一步应先冻结包含四类参考状态的新独立型号测试集，按验证集研究剩余有效算力/带宽交互特征，仍需独立基准校准。", "",
              "## 未知型号和可信度", "",
              "检查关键参数、动态状态合法性、配额语义、静态数值是否超出训练范围、软件后端是否在合成训练域、验证集是否覆盖四类，以及验证Macro-F1是否达到预先设定0.75。任一范围/后端/质量问题返回待验证；保留 raw_rf 供诊断。即使通过，也只标注 provisional_synthetic_model_only。架构名称未知本身不触发拒绝。", "",
              "RF 投票比例只是树的输出，不是可靠性。待验证时返回硬约束结果和0.8倍有效能力的保守理论回退；该回退也不保证真实性能。未获得独立性能标签的未知设备仅展示预测，不进入真实性能准确率。NPU、未知后端和缺关键参数设备不接入 NVIDIA 执行模型。", "",
              "| 例子 | 最终结果 | 原因 |", "|---|---|---|"]
    for example in unknown:
        p = example["prediction"]
        lines.append(f"| {example['case']} | {p['label_zh']} | {'; '.join(p['reasons'])} |")
    lines += ["", "## 分配与释放演示", "", "构造 L40 的五步资源轨迹，每次修改后重新预测，静态硬件哈希保持一致。下表如实呈现 RF 输出；理论参考独立显示，不用其覆盖模型结果。", "",
              "| 操作 | 剩余MiB | 剩余配额 | 实例 | 训练预测 | 推理预测 | 最终分类 | 理论合同 |", "|---|---|---|---|---|---|---|---|"]
    for row in dynamic:
        s, p = row["state"], row["prediction"]
        lines.append(f"| {row['event']} | {s['free_memory_mib']} | {s['free_compute_quota']} | {s['allocated_vgpu_count']} | {p['training']} | {p['inference']} | {p['label_zh']} | {row['independent_reference_rule']['label']} |")
    lines += ["", "`static-demo.json` 包含22条设备的初始资源视图和硬件画像，缺失参数不补齐，无法判定的设备输出待验证。所有动态事件详见 `dynamic-demo.json`，每步包含硬件、资源、两个预测、原因、理论参考和数据类型。具体 Pod 的显存/算力/实例与软件兼容性硬约束仍优先；此轮旧执行器仅运行已支持的三款 NVIDIA 卡。", "",
              "## 原仿真兼容回归", "",
              "保持 Binpack、静态分类、动态分类三个策略。静态/动态分别通过适配器接入同一新A模型，静态冻结初始状态，动态逐次预测；Binpack不依赖RF。11张NVIDIA(4090×4、5090×4、L40×3)，相同60个Pod、随机种子101、执行种子707。执行时间仍由第二轮独立的合成性能模型计算，不使用第三轮标签规则。", "",
              "| 策略 | 完成数量 | 平均等待s | 训练JCTs | 推理Pod P95s |", "|---|---|---|---|---|"]
    for m in reg["summary"]:
        lines.append(f"| {m['strategy']} | {m['completed_total_after_drain']} | {m['mean_wait_s']:.3f} | {m['training_mean_jct_s']:.3f} | {m['inference_pod_response_p95_s']:.3f} |")
    lines += ["", "所有策略无资源超分配、结束后显存/配额恢复。这里只验证兼容性，60个Pod的单次回归不支持调度收益结论，资源利用率是预留配额/显存占比，不是实测GPU忙碌率。", "",
              "## 可复现命令", "", "在 gpu-scheduling-lab 目录 PowerShell：", "", "```powershell",
              "$env:TEMP=(Join-Path (Get-Location) '.tmp'); $env:TMP=$env:TEMP",
              "$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'", ".\\.venv\\Scripts\\python.exe -m gpu_lab.crossgpu.run --output results/offline-v3-replay",
              ".\\.venv\\Scripts\\python.exe -m pytest -q --basetemp .tmp/pytest-round3-replay", "```", "",
              "依赖沿用 requirements.txt；不需要CUDA。运行清单记录种子、Python/sklearn版本、代码哈希。pytest真实执行结果另存 pytest-results.txt/json。来源审计见 source-audit.md；全部分类监督标签来源为synthetic，规格数据为public_spec，真实性能标签为unknown。"]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "results/offline-v3")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error("Use a new output directory; historical results are never overwritten")
    if any(output == d.resolve() or d.resolve() in output.parents for d in (ROOT / "results").glob("offline-v[12]*")):
        parser.error("Cannot write inside historical v1/v2 results")
    output.mkdir(parents=True, exist_ok=True)
    before, start = legacy_snapshot(), time.perf_counter()
    devices, source = compile_catalog()
    source_audit = export_catalog(output, devices, source)
    shutil.copy2(CATALOG_SOURCE, output / "source-records.json")
    save_json(output / "reference-contract.json", CONTRACT)
    rows, dataset = generate(devices)
    write_jsonl(output / "synthetic-states.jsonl", rows)
    save_json(output / "dataset-summary.json", dataset)
    protocols, models = {}, {}
    for protocol, splits in (("A-known-models", split_known(rows)), ("B-unseen-models", split_unseen(rows))):
        print(f"{protocol}: training {len(splits['train'])} samples", flush=True)
        manifest = audit(splits, model_disjoint=protocol.startswith("B"))
        for name, batch in splits.items():
            write_jsonl(output / f"{protocol}-{name}.jsonl", batch)
        save_json(output / f"{protocol}-split-manifest.json", {**manifest, "sample_ids": {n: [r["sample_id"] for r in batch] for n, batch in splits.items()}})
        model = train(splits, devices)
        model.save(output / f"{protocol}-random-forest.joblib")
        save_json(output / f"{protocol}-model-metadata.json", model.metadata)
        protocols[protocol] = evaluate_protocol(model, splits, output, protocol, devices)
        models[protocol] = model
        print(protocol, {k: protocols[protocol]['test'][k] for k in ("accuracy", "macro_f1", "misclassified")}, flush=True)
    dynamic = dynamic_demo(models["A-known-models"], devices["l40"])
    unknown = unknown_demo(models["B-unseen-models"], devices)
    save_json(output / "static-demo.json", static_demo(models["A-known-models"], devices))
    save_json(output / "dynamic-demo.json", dynamic)
    save_json(output / "unknown-demo.json", unknown)
    print("Legacy NVIDIA-only simulation regression", flush=True)
    reg = regression(models["A-known-models"], devices)
    save_json(output / "simulation-regression.json", {k: v for k, v in reg.items() if k != "results"})
    write_csv(output / "simulation-regression.csv", reg["summary"])
    for name, result in reg["results"].items():
        write_jsonl(output / f"regression-{name}-events.jsonl", result["events"])
        write_csv(output / f"regression-{name}-pods.csv", result["pods"])
    after = legacy_snapshot()
    if before != after:
        raise AssertionError("Historical results changed")
    save_json(output / "historical-results-preserved.json", {"unchanged": True, "files": len(before), "sha256": before})
    report(output, source_audit, dataset, protocols, dynamic, unknown, reg, len(before))
    sources = list((ROOT / "gpu_lab").glob("*.py")) + list((ROOT / "gpu_lab/crossgpu").glob("*.py")) + list((ROOT / "tests").glob("*.py")) + [CATALOG_SOURCE]
    save_json(output / "run-manifest.json", {"seed": 20261016, "python": platform.python_version(), "sklearn": sklearn.__version__,
              "elapsed_s": time.perf_counter()-start, "feature_names": list(FEATURES),
              "source_sha256": {str(f.relative_to(ROOT)): sha256(f) for f in sources},
              "offline_only": True, "measured_samples": 0, "benchmark_samples": 0, "test_not_used_for_hyperparameter_selection": True})
    print(json.dumps({"output": str(output), "samples": len(rows), "historical_files_unchanged": len(before)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
