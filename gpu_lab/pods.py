"""Offline Pod-label reader; never contacts the Kubernetes API."""
from pathlib import Path
import yaml

from .resources import TASK_TYPES

TASK_LABEL = "gpu-lab/workload-type"


def task_type_from_pod(pod: dict) -> str:
    if not isinstance(pod, dict) or pod.get("kind") != "Pod":
        raise ValueError("Expected a Pod mapping")
    value = pod.get("metadata", {}).get("labels", {}).get(TASK_LABEL)
    if value not in TASK_TYPES:
        raise ValueError(f"Pod label {TASK_LABEL} must explicitly be training or inference")
    return value


def load_pod_type(path):
    return task_type_from_pod(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
