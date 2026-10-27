from copy import deepcopy
import json
from pathlib import Path

from ..profiles import ROOT
from ..crossgpu.catalog import CATALOG_SOURCE, STATIC, compile_catalog
from ..crossgpu.data import audit, split_known

PLAN_PATH = ROOT / "data/round4-plan.json"
ADDITIONS = ROOT / "data/public/round4/additions.json"


def load_plan():
    return json.loads(PLAN_PATH.read_text(encoding="utf-8"))


def assemble_catalog(destination):
    source = json.loads(CATALOG_SOURCE.read_text(encoding="utf-8"))
    new = json.loads(ADDITIONS.read_text(encoding="utf-8"))
    source["schema_version"], source["verified_on"] = 4, new["verified_on"]
    source["sources"].update(new["sources"])
    source["devices"] = [d for d in source["devices"] if d["vendor"] == "NVIDIA"]
    for update in new["updates"]:
        old = next(d for d in source["devices"] if d["id"] == update["id"])
        old.update(update)
    source["devices"].extend(new["devices"])
    # The common supervisor is FP32. Mixed Tensor/accumulator scopes stay in the
    # public database but cannot serve as silent architecture-identifying inputs.
    for row in source["devices"]:
        row["comparable_mixed_features"] = []
    used = {d["source"] for d in source["devices"]}
    used.update(s for d in source["devices"] for s in d.get("field_sources", {}).values())
    source["sources"] = {k:v for k,v in source["sources"].items() if k in used}
    Path(destination).write_text(json.dumps(source, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    return compile_catalog(destination)


def split_protocol(rows, config, seed):
    if config["kind"] == "known_model_unseen_episode":
        splits = split_known([r for r in rows if r["device_id"] not in config["exclude"]], seed=seed)
    else:
        splits = {name: [r for r in rows if r["device_id"] in config[name]] for name in ("train","validation","test")}
    audit(splits, model_disjoint=config["kind"] != "known_model_unseen_episode")
    return splits
