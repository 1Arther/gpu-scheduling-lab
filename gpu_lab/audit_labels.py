"""Audit the saved reference definition without regenerating or relabeling v1."""
from pathlib import Path

from .labeling import ReferenceOracle
from .profiles import load_profiles
from .resources import Allocation, GPUState, ResourceRequest
from .run import save_json


def audit_labels(output: Path):
    profiles, oracle = load_profiles(), ReferenceOracle.load()
    cases = []
    for name, free_memory, free_compute in (("40pct_compute", 44776, 40), ("95pct_compute_7000mib", 7000, 95)):
        state = GPUState(name, profiles["l40"])
        state.allocate(Allocation("background", "training", ResourceRequest(
            state.profile.modeled_memory_mib - free_memory, 100 - free_compute)))
        cases.append({"case": name, "remaining_memory_mib": free_memory,
                      "remaining_memory_gib": free_memory / 1024,
                      "remaining_compute_units": free_compute, **oracle.evaluate(state)})
    output.mkdir(parents=True, exist_ok=True)
    save_json(output / "label-audit.json", {"data_kind": "audit_of_declared_synthetic_reference_rules", "cases": cases,
        "contract": oracle.contract,
        "definition_correction": "Names are reference-contract feasibility states, not vendor role labels or empirical hardware capabilities"})
    lines = ["# 第二轮：第一轮标签审查", "",
             "保留第一轮标签、模型和三个划分，不改写历史结果。修正的是解释及使用方式：四类是两种固定参考负载的可行性组合，不是 GPU 固有的训推属性。", "",
             "| 参考任务 | 显存 | 最低配额 | 运算/次 | 显存流量/次 | 延迟上限 | 吞吐下限 |",
             "|---|---:|---:|---:|---:|---:|---:|",
             "| 训练 step | 8192 MiB | 20/100 | 2 TFLOP | 8 GB | 200 ms | 5 step/s |",
             "| 推理 request | 4096 MiB | 10/100 | 0.06 TFLOP | 2.5 GB | 10 ms | 100 request/s |", "",
             "固定假设：稠密非 Tensor FP32 峰值效率 0.35、带宽效率 0.55、带宽按剩余算力比例分配。全部是人为声明的合成假设，没有真实性能标定。", "",
             "公式：`t=max(work/(peak_fp32*0.35*q), traffic/(bandwidth*0.55*q))`。参考配额可弹性增加，实际所需配额由性能门槛反解；L40 训练至少 32/100、推理至少 53/100。", "",
             "| L40 合成状态 | 训练解析延迟 | 推理解析延迟 | 解释 |",
             "|---|---:|---:|---|"]
    for row in cases:
        d = row["reference_details"]
        reason = ("训练达标；推理超过 10ms" if row["case"] == "40pct_compute"
                  else "训练虽有足够算力，但显存不足 8192MiB；推理达标")
        lines.append(f"| {row['remaining_memory_mib']} MiB，{row['remaining_compute_units']}% 配额 | "
                     f"{d['training']['analytical_latency_s']*1000:.3f} ms | {d['inference']['analytical_latency_s']*1000:.3f} ms | {reason} |")
    lines += ["", "44,776 MiB 是约 43.73 GiB；7,000 MiB 是约 6.84 GiB。不能混用 GB/GiB/MiB。", "",
              "训练优先 = 仅训练参考达标；推理优先 = 仅推理参考达标；训推一体 = 两种参考分别达标；暂不可用 = 两种参考都不达标。训推一体不保证两任务同时放入仍达标，暂不可用也不代表更小 Pod 不能运行。", "",
              "两个示例不能证明资源变化必然导致真实 GPU 的训练/推理能力转换。最小显存是负载合同；效率、带宽隔离和 SLO 是假设。人工规则相对自身标签为 100% 是定义上的一致性，不是性能验证。", "",
              "第二轮保留四类展示，但所有候选卡先通过当前 Pod 的显存/算力/实例硬约束；可行候选按 RF 软概率、当前 Pod 申请、硬件相对特征、资源碎片和共享情况综合排序。分类标签不再充当 Pod 准入条件。", ""]
    (output / "label-audit.md").write_text("\n".join(lines), encoding="utf-8")
    return cases
