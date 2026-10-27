"""Two split protocols: episode-disjoint known models, and model-family-disjoint."""
from collections import Counter
import random

from ..resources import Allocation, GPUState, ResourceRequest
from .features import features, feature_hash, from_ledger
from .theory import assess

VALIDATION_MODELS = {"a30", "mi210", "h100"}
TEST_MODELS = {"a10", "mi300x", "mtt_s4000"}


def generate(devices, seed=20261016, episodes=40, steps=12):
    rng, seen, rows, duplicates = random.Random(seed), set(), [], 0
    for device in sorted(devices.values(), key=lambda d: d.device_id):
        if not device.eligible():
            continue
        for ep in range(episodes):
            episode_id = f"{seed}:{device.device_id}:episode-{ep:04d}"
            ledger = GPUState(episode_id, device)
            mode = rng.choice(("memory", "compute", "small", "mixed"))
            for step in range(steps + 1):
                if step:
                    if ledger.allocations and rng.random() < .22:
                        ledger.release(rng.choice(list(ledger.allocations)))
                    else:
                        if mode == "memory":
                            ratio, quota = rng.uniform(.1, .65), rng.randint(1, 12)
                        elif mode == "compute":
                            ratio, quota = rng.uniform(.005, .12), rng.randint(15, 60)
                        elif mode == "small":
                            ratio, quota = rng.uniform(.005, .08), rng.randint(1, 8)
                        else:
                            ratio, quota = rng.uniform(.02, .4), rng.randint(5, 35)
                        request = ResourceRequest(max(1, int(device.modeled_memory_mib * ratio)), quota)
                        if ledger.can_allocate(request):
                            ledger.allocate(Allocation(f"a{step}", rng.choice(("training", "inference")), request))
                state = from_ledger(ledger)
                x = features(device, state)
                fp = feature_hash(x)
                if fp in seen:
                    duplicates += 1
                    continue
                seen.add(fp)
                truth = assess(device, state)
                if truth["label_source"] != "synthetic":
                    raise ValueError("Never create supervised labels from an unknown device")
                rows.append({"sample_id": f"{episode_id}:s{step:03d}", "episode_id": episode_id,
                             "device_id": device.device_id, "model_group": device.model_group,
                             "features": x, "feature_sha256": fp, "dynamic_state": state,
                             "allocations": ledger.snapshot()["allocations"], "label_source": "synthetic",
                             "training_adapted": truth["training"], "inference_adapted": truth["inference"],
                             "label": truth["label"], "contract_id": "cross-gpu-fp32-synthetic-v3"})
    return rows, {"seed": seed, "episodes_per_device": episodes, "steps": steps,
                  "samples": len(rows), "duplicates_removed": duplicates,
                  "label_counts": dict(Counter(r["label"] for r in rows)),
                  "label_sources": {"measured": 0, "public_benchmark": 0, "synthetic": len(rows), "unknown": 0}}


def split_known(rows, seed=20261016):
    result = {n: [] for n in ("train", "validation", "test")}
    membership, rng = {}, random.Random(seed)
    for model in sorted({r["model_group"] for r in rows}):
        groups = sorted({r["episode_id"] for r in rows if r["model_group"] == model})
        rng.shuffle(groups)
        a, b = int(.6 * len(groups)), int(.8 * len(groups))
        for name, batch in zip(result, (groups[:a], groups[a:b], groups[b:])):
            membership.update({g: name for g in batch})
    for row in rows:
        result[membership[row["episode_id"]]].append(row)
    audit(result, model_disjoint=False)
    return result


def split_unseen(rows):
    result = {n: [] for n in ("train", "validation", "test")}
    for row in rows:
        name = "test" if row["model_group"] in TEST_MODELS else "validation" if row["model_group"] in VALIDATION_MODELS else "train"
        result[name].append(row)
    audit(result, model_disjoint=True)
    return result


def audit(splits, model_disjoint):
    checks, names = {}, list(splits)
    keys = ("sample_id", "episode_id", "feature_sha256") + (("model_group",) if model_disjoint else ())
    for name, rows in splits.items():
        if not rows:
            raise ValueError(f"Empty split: {name}")
        if any(r["label_source"] != "synthetic" for r in rows):
            raise ValueError("Unknown/self-predicted labels must not enter this synthetic experiment")
        if any(feature_hash(r["features"]) != r["feature_sha256"] for r in rows):
            raise ValueError("Feature checksum mismatch")
    for key in keys:
        for i, left in enumerate(names):
            for right in names[i+1:]:
                overlap = {r[key] for r in splits[left]} & {r[key] for r in splits[right]}
                checks[f"{left}:{right}:{key}"] = len(overlap)
                if overlap:
                    raise ValueError(f"Split leakage: {key}")
    return {"passed": True, "model_disjoint": model_disjoint, "overlaps": checks,
            "sizes": {n: len(r) for n, r in splits.items()},
            "model_groups": {n: sorted({r["model_group"] for r in batch}) for n, batch in splits.items()},
            "class_counts": {n: dict(Counter(r["label"] for r in batch)) for n, batch in splits.items()},
            "truth_kind": "synthetic", "no_real_performance_accuracy_claim": True}
