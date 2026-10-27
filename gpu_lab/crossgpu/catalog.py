from dataclasses import dataclass
import json
import math
from pathlib import Path

from ..profiles import ROOT

CATALOG_SOURCE = ROOT / "data/public/round3/source-records.json"
STATIC = ("memory_capacity_mib", "memory_bandwidth_gbps", "fp32_tflops", "fp16_dense_tflops",
          "bf16_dense_tflops", "int8_dense_tops", "max_board_power_w")
REQUIRED = STATIC[:3]
UNITS = dict(zip(STATIC, ("MiB", "GB/s (decimal bytes/s, NOT Gbit/s)", "TFLOPS", "TFLOPS", "TFLOPS", "TOPS", "W")))


@dataclass(frozen=True)
class Device:
    record: dict

    @property
    def device_id(self):
        return self.record["device_id"]

    @property
    def model_group(self):
        return self.record["model_group"]

    def values(self, comparable=False):
        return {k: (v["value"] if not comparable or v["model_comparable"] else None)
                for k, v in self.record["specifications"].items()}

    def eligible(self):
        values = self.values(comparable=True)
        return self.record["device_type"] == "GPU" and all(values[k] is not None for k in REQUIRED)

    @property
    def profile_id(self):
        return self.device_id

    @property
    def modeled_memory_mib(self):
        value = self.values()["memory_capacity_mib"]
        if value is None:
            raise ValueError("No verified memory capacity for a resource ledger")
        return int(value)


def compile_catalog(source=CATALOG_SOURCE):
    data = json.loads(Path(source).read_text(encoding="utf-8"))
    devices = {}
    for row in data["devices"]:
        specs = {}
        for i, name in enumerate(STATIC):
            original = row["values"][i]
            if original is not None and (not math.isfinite(original) or original <= 0):
                raise ValueError(f"Bad public value: {row['id']} {name}")
            source_id = row.get("field_sources", {}).get(name, row["source"])
            reference = data["sources"][source_id]
            if not reference["url"].startswith("https://"):
                raise ValueError("Expected HTTPS original source")
            value = math.floor(original * 1e9 / 2**20) if i == 0 and original is not None else original
            mixed = name in STATIC[3:6]
            compatible = (not mixed or name in row.get("comparable_mixed_features", []))
            if name == "fp32_tflops":
                compatible = row.get("fp32_comparable", row["device_type"] == "GPU")
            raw_unit = "GB nominal" if i == 0 else UNITS[name]
            display = row.get("raw_units", {}).get(name, f"{original} {raw_unit}" if original is not None else None)
            raw_value, conversion = original, None
            if display and original is not None:
                for original_unit in ("TB/s", "PFLOPS", "POPS"):
                    if original_unit in display:
                        raw_unit, raw_value = original_unit, original / 1000
                        conversion = f"{original_unit} * 1000 -> {UNITS[name]} (decimal)"
            if i == 0:
                conversion = "floor(nominal GB * 1e9 / 2**20); conservative OFFLINE assumption, not measured allocatable MiB"
            specs[name] = {
                "value": value, "unit": UNITS[name], "raw_value": raw_value, "raw_unit": raw_unit,
                "original_display": display,
                "data_kind": "public_spec" if original is not None else "unknown",
                "source_url": reference["url"], "source_id": source_id, "locator": reference["locator"],
                "scope": row["scope"], "sparsity": "dense" if original is not None and (i in (3,4,5) or i == 2 and compatible) else "not_applicable_or_unknown",
                "compute_basis": (("IEEE FP32 non-Tensor/vector peak; no TF32/FP32 matrix substitution" if compatible else "unverified accelerator FP32 engine/accumulator scope") if i == 2
                                  else row.get("mixed_precision_basis", "vendor dense figure; accumulator/engine not fully aligned") if mixed
                                  else "capacity/bandwidth/configured board power"),
                "model_comparable": compatible and original is not None,
                "model_mask_reason": ("missing" if original is None else "unaligned precision/engine/accumulator scope" if not compatible else None),
                "conversion": conversion, "derived_from_public_spec": conversion is not None,
            }
        record = {"device_id": row["id"], "model_group": row.get("model_group", row["id"]),
                  "vendor": row["vendor"], "model": row["model"], "architecture": row["architecture"],
                  "device_type": row["device_type"], "hardware_scope": row["scope"], "software_backend": row["backend"],
                  "native_quota_semantics": "unverified_backend_specific; NOT assumed equivalent across vendors",
                  "supported_precisions_evidence": [k for k in ("FP32", "FP16", "BF16", "INT8") if (
                      specs[{"FP32":"fp32_tflops","FP16":"fp16_dense_tflops","BF16":"bf16_dense_tflops","INT8":"int8_dense_tops"}[k]]["value"] is not None
                      or any(name.startswith(k.lower() + "_") for name in row.get("rejected_observations", {})))],
                  "capability_observations_not_comparable": row.get("rejected_observations", {}),
                  "specifications": specs, "note": row.get("note"), "performance_label_source": "unknown"}
        if row["id"] in devices:
            raise ValueError("Duplicate device")
        devices[row["id"]] = Device(record)
    return devices, data
