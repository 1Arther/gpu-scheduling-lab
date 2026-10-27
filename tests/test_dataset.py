from dataclasses import replace
import json
from urllib.parse import urlparse
import pytest

from gpu_lab.dataset import audit_splits, fingerprint, generate_dataset, split_dataset
from gpu_lab.features import state_features
from gpu_lab.labeling import LABELS
from gpu_lab.profiles import DEFAULT_PROFILES
from gpu_lab.resources import Allocation, GPUState, ResourceRequest


def test_public_source_traceability(profiles):
    catalog = json.loads(DEFAULT_PROFILES.read_text(encoding="utf-8"))
    assert catalog["data_kind"] == "public_hardware_specifications"
    assert len(profiles) == 3
    for profile in profiles.values():
        source = catalog["sources"][profile.source_id]
        assert urlparse(source["url"]).hostname == "images.nvidia.com"
        assert {"memory_gb", "bandwidth_gb_s", "fp32_tflops"} <= set(source["fields"])
        assert profile.modeled_memory_mib * 1048576 <= profile.memory_gb * 1e9


def test_required_features_not_silently_imputed(profiles):
    with pytest.raises(ValueError, match="Required"):
        replace(profiles["l40"], fp32_tflops=None)


def test_dataset_reproducible_and_seed_sensitive(profiles, oracle):
    first, meta = generate_dataset(profiles, oracle, seed=19, episodes_per_profile=10, steps=6)
    second, meta2 = generate_dataset(profiles, oracle, seed=19, episodes_per_profile=10, steps=6)
    other, _ = generate_dataset(profiles, oracle, seed=20, episodes_per_profile=10, steps=6)
    assert first == second and meta == meta2
    assert [r["feature_sha256"] for r in first] != [r["feature_sha256"] for r in other]


def test_split_no_episode_sample_or_feature_leakage(rows, splits):
    audit = audit_splits(splits)
    assert audit["passed"]
    assert all(v == 0 for v in audit["overlaps"].values())
    assert sum(audit["sizes"].values()) == len(rows)
    assert splits == split_dataset(rows, seed=41)
    assert splits != split_dataset(rows, seed=42)
    assert len({fingerprint(r["features"]) for r in rows}) == len(rows)
    for batch in splits.values():
        assert {r["label"] for r in batch} == set(LABELS)


def test_leakage_audit_rejects_shared_experiment(splits):
    bad = {k: list(v) for k, v in splits.items()}
    bad["test"].append(splits["train"][0])
    with pytest.raises(ValueError, match="leakage"):
        audit_splits(bad)


def test_fingerprint_cannot_hide_changed_features(splits):
    bad = {k: list(v) for k, v in splits.items()}
    row = {**bad["test"][0], "features": {**bad["test"][0]["features"], "remaining_memory_mib": -1}}
    bad["test"][0] = row
    with pytest.raises(ValueError, match="fingerprint"):
        audit_splits(bad)


def test_synthetic_states_replay_and_label_consistency(rows, profiles, oracle):
    for row in rows:
        state = GPUState(row["state"]["gpu_id"], profiles[row["profile_id"]])
        for a in row["state"]["allocations"]:
            state.allocate(Allocation(a["allocation_id"], a["task_type"], ResourceRequest(**a["request"])))
        assert state_features(state) == row["features"]
        assert oracle.evaluate(state)["label"] == row["label"]
        assert row["data_kind"].startswith("synthetic")
