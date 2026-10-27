"""Reachable synthetic resource states, grouped by allocation/release episode."""
from collections import Counter
import hashlib
import json
import random

from .features import state_features
from .labeling import ReferenceOracle
from .resources import Allocation, GPUState, ResourceRequest


def fingerprint(features: dict) -> str:
    return hashlib.sha256(json.dumps(features, sort_keys=True, allow_nan=False).encode()).hexdigest()


def generate_dataset(profiles, oracle: ReferenceOracle, seed=20261016,
                     episodes_per_profile=300, steps=16) -> tuple[list[dict], dict]:
    if episodes_per_profile < 10 or steps < 2:
        raise ValueError("Need >=10 episodes per profile and >=2 steps")
    rng = random.Random(seed)
    rows, seen = [], set()
    duplicates = 0
    for profile_id, profile in sorted(profiles.items()):
        for episode in range(episodes_per_profile):
            episode_id = f"{seed}:{profile_id}:episode-{episode:05d}"
            state = GPUState(episode_id, profile, oracle.contract["assumptions"]["max_instances_per_gpu"])
            # Different synthetic packing regimes, not inferred production frequency.
            mode = rng.choice(("memory_heavy", "compute_heavy", "many_small", "mixed"))
            for step in range(steps + 1):
                if step:
                    if state.allocations and rng.random() < 0.22:
                        state.release(rng.choice(list(state.allocations)))
                    else:
                        task_type = rng.choice(("training", "inference"))
                        if mode == "memory_heavy":
                            memory, compute = rng.randint(2048, 14000), rng.randint(1, 12)
                        elif mode == "compute_heavy":
                            memory, compute = rng.randint(256, 4096), rng.randint(10, 55)
                        elif mode == "many_small":
                            memory, compute = rng.randint(128, 1536), rng.randint(1, 7)
                        else:
                            memory, compute = rng.randint(512, 10000), rng.randint(3, 40)
                        allocation = Allocation(f"a-{step}", task_type, ResourceRequest(memory, compute))
                        if state.can_allocate(allocation.request):
                            state.allocate(allocation)
                features = state_features(state)
                fp = fingerprint(features)
                # Remove exact feature duplicates globally BEFORE grouped splitting.
                # This also avoids idle/full snapshots appearing in different splits.
                if fp in seen:
                    duplicates += 1
                    continue
                seen.add(fp)
                rows.append({
                    "sample_id": f"{episode_id}:step-{step:03d}",
                    "episode_id": episode_id,
                    "profile_id": profile_id,
                    "packing_mode": mode,
                    "data_kind": "synthetic_resource_state_with_analytical_label",
                    "hardware_source_id": profile.source_id,
                    "contract_id": oracle.contract["contract_id"],
                    "features": features,
                    "feature_sha256": fp,
                    "state": state.snapshot(),
                    **oracle.evaluate(state),
                })
    return rows, {"seed": seed, "episodes_per_profile": episodes_per_profile, "steps": steps,
                  "duplicates_removed": duplicates, "samples": len(rows),
                  "sampling": "uniform synthetic packing regimes, not a production workload distribution",
                  "label_counts": dict(Counter(r["label"] for r in rows))}


def split_dataset(rows, seed=20261016):
    """60/20/20 episode groups; no feature, scenario, or sample overlap."""
    groups = sorted({r["episode_id"] for r in rows})
    rng = random.Random(seed)
    rng.shuffle(groups)
    n = len(groups)
    if n < 10:
        raise ValueError("Too few independent episodes")
    a, b = int(n * 0.6), int(n * 0.8)
    membership = {g: split for split, batch in zip(
        ("train", "validation", "test"), (groups[:a], groups[a:b], groups[b:])) for g in batch}
    splits = {name: [] for name in ("train", "validation", "test")}
    for row in rows:
        splits[membership[row["episode_id"]]].append(row)
    audit_splits(splits)  # Fail, never silently accept leakage.
    return splits


def audit_splits(splits):
    names = list(splits)
    audits = {}
    for name, batch in splits.items():
        for row in batch:
            if row["feature_sha256"] != fingerprint(row["features"]):
                raise ValueError(f"Feature fingerprint mismatch in {name}")
        if len({row["sample_id"] for row in batch}) != len(batch):
            raise ValueError(f"Duplicate sample ID in {name}")
    for key in ("sample_id", "episode_id", "feature_sha256"):
        sets = {name: {r[key] for r in splits[name]} for name in names}
        for i, left in enumerate(names):
            for right in names[i + 1:]:
                count = len(sets[left] & sets[right])
                audits[f"{left}__{right}__{key}_overlap"] = count
                if count:
                    raise ValueError(f"Data leakage: {key}, {left}, {right}, {count}")
    return {"passed": True, "overlaps": audits,
            "sizes": {n: len(rows) for n, rows in splits.items()},
            "episodes": {n: len({r['episode_id'] for r in rows}) for n, rows in splits.items()},
            "class_counts": {n: dict(Counter(r['label'] for r in rows)) for n, rows in splits.items()},
            "scope": "unseen synthetic states of the same three hardware profiles; not unseen-chip generalization"}


def write_jsonl(path, rows):
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
