"""Run the second offline iteration; no cluster commands or remote APIs."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import platform
import statistics
import sys
import time

import numpy as np
import sklearn

from .audit_labels import audit_labels
from .comparison import run_classification_comparison, write_csv
from .dataset import write_jsonl
from .execution import DEFAULT_EXECUTION, ExecutionModel
from .fleet import create_fleet, DEFAULT_LAYOUT
from .profiles import load_profiles, ROOT
from .resources import Allocation, ResourceRequest
from .round2_plots import make_plots
from .run import save_json, sha256
from .scoring import DEFAULT_SCORING, PodRequest, PodScorer
from .simulation import simulate
from .strategies import Scheduler, STRATEGIES
from .workloads import generate_trace, SCENARIOS, trace_hash

METRICS = ("mean_wait_s", "wait_p95_s", "training_mean_wait_s", "training_mean_jct_s", "training_mean_runtime_s",
           "inference_mean_wait_s", "inference_pod_response_p95_s", "inference_pod_mean_response_s",
           "inference_mean_serial_service_per_request_s", "inference_batch_slo_met_fraction",
           "inference_completed_batch_requests_per_s_within_horizon", "reserved_compute_utilization",
           "reserved_memory_utilization", "active_gpu_utilization", "completed_within_horizon",
           "completed_total_after_drain", "unfinished_at_horizon", "rejected_pods", "drain_makespan_s")


def summarize(metrics):
    groups = defaultdict(list)
    for row in metrics:
        groups[(row["scenario"], row["interference"], row["strategy"])].append(row)
    result = []
    for (scenario, interference, strategy), group in sorted(groups.items()):
        row = {"scenario": scenario, "interference": interference, "strategy": strategy, "seeds": len(group)}
        for key in METRICS:
            values = [r[key] for r in group if r[key] is not None]
            row[key + "_mean"] = statistics.mean(values) if values else None
            row[key + "_std"] = statistics.stdev(values) if len(values) > 1 else 0.
        result.append(row)
    return result


def paired_results(metrics):
    cohorts = defaultdict(dict)
    for row in metrics:
        cohorts[(row["scenario"], row["trace_seed"], row["interference"])][row["strategy"]] = row
    results = []
    for (scenario, seed, level), by_strategy in sorted(cohorts.items()):
        if len({r["trace_sha256"] for r in by_strategy.values()}) != 1:
            raise AssertionError("Strategies did not use identical traces")
        for baseline in ("binpack", "static_rf"):
            dynamic, base = by_strategy["dynamic_rf"], by_strategy[baseline]
            row = {"scenario": scenario, "trace_seed": seed, "interference": level, "baseline": baseline,
                   "trace_sha256": dynamic["trace_sha256"]}
            for key in METRICS:
                if base[key] is None or dynamic[key] is None:
                    row[key + "_dynamic_minus_baseline"] = None
                    row[key + "_relative_change_pct"] = None
                else:
                    row[key + "_dynamic_minus_baseline"] = dynamic[key] - base[key]
                    row[key + "_relative_change_pct"] = (dynamic[key] / base[key] - 1) * 100 if abs(base[key]) > 1e-12 else None
            results.append(row)
    return results


def write_report(output, comparison, metrics, summary, config, ablation_rows=None):
    lines = ["# 第二轮离线实验报告", "", "全部为合成资源状态及合成执行性能，没有真实性能标签，没有连接或修改实际 Kubernetes/Volcano。", "",
             "## 标签审查", "", "详见 [标签逐项审查](label-audit.md)。训练优先/推理优先是固定参考合同的可行性组合，不是硬件固有能力，也不是厂商的产品定位。第一轮文件与划分保持不变，SHA256 清单见 first-round-preserved-sha256.json。", "",
             "## 分类对照：对人工参考标签的一致性", "", "| 划分 | 方法 | Accuracy | Macro-F1 | 错分/样本 |", "|---|---|---:|---:|---:|"]
    for split, methods in comparison["results"].items():
        for method, r in methods.items():
            lines.append(f"| {split} | {method} | {r['accuracy']:.6f} | {r['macro_f1']:.6f} | {r['misclassified']}/{r['samples']} |")
    lines += ["", "人工规则为标签生成器，因此其 100% 是定义上的一致，不是独立性能验证。静态 RF 使用相同训练集和树参数，仅保留五个硬件特征；在原验证集上比较 None / balanced_subsample 两种类别权重，按 Macro-F1 选择。候选指标见 classification-comparison.json。直接沿用平衡权重的弱基线已在 smoke 结果保留，不作为最终主基线。动态 RF 直接复用第一轮已保存模型。验证/测试数据不参与拟合；只验证集参与上述基线选择。", "",
              "测试误分类明细为 test-errors-*.csv，四类支持数、Precision/Recall/F1 和混淆矩阵见 classification-comparison.json。最终静态 RF 在测试集都预测 balanced；相同硬件对应许多不同动态标签，单靠静态输入不能识别这些状态。其连续概率仍可随卡型变化。", "",
              "## Pod 评分与准入", "",
              "所有策略首先执行同一个显存/算力/实例数硬约束。四分类只作软提示，unavailable 仍可能接纳更小 Pod。", "",
              "动态评分 = 0.30 × RF 对当前任务类型的参考可行概率 + 0.45 × 申请配额下的硬件/共享代理值 + 0.15 × 装箱紧凑度 + 0.10 × 剩余资源平衡度。", "",
              "RF 是学习部分；其余为显式启发式，并非训练出的真实性能预测器。评分输入包含原始硬件、动态状态、已有类型和当前 Pod 申请；调度器看不到执行 work、隐藏压力或执行 oracle。详见 scoring-policy.json、pod-scoring-demo.json。", "",
              "Binpack 按分配后归一化剩余显存/算力/实例数排序；静态 RF 按类型概率排序并以 binpack 破同分；动态策略按上述 Pod 评分排序。三者使用相同 FCFS 可行回填、固定申请、不抢占、不自动扩缩配额。", "",
              "## 独立执行时间模型及公平性", "",
              "11 卡离线布局为 4090×4、5090×4、L40×3。每个 Pod 本轮申请一个 vGPU、落在一张物理 GPU；不模拟跨卡或多节点训练通信。", "",
              "执行模型是硬件加权调和速度 × Amdahl 配额缩放 ÷ 同卡共享干扰 × 可复现扰动；与参考标签的 roofline/SLO 公式不同，不读取 RF 类别、概率或评分。参数为独立冻结的合成假设，不是实测标定。模块隔离可以排除直接循环取值，但不能消除同一组人工性能假设带来的偏好。", "",
              "每次分配和释放重新计算同卡所有任务的进度速率，已运行任务会受后来任务干扰；不是启动时一次算定 duration。扰动由执行种子、Pod ID、GPU 型号的 SHA256 决定，不随策略的事件调用次数变化。", "",
              f"主实验：{len(config['scenarios'])} 种负载 × {len(config['seeds'])} 个种子 × 3 策略；每条轨迹 {config['pods']} 个 Pod；共同观察窗 {config['horizon_s']:g}s。",
              "同一配对实验的 trace SHA256、种子、执行配置、干扰倍率、观察窗和硬约束相同。没有策略按未来到达或隐藏工作量选卡。", "",
              "## 仿真结果", "",
              "以下为配对种子的算术均值；原始每次运行见 simulation-metrics.csv，均值/样本标准差见 simulation-summary.csv，配对差值见 paired-comparisons.csv。", "",
              "| 场景 | 策略 | 平均等待 s | 训练 JCT s | 推理批次 Pod 响应 P95 s | 算力预留率 | 显存预留率 | 窗内完成 Pod |",
              "|---|---|---:|---:|---:|---:|---:|---:|"]
    main = [r for r in summary if r["interference"] == 1.]
    for r in main:
        lines.append(f"| {r['scenario']} | {r['strategy']} | {r['mean_wait_s_mean']:.3f} | {r['training_mean_jct_s_mean']:.3f} | "
                     f"{r['inference_pod_response_p95_s_mean']:.3f} | {r['reserved_compute_utilization_mean']:.2%} | {r['reserved_memory_utilization_mean']:.2%} | {r['completed_within_horizon_mean']:.2f} |")
    lines += ["", "## 结果解释与限制", ""]
    for scenario in config["scenarios"]:
        rows = {r["strategy"]: r for r in main if r["scenario"] == scenario}
        dynamic, base = rows["dynamic_rf"], rows["binpack"]
        change = lambda key: (dynamic[key+"_mean"] / base[key+"_mean"] - 1)*100 if base[key+"_mean"] else 0
        lines.append(f"- {scenario}：动态策略相对 Binpack，训练 JCT 变化 {change('training_mean_jct_s'):+.2f}%，推理批次 Pod 响应 P95 变化 {change('inference_pod_response_p95_s'):+.2f}%。正数表示更慢；不隐藏失败场景。")
    selection_path = output / "policy-validation-selection.json"
    if selection_path.exists():
        selected = json.loads(selection_path.read_text(encoding="utf-8"))
        lines += ["", "## 算法改进尝试与独立验证", "",
                  f"在调度验证种子 {selected['validation_seeds']} 上比较原评分、两个装箱邻域约束版本及仅对显存受限训练任务启用约束的版本。正式实验种子与验证种子不重叠。验证目标在 policy-validation-selection.json 中固定记录；本轮选择 `{selected['selected']['variant']}`。", "",
                  "| 验证候选 | 归一化综合成本（低好） |", "|---|---:|"]
        for candidate in selected["candidates"]:
            if candidate["eligible_for_main_ML_policy"]:
                lines.append(f"| {candidate['variant']} | {candidate['normalized_validation_cost']:.6f} |")
        lines += ["", "部分装箱约束版本在大显存场景改善，却在其他场景损失，故未强行作为最终版本。原评分及改进候选都保留；这不是调参到动态算法必胜。验证指标与原始记录见 policy-validation-*.csv。", ""]
    if ablation_rows:
        lines += ["", "## RF 排序贡献消融", "",
                  "使用相同评分结构、装箱约束、硬件/共享启发式；只把 RF 概率权重置零并同比例归一化其他权重。只在主干扰倍率 1 下运行，使用相同正式轨迹。", "",
                  "| 场景 | 含 RF 训练 JCT | 无 RF 训练 JCT | 含 RF 推理批次 P95 | 无 RF 推理批次 P95 |",
                  "|---|---:|---:|---:|---:|"]
        for scenario in config["scenarios"]:
            variants = {v: [r for r in ablation_rows if r["scenario"] == scenario and r["variant"] == v] for v in ("with_RF", "without_RF")}
            w, n = variants["with_RF"], variants["without_RF"]
            avg = lambda rows, key: statistics.mean(r[key] for r in rows)
            lines.append(f"| {scenario} | {avg(w, 'training_mean_jct_s'):.3f} | {avg(n, 'training_mean_jct_s'):.3f} | {avg(w, 'inference_pod_response_p95_s'):.3f} | {avg(n, 'inference_pod_response_p95_s'):.3f} |")
        lines += ["", "RF 增量要看这张表，不能把混合评分相对 Binpack 的全部变化归因于机器学习。RF 分类准确率高，不保证这一人工参考分类信号能改善任意 Pod 的调度。原始配对值见 rf-ablation.csv。", ""]
    lines += ["", "可能影响结果的机制：静态类别无法反映剩余配额；参考合同不等于当前 Pod；硬件/共享代理值偏好快卡或分散放置，装箱可能更紧凑；大显存 Pod 只能使用部分卡，存在容量碎片和队列阻塞。当前为贪心排序，没有完整的未来需求规划。", "",
              "这些机制是根据策略实现提出的解释，不是通过本轮数据证明的因果关系；分类和调度收益必须分别评估。当前不能把混合评分的全部收益归因于 RF。", "",
              "指标口径：等待=Pod start-arrival；训练 JCT=finish-arrival；runtime=finish-start。所有可行任务最终排空，均值含观察窗后完成的任务，避免只统计快任务；另报共同窗内完成数量、窗末未完成数量。", "",
              "资源利用率为观察窗内预留算力/显存的积分除以总容量和时间，不是 nvidia-smi 或 CUDA 实测利用率。预留率更高也不必然代表效率更好。", "",
              "推理 Pod 表示有限串行请求批次：P95 是批次 Pod 从到达到完成的响应时间；平均每请求服务时间为累计批次运行时间/请求数；吞吐为共同窗内已完成整批的请求数/时间；SLO 为合成批次响应预算。没有逐 token 延迟、在线服务排队模型或真实请求级 P95。", "",
              "误差棒为配对种子的样本标准差，不是置信区间或显著性检验。零/双倍干扰敏感性见 figures/interference-sensitivity.png；不保证任何策略在其他执行假设下获胜。未模拟控制平面、容器启动、网络或调度模型的实际推理耗时。", "",
              "## 图表与运行", "", "![分类对照](figures/classification-comparison.png)", "", "![三策略对照](figures/scheduling-comparison.png)", "",
              "SVG 可直接导出论文。完整命令与选择过程见项目 README 和 run-config.json；测试：`python -m pytest -q`。", ""]
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")


def run(args):
    started = time.perf_counter()
    output, first = args.output.resolve(), args.first_round.resolve()
    if output == first or first in output.parents:
        raise ValueError("Second-round output must not overwrite or nest inside the first-round artifacts")
    output.mkdir(parents=True, exist_ok=True)
    profiles = load_profiles()
    audit_labels(output)
    print("Audited v1 labels; loading frozen splits and fitting static RF only.", flush=True)
    static, dynamic, comparison, preserved = run_classification_comparison(first, output, profiles)
    config = {"scenarios": args.scenarios, "seeds": args.seeds, "pods": args.pods, "horizon_s": args.horizon,
              "execution_seed_offset": 20261016, "sensitivity": not args.skip_sensitivity,
              "queue_policy": "FCFS feasible backfill", "real_cluster_changes": False,
              "scoring_policy_source": str(args.scoring_policy.resolve()),
              "no_rf_policy_source": str(args.no_rf_policy.resolve()) if args.no_rf_policy else None,
              "versions": {"python": platform.python_version(), "numpy": np.__version__, "sklearn": sklearn.__version__}}
    for src, name in ((DEFAULT_EXECUTION, "execution-model.json"), (args.scoring_policy, "scoring-policy.json"), (DEFAULT_LAYOUT, "cluster-layout.json")):
        (output / name).write_bytes(src.read_bytes())
        config[name + "_sha256"] = sha256(src)
    save_json(output / "run-config.json", config)
    selection_path = args.scoring_policy.with_name("selection.json")
    if selection_path.exists():
        selection = json.loads(selection_path.read_text(encoding="utf-8"))
        if set(selection["validation_seeds"]) & set(args.seeds):
            raise ValueError("Final scheduling evaluation seeds overlap with policy-validation seeds")
        (output / "policy-validation-selection.json").write_bytes(selection_path.read_bytes())
        for file in ("validation-policy-metrics.csv", "validation-policy-objectives.csv"):
            source = selection_path.parent / file
            if source.exists():
                (output / file.replace("validation-policy-", "policy-validation-")).write_bytes(source.read_bytes())
    fleet = create_fleet(profiles)
    l40 = next(s for s in fleet.values() if s.profile.profile_id == "l40")
    l40.allocate(Allocation("synthetic-background", "training", ResourceRequest(1000, 60)))
    scorer = PodScorer(dynamic, profiles, path=args.scoring_policy)
    score_demo = []
    for task_type, memory, compute in (("training", 16384, 35), ("inference", 4096, 15), ("inference", 512, 5)):
        score_demo.append({"task_type": task_type, "memory_mib": memory, "compute_units": compute,
                           "candidates": scorer.score_many(list(fleet.values()), PodRequest(task_type, ResourceRequest(memory, compute)))})
    save_json(output / "pod-scoring-demo.json", score_demo)
    metrics, representative = [], {}
    for scenario in args.scenarios:
        for seed in args.seeds:
            pods = generate_trace(scenario, seed, args.pods)
            trace_dir = output / "traces"
            trace_dir.mkdir(exist_ok=True)
            write_jsonl(trace_dir / f"{scenario}-{seed}.jsonl", [p.record() for p in pods])
            levels = (1.,) if args.skip_sensitivity or scenario != "bursty_mixed" else (0., 1., 2.)
            for level in levels:
                for name in STRATEGIES:
                    scheduler = Scheduler(name, profiles, static, dynamic, scoring_path=args.scoring_policy)
                    result = simulate(pods, profiles, scheduler, ExecutionModel(seed=20261016+seed, interference=level), args.horizon)
                    row = {"scenario": scenario, "trace_seed": seed, **result["metrics"]}
                    metrics.append(row)
                    run_dir = output / "runs" / f"{scenario}-seed{seed}-i{level:g}-{name}"
                    run_dir.mkdir(parents=True, exist_ok=True)
                    save_json(run_dir / "metrics.json", row)
                    write_csv(run_dir / "pods.csv", result["pods"])
                    write_csv(run_dir / "timeline.csv", result["timeline"])
                    write_jsonl(run_dir / "events.jsonl", result["events"])
                    if scenario == args.scenarios[0] and seed == args.seeds[0] and level == 1.:
                        representative[name] = result
                    print(f"{scenario} seed={seed} interference={level:g} {name}: wait={row['mean_wait_s']:.2f}s training_JCT={row['training_mean_jct_s']:.2f}s completed={row['completed_within_horizon']}/{args.pods}", flush=True)
                    write_csv(output / "simulation-metrics.csv", metrics)
    summary = summarize(metrics)
    write_csv(output / "simulation-summary.csv", summary)
    write_csv(output / "paired-comparisons.csv", paired_results(metrics))
    save_json(output / "simulation-results.json", {"config": config, "runs": metrics, "summary": summary})
    ablation_rows = []
    if args.no_rf_policy:
        (output / "no-rf-ablation-policy.json").write_bytes(args.no_rf_policy.read_bytes())
        for scenario in args.scenarios:
            for seed in args.seeds:
                pods = generate_trace(scenario, seed, args.pods)
                result = simulate(pods, profiles, Scheduler("dynamic_rf", profiles, static, dynamic, scoring_path=args.no_rf_policy),
                                  ExecutionModel(seed=20261016+seed), args.horizon)
                actual = next(r for r in metrics if r["scenario"] == scenario and r["trace_seed"] == seed and r["strategy"] == "dynamic_rf" and r["interference"] == 1.)
                ablation_rows.extend([{**actual, "variant": "with_RF"},
                                      {"scenario": scenario, "trace_seed": seed, **result["metrics"], "variant": "without_RF"}])
                print(f"Ablation {scenario} seed={seed}: with_RF wait={actual['mean_wait_s']:.2f}, without_RF wait={result['metrics']['mean_wait_s']:.2f}", flush=True)
        write_csv(output / "rf-ablation.csv", ablation_rows)
    make_plots(output / "figures", comparison, summary, args.scenarios, representative)
    write_report(output, comparison, metrics, summary, config, ablation_rows)
    after = {f.name: sha256(f) for f in sorted(first.iterdir()) if f.is_file()}
    if after != preserved:
        raise AssertionError("First-round artifacts changed during second-round run")
    save_json(output / "preservation-check.json", {"passed": True, "files_checked": len(after), "sha256": after})
    save_json(output / "source-sha256.json", {str(f.relative_to(ROOT)): sha256(f) for f in sorted((ROOT / "gpu_lab").glob("*.py"))})
    print(json.dumps({"output": str(output), "simulation_runs": len(metrics), "first_round_preserved": True,
                      "classification_test": {name: {k: values[k] for k in ("accuracy", "macro_f1", "misclassified")}
                                              for name, values in comparison["results"]["test"].items()},
                      "elapsed_seconds": time.perf_counter()-started}, ensure_ascii=False, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "results/offline-v2")
    parser.add_argument("--first-round", type=Path, default=ROOT / "results/offline-v1")
    parser.add_argument("--seeds", type=int, nargs="+", default=[11, 29, 47])
    parser.add_argument("--pods", type=int, default=120)
    parser.add_argument("--scenarios", choices=SCENARIOS, nargs="+", default=list(SCENARIOS))
    parser.add_argument("--horizon", type=float, default=600.)
    parser.add_argument("--skip-sensitivity", action="store_true")
    parser.add_argument("--scoring-policy", type=Path, default=DEFAULT_SCORING)
    parser.add_argument("--no-rf-policy", type=Path, default=None, help="Optional matched heuristic-only ablation policy")
    args = parser.parse_args()
    if len(args.seeds) != len(set(args.seeds)) or len(args.scenarios) != len(set(args.scenarios)):
        parser.error("Duplicate seeds/scenarios would duplicate experiments")
    run(args)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
