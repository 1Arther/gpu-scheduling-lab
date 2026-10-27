"""Create an offline 4+4+3 physical-GPU layout from user-specified counts."""
import json

from .profiles import ROOT
from .resources import GPUState, positive_integer

DEFAULT_LAYOUT = ROOT / "data/synthetic/cluster-layout.json"


def create_fleet(profiles, path=DEFAULT_LAYOUT):
    layout = json.loads(path.read_text(encoding="utf-8"))
    fleet = {}
    for node in layout["nodes"]:
        positive_integer(node["gpu_count"], "gpu_count")
        profile = profiles[node["profile_id"]]
        for i in range(node["gpu_count"]):
            gpu_id = f"{node['node_id']}/gpu-{i}"
            if gpu_id in fleet:
                raise ValueError("Duplicate offline GPU ID")
            fleet[gpu_id] = GPUState(gpu_id, profile)
    return fleet
