"""python -m gpu_lab.run experiment|demo --help. All outputs are local files."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import platform
import sys
import time

import numpy as np
import sklearn
from sklearn.metrics import accuracy_score, f1_score

from .dataset import audit_splits, generate_dataset, split_dataset, write_jsonl
from .dynamic import DynamicGPU
from .fleet import create_fleet
from .labeling import DEFAULT_CONTRACT, LABELS, LABEL_NAMES_ZH, ReferenceOracle
from .model import SuitabilityModel, evaluate_model, train_model
from .profiles import DEFAULT_PROFILES, ROOT, load_profiles
from .resources import Allocation, GPUState, ResourceRequest


def save_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dynamic_demo(model, profiles, oracle):
    # A deliberately constructed resource trajectory to exercise the four outputs.
    # It is a software demonstration, not a runtime performance experiment.
    state = GPUState("synthetic-l40-demo-0", profiles["l40"])
    gpu = DynamicGPU(state, model)
    gpu.allocate(Allocation("compute-heavy", "training", ResourceRequest(1000, 60)))
    gpu.allocate(Allocation("memory-heavy", "inference", ResourceRequest(state.profile.modeled_memory_mib - 7000, 5)))
    gpu.release("compute-heavy")
    gpu.release("memory-heavy")
    # Reference labels are displayed alongside RF predictions for inspection only.
    replay = GPUState("synthetic-l40-demo-0", profiles["l40"])
    for row in gpu.history:
        snapshot = row["state"]
        replay = GPUState(snapshot["gpu_id"], profiles[snapshot["profile_id"]])
        for a in snapshot["allocations"]:
            replay.allocate(Allocation(a["allocation_id"], a["task_type"], ResourceRequest(**a["request"])))
        row["reference_assessment"] = oracle.evaluate(replay)
    fleet = [{"state": state.snapshot(), "prediction": model.predict(state)}
             for state in create_fleet(profiles).values()]
    return {"data_kind": "synthetic_dynamic_demo", "profile_id": "l40", "events": gpu.history,
            "initial_offline_fleet": fleet,
            "note": "Reservations chosen to illustrate all four reference classes, not a sampled production trace"}


def write_report(path, metrics, demo):
    lines = [
        "# 离线第一轮运行报告", "",
        "全部指标来自合成资源状态和解析参考标签，没有执行真实 GPU 任务，也没有访问或修改集群。", "",
        f"种子：`{metrics['seed']}`；模型：RandomForest；硬件：RTX 4090 / RTX 5090 / L40。", "",
        "| 划分 | 样本数 | Accuracy | Macro-F1 |",
        "|---|---:|---:|---:|",
    ]
    for name, result in metrics["classification"].items():
        lines.append(f"| {name} | {result['samples']} | {result['accuracy']:.6f} | {result['macro_f1']:.6f} |")
    lines += ["", "## 测试集各类指标", "",
              "| 类别 | Precision | Recall | F1 | 样本数 |", "|---|---:|---:|---:|---:|"]
    for label in LABELS:
        r = metrics["classification"]["test"]["per_class"][label]
        lines.append(f"| {LABEL_NAMES_ZH[label]} | {r['precision']:.6f} | {r['recall']:.6f} | {r['f1-score']:.6f} | {int(r['support'])} |")
    lines += ["", "## 分配与释放演示", "",
              "| 事件 | 剩余显存 MiB | 剩余算力配额 /100 | RF 预测 | 解析参考标签 |",
              "|---|---:|---:|---|---|"]
    for event in demo["events"]:
        s = event["state"]
        lines.append(f"| {event['event']} | {s['remaining_memory_mib']} | {s['remaining_compute_units']} | "
                     f"{LABEL_NAMES_ZH[event['prediction']['label']]} | {LABEL_NAMES_ZH[event['reference_assessment']['label']]} |")
    lines += ["", "## 结论边界", "",
              "- 训练/验证/测试按独立分配释放轨迹划分；重复特征在划分前全局去重；重叠审计见 metrics.json。",
              "- 只在训练集拟合 RF 和缺失值填补器；固定超参数；验证集仅报告，测试集只做最终评估。",
              "- 高分表示拟合参考负载可行性规则，不是对真实训练/推理性能的独立验证。",
              "- 只覆盖这三种卡的新合成状态；不能据此声称其他芯片/厂商上的泛化能力。",
              "- 算力配额不是 GPU 实时利用率，按配额比例分配带宽只是标签生成假设。",
              "- 暂不可用表示两种参考负载都不满足，不代表不能运行任何更小任务。",
              "- 下一轮实现 binpack/静态分类/动态分类和独立执行时间模型，当前没有调度收益数据。",
              "- 真实测试数据数量为 0。既有 phase1 环境快照不是本轮性能测试。", ""]
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def experiment(args):
    started = time.perf_counter()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    profiles = load_profiles()
    oracle = ReferenceOracle.load()
    rows, generation = generate_dataset(profiles, oracle, args.seed, args.episodes_per_profile, args.steps)
    splits = split_dataset(rows, args.seed)
    audit = audit_splits(splits)
    for name, batch in splits.items():
        if set(r["label"] for r in batch) != set(LABELS):
            raise ValueError(f"Missing reference class in {name}; increase synthetic episodes, do not fabricate metrics")
        write_jsonl(output / f"{name}.jsonl", batch)
    metadata = {
        "data_kind": "synthetic_training_data",
        "hardware_catalog_sha256": sha256(DEFAULT_PROFILES),
        "reference_contract_sha256": sha256(DEFAULT_CONTRACT),
        "contract_id": oracle.contract["contract_id"],
    }
    model = train_model(splits["train"], args.seed, args.trees, metadata)
    metrics = {
        "data_kind": "evaluation_against_synthetic_analytical_reference_labels",
        "real_gpu_performance_measurements": 0,
        "seed": args.seed,
        "generation": generation,
        "split_audit": audit,
        "model": model.metadata,
        "classification": {},
        "versions": {"python": platform.python_version(), "numpy": np.__version__, "scikit_learn": sklearn.__version__},
    }
    majority = Counter(r["label"] for r in splits["train"]).most_common(1)[0][0]
    all_predictions = {}
    for name, batch in splits.items():
        result, predictions = evaluate_model(model, batch)
        all_predictions[name] = predictions
        metrics["classification"][name] = result
        write_jsonl(output / f"{name}-predictions.jsonl", [
            {"sample_id": r["sample_id"], "reference_label": r["label"], "predicted_label": p,
             "data_kind": "prediction_on_synthetic_state"} for r, p in zip(batch, predictions)
        ])
    y_test = [r["label"] for r in splits["test"]]
    metrics["majority_baseline_test"] = {
        "label_selected_from_train": majority,
        "accuracy": float(accuracy_score(y_test, [majority] * len(y_test))),
        "macro_f1": float(f1_score(y_test, [majority] * len(y_test), labels=list(LABELS), average="macro", zero_division=0)),
    }
    # Per-hardware slices reuse the already-computed test predictions; do not refit or select.
    test_predictions = all_predictions["test"]
    metrics["test_by_profile"] = {}
    for profile_id in profiles:
        pairs = [(r["label"], p) for r, p in zip(splits["test"], test_predictions) if r["profile_id"] == profile_id]
        truth, prediction = zip(*pairs)
        metrics["test_by_profile"][profile_id] = {"samples": len(pairs), "accuracy": float(accuracy_score(truth, prediction)),
            "macro_f1_fixed_four_classes": float(f1_score(truth, prediction, labels=list(LABELS), average="macro", zero_division=0)),
            "class_counts": dict(Counter(truth)),
            "note": "Fixed-four-class macro-F1 includes zero for any class absent on this profile; inspect support counts"}
    model.save(output / "random_forest.joblib")
    demo = dynamic_demo(model, profiles, oracle)
    save_json(output / "dynamic-demo.json", demo)
    save_json(output / "dataset-metadata.json", {**metadata, **generation, "split_audit": audit})
    metrics["elapsed_seconds"] = time.perf_counter() - started
    save_json(output / "metrics.json", metrics)
    # Keep exact copies beside the artifact; provenance survives future catalog/contract changes.
    (output / "hardware-catalog.json").write_bytes(DEFAULT_PROFILES.read_bytes())
    (output / "reference-contract.json").write_bytes(DEFAULT_CONTRACT.read_bytes())
    write_report(output / "report.md", metrics, demo)
    print(json.dumps({"output": str(output), "samples": generation["samples"], "split_sizes": audit["sizes"],
                      "test_accuracy": metrics["classification"]["test"]["accuracy"],
                      "test_macro_f1": metrics["classification"]["test"]["macro_f1"],
                      "split_leakage_audit_passed": audit["passed"],
                      "elapsed_seconds": metrics["elapsed_seconds"]}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("experiment", help="Generate, split, fit RF, evaluate and demonstrate")
    exp.add_argument("--output", type=Path, default=ROOT / "results/offline-v1")
    exp.add_argument("--seed", type=int, default=20261016)
    exp.add_argument("--episodes-per-profile", type=int, default=300)
    exp.add_argument("--steps", type=int, default=16)
    exp.add_argument("--trees", type=int, default=240)
    demo = sub.add_parser("demo", help="Replay allocation/release with a trusted saved model")
    demo.add_argument("--model", type=Path, default=ROOT / "results/offline-v1/random_forest.joblib")
    args = parser.parse_args()
    if args.command == "experiment":
        experiment(args)
    else:
        model = SuitabilityModel.load(args.model)
        if (model.metadata["hardware_catalog_sha256"] != sha256(DEFAULT_PROFILES)
                or model.metadata["reference_contract_sha256"] != sha256(DEFAULT_CONTRACT)):
            raise ValueError("Public catalog or reference contract changed; retrain before this demo")
        result = dynamic_demo(model, load_profiles(), ReferenceOracle.load())
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
