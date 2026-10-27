from dataclasses import dataclass
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILES = ROOT / "data/public/gpus.json"


@dataclass(frozen=True)
class GPUProfile:
    profile_id: str
    name: str
    vendor: str
    architecture: str
    memory_gb: float
    bandwidth_gb_s: float
    fp32_tflops: float
    cuda_cores: int | None
    tdp_w: float | None
    source_id: str

    def __post_init__(self):
        for name in ("memory_gb", "bandwidth_gb_s", "fp32_tflops"):
            value = getattr(self, name)
            if value is None or not math.isfinite(value) or value <= 0:
                raise ValueError(f"Required public specification missing/invalid: {name}")
        for name in ("cuda_cores", "tdp_w"):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError(f"Invalid optional specification: {name}")

    @property
    def modeled_memory_mib(self) -> int:
        """Conservative synthetic capacity, not a query of live allocatable memory."""
        return math.floor(self.memory_gb * 1_000_000_000 / 1_048_576)


def load_profiles(path: Path = DEFAULT_PROFILES) -> dict[str, GPUProfile]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    profiles = {}
    for row in data["gpus"]:
        profile = GPUProfile(**row)
        if profile.source_id not in data["sources"]:
            raise ValueError(f"Missing source for {profile.profile_id}")
        if profile.profile_id in profiles:
            raise ValueError("Duplicate profile ID")
        profiles[profile.profile_id] = profile
    return profiles
