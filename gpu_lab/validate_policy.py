"""Select a fragmentation guard using only held-out scheduling validation traces.

Classification splits remain fixed. Validation trace seeds are distinct from
the final simulation seeds. Includes a model-free ablation for honest attribution.
"""
import argparse
import json
from pathlib import Path
import statistics
import sys

from .comparison import run_classification_comparison, write_csv
from .execution import ExecutionModel
from .model import SuitabilityModel
from .profiles import ROOT, load_profiles
from .run import save_json, sha256
from .simulation import simulate
from .strategies import Scheduler
from .workloads import generate_trace, SCENARIOS


def validate(output, seeds, count):
    output.mkdir(parents=True, exist_ok=True)
    profiles = load_profiles()
    static, dynamic, _, _ = run_classification_comparison(ROOT / "results/offline-v1", output, profiles)
    policies = {"original_v1": ROOT / "data/synthetic/scoring-policy.json",
                "compact_002": ROOT / "data/synthetic/scoring-policy-compact-002.json",
                "compact_006": ROOT / "data/synthetic/scoring-policy-compact-006.json",
                "memory_guard": ROOT / "data/synthetic/scoring-policy-memory-guard.json",
                "no_rf_ablation": ROOT / "data/synthetic/scoring-policy-no-rf.json"}
    rows = []
    for seed in seeds:
        for scenario in SCENARIOS:
            pods = generate_trace(scenario, seed, count)
            variants = {"binpack": None, **policies}
            for name, path in variants.items():
                scheduler = Scheduler("binpack" if path is None else "dynamic_rf", profiles, static, dynamic, scoring_path=path)
                result = simulate(pods, profiles, scheduler, ExecutionModel(seed=20261016+seed))
                row = {"scenario": scenario, "trace_seed": seed, "variant": name, **result["metrics"]}
                rows.append(row)
                print(f"Validation {scenario} seed={seed} {name}: wait={row['mean_wait_s']:.2f}, train_JCT={row['training_mean_jct_s']:.2f}, infer_P95={row['inference_pod_response_p95_s']:.2f}", flush=True)
    objective = []
    for variant in policies:
        ratios = []
        for row in (r for r in rows if r["variant"] == variant):
            base = next(r for r in rows if r["variant"] == "binpack" and r["scenario"] == row["scenario"] and r["trace_seed"] == row["trace_seed"])
            # Predeclared equal tradeoff across waiting, training response and inference response.
            value = (.30 * (row["mean_wait_s"]+1)/(base["mean_wait_s"]+1)
                     + .35 * row["training_mean_jct_s"]/base["training_mean_jct_s"]
                     + .35 * row["inference_pod_response_p95_s"]/base["inference_pod_response_p95_s"])
            ratios.append(value)
        objective.append({"variant": variant, "normalized_validation_cost": statistics.mean(ratios),
                          "eligible_for_main_ML_policy": variant != "no_rf_ablation"})
    chosen = min((r for r in objective if r["eligible_for_main_ML_policy"]), key=lambda r: r["normalized_validation_cost"])
    selected = policies[chosen["variant"]]
    # Match the ablation's packing guard to the selected policy for a fair comparison.
    ablation = json.loads(policies["no_rf_ablation"].read_text(encoding="utf-8"))
    chosen_config = json.loads(selected.read_text(encoding="utf-8"))
    ablation["packing_slack"] = chosen_config.get("packing_slack")
    for key in ("guard_task_types", "guard_memory_dominated_only"):
        if key in chosen_config:
            ablation[key] = chosen_config[key]
    save_json(output / "selected-policy.json", chosen_config)
    save_json(output / "selected-no-rf-ablation.json", ablation)
    write_csv(output / "validation-policy-metrics.csv", rows)
    write_csv(output / "validation-policy-objectives.csv", objective)
    save_json(output / "selection.json", {"validation_seeds": seeds, "final_seeds_excluded": [11, 29, 47],
        "pods_per_trace": count, "scenarios": list(SCENARIOS), "objective": "mean(.30*(wait+1)/(baseline_wait+1)+.35*train_JCT/baseline+.35*inference_P95/baseline)",
        "candidates": objective, "selected": chosen, "selected_source": str(selected), "source_sha256": sha256(selected),
        "limitations": "Synthetic validation on this fixed execution model, not real-world calibration; no-rf is an attribution ablation, not an eligible ML policy"})
    print(json.dumps({"selected": chosen, "output": str(output)}, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "results/offline-v2-policy-validation")
    parser.add_argument("--seeds", type=int, nargs="+", default=[20261017, 20261018])
    parser.add_argument("--pods", type=int, default=120)
    args = parser.parse_args()
    if set(args.seeds) & {11, 29, 47}:
        parser.error("Do not tune on the final evaluation seeds")
    validate(args.output, args.seeds, args.pods)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
