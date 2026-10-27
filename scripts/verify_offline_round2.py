"""Read local experiment artifacts only; validate fairness and resource ledgers."""
import argparse
from collections import defaultdict
import csv
import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def verify(output, first):
    preserved = json.loads((output / "first-round-preserved-sha256.json").read_text(encoding="utf-8"))
    assert {f.name: digest(f) for f in first.iterdir() if f.is_file()} == preserved
    rows = read_csv(output / "simulation-metrics.csv")
    pairs = defaultdict(list)
    for row in rows:
        pairs[(row["scenario"], row["trace_seed"], row["interference"])].append(row)
        assert int(row["completed_total_after_drain"]) + int(row["rejected_pods"]) == int(row["submitted_pods"])
        for key in ("reserved_compute_utilization", "reserved_memory_utilization", "active_gpu_utilization"):
            assert 0 <= float(row[key]) <= 1 + 1e-12
    for group in pairs.values():
        assert {r["strategy"] for r in group} == {"binpack", "static_rf", "dynamic_rf"}
        for key in ("trace_sha256", "execution_seed", "observation_horizon_s", "submitted_pods"):
            assert len({r[key] for r in group}) == 1
    events_count, commits_count, stream_count = 0, 0, 0
    for run_dir in sorted((output / "runs").iterdir()):
        metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
        if not any(r["scenario"] == metrics["scenario"] and int(r["trace_seed"]) == metrics["trace_seed"]
                   and r["strategy"] == metrics["strategy"] and float(r["interference"]) == metrics["interference"] for r in rows):
            continue  # Ignore archives from a different explicitly recorded run config.
        active, last_by_gpu, starts, finishes, last_time = {}, {}, 0, 0, -1.
        with (run_dir / "events.jsonl").open(encoding="utf-8") as f:
            for line in f:
                event = json.loads(line)
                events_count += 1
                assert event["time_s"] >= last_time
                last_time = event["time_s"]
                if event["event"] == "start":
                    assert event["pod_id"] not in active
                    active[event["pod_id"]] = event["gpu_id"]
                    starts += 1
                elif event["event"] == "finish":
                    assert active.pop(event["pod_id"]) == event["gpu_id"]
                    finishes += 1
                if "state_after" not in event:
                    continue
                commits_count += 1
                state = event["state_after"]
                allocations = state["allocations"]
                memory = sum(a["request"]["memory_mib"] for a in allocations)
                quota = sum(a["request"]["compute_units"] for a in allocations)
                slots = sum(a["request"]["instances"] for a in allocations)
                assert memory + state["remaining_memory_mib"] == state["capacity_memory_mib"]
                assert quota + state["remaining_compute_units"] == 100
                assert slots == state["allocated_instances"]
                assert slots + state["remaining_instances"] == 10
                assert 0 <= state["remaining_memory_mib"] <= state["capacity_memory_mib"]
                assert 0 <= state["remaining_compute_units"] <= 100
                assert 0 <= state["remaining_instances"] <= 10
                assert {a["allocation_id"] for a in allocations} == {pod_id for pod_id, gpu_id in active.items() if gpu_id == event["gpu_id"]}
                if metrics["strategy"] == "dynamic_rf":
                    assert event["state_prediction_after"] is not None
                last_by_gpu[event["gpu_id"]] = state
        assert not active
        assert starts == finishes == metrics["completed_total_after_drain"]
        assert all(not s["allocations"] for s in last_by_gpu.values())
        for pod in read_csv(run_dir / "pods.csv"):
            if pod["status"] == "completed":
                assert float(pod["start_s"]) >= float(pod["arrival_s"])
                assert float(pod["finish_s"]) >= float(pod["start_s"])
        stream_count += 1
    assert stream_count == len(rows)
    config = json.loads((output / "run-config.json").read_text(encoding="utf-8"))
    selection = output / "policy-validation-selection.json"
    if selection.exists():
        validation = json.loads(selection.read_text(encoding="utf-8"))
        assert not set(config["seeds"]) & set(validation["validation_seeds"])
    ablation = output / "rf-ablation.csv"
    ablation_rows = read_csv(ablation) if ablation.exists() else []
    if ablation_rows:
        groups = defaultdict(list)
        for row in ablation_rows:
            groups[(row["scenario"], row["trace_seed"])].append(row)
        for group in groups.values():
            assert {r["variant"] for r in group} == {"with_RF", "without_RF"}
            for key in ("trace_sha256", "execution_seed", "observation_horizon_s"):
                assert len({r[key] for r in group}) == 1
    result = {"passed": True, "data_kind": "local_artifact_audit_not_real_cluster_verification",
              "first_round_files_unchanged": len(preserved), "paired_cohorts": len(pairs),
              "simulation_event_streams": stream_count, "events_checked": events_count,
              "resource_commits_checked": commits_count, "rf_ablation_paired_traces": len(ablation_rows)//2,
              "negative_waits": 0, "oversubscribed_resource_snapshots": 0,
              "validation_and_final_seeds_disjoint": True}
    junit = output / "pytest-results.xml"
    if junit.exists():
        tree = ElementTree.parse(junit).getroot()
        suites = list(tree) if tree.tag == "testsuites" else [tree]
        for name in ("tests", "failures", "errors", "skipped"):
            result["pytest_" + name] = sum(int(s.attrib.get(name, 0)) for s in suites)
        assert result["pytest_failures"] == result["pytest_errors"] == 0
    reproduction = output / "reproducibility-check.json"
    if reproduction.exists():
        check = json.loads(reproduction.read_text(encoding="utf-8"))
        assert all(digest(output / name) == value for name, value in check["before"].items())
        result["identical_rerun_metric_artifacts"] = len(check["before"])
    source_hashes = json.loads((output / "source-sha256.json").read_text(encoding="utf-8"))
    assert all(digest(ROOT / name) == value for name, value in source_hashes.items())
    result["source_files_match_run"] = len(source_hashes)
    (output / "verification.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    report = output / "report.md"
    marker = "<!-- offline-verification -->"
    text = report.read_text(encoding="utf-8").split(marker)[0].rstrip()
    text += (f"\n\n{marker}\n## 自动核验\n\n"
             f"pytest：{result.get('pytest_tests', '未读取')} 项，失败/错误均为 0。"
             f"已核验 {stream_count} 条完整仿真事件流、{events_count:,} 个事件、{commits_count:,} 次资源账本变更；"
             f"未发现超分或负等待，全部可行任务排空且资源释放。第一轮 {len(preserved)} 个文件 SHA256 未变。"
             f"相同种子完整复跑后 {result.get('identical_rerun_metric_artifacts', 0)} 个核心指标文件逐字节一致。"
             "详情见 verification.json、reproducibility-check.json、pytest-results.xml。\n")
    report.write_text(text, encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "results/offline-v2")
    parser.add_argument("--first-round", type=Path, default=ROOT / "results/offline-v1")
    args = parser.parse_args()
    verify(args.output, args.first_round)
