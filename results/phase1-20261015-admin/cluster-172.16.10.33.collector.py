#!/usr/bin/env python3
"""Read-only Kubernetes/Volcano inventory. Standard library; JSON Lines output."""
import datetime
import argparse
import hashlib
import json
import re
import socket
import subprocess
import time

KUBECTL_PREFIX = ["kubectl"]


def emit(item):
    print(json.dumps(item, ensure_ascii=False), flush=True)


def run(ident, argv, project=None):
    if argv[0] == "kubectl":
        argv = KUBECTL_PREFIX + argv[1:]
    started = time.monotonic()
    try:
        result = subprocess.run(argv, capture_output=True, timeout=18)
        text = result.stdout.decode("utf-8", errors="replace")
        item = {"type": "command", "id": ident, "command": argv, "exit_code": result.returncode,
                "seconds": round(time.monotonic()-started, 3), "stderr": result.stderr.decode(errors="replace")[:5000]}
        if project is not None and result.returncode == 0:
            item["data"] = project(json.loads(text))
            item["projection"] = "environment fields only; user workload environment and credentials omitted"
            item["raw_stdout_sha256"] = hashlib.sha256(result.stdout).hexdigest()
        else:
            item["stdout"] = text[:180000]
        emit(item)
        return item.get("data")
    except Exception as exc:
        emit({"type": "command", "id": ident, "command": argv, "exit_code": None,
              "error": type(exc).__name__ + ": " + str(exc)})
        return None


def metadata(obj):
    m = obj.get("metadata", {})
    return {"name": m.get("name"), "namespace": m.get("namespace"),
            "labels": {k:v for k,v in m.get("labels", {}).items() if any(t in k for t in ["app", "helm", "volcano", "nvidia", "hami"])},
            "annotations": {k:v for k,v in m.get("annotations", {}).items() if k.startswith(("meta.helm.sh/", "volcano.sh/", "nvidia.com/", "hami.io/"))},
            "owners": m.get("ownerReferences", [])}


def spec_summary(spec):
    result = {k:spec.get(k) for k in ["schedulerName", "nodeName", "runtimeClassName", "nodeSelector", "tolerations", "affinity"]}
    result.update({
        "containers": [{k:c.get(k) for k in ["name", "image", "resources"]} for c in spec.get("containers", [])],
        "initContainers": [{k:c.get(k) for k in ["name", "image", "resources", "restartPolicy"]} for c in spec.get("initContainers", [])],
        "configMaps": [v["configMap"]["name"] for v in spec.get("volumes", []) if v.get("configMap")],
        "overhead": spec.get("overhead", {})})
    return result


def component_spec(spec):
    result = spec_summary(spec)
    allowed = {"DEVICE_LIST_STRATEGY", "NVIDIA_DRIVER_ROOT", "PASS_DEVICE_SPECS", "MIG_STRATEGY", "FAIL_ON_INIT_ERROR",
               "NVIDIA_VISIBLE_DEVICES", "NVIDIA_DRIVER_CAPABILITIES", "CONFIG_FILE", "CUDA_VISIBLE_DEVICES"}
    for output, original in zip(result["containers"], spec.get("containers", [])):
        output["command"] = original.get("command")
        output["args"] = original.get("args")
        output["gpu_configuration_env"] = [{"name":e["name"], "value":e.get("value"), "valueFrom":e.get("valueFrom")}
                                            for e in original.get("env", []) if e["name"] in allowed]
    return result


def relevant(obj):
    spec = obj.get("spec", {})
    if "template" in spec: spec = spec["template"].get("spec", {})
    text = obj.get("metadata", {}).get("name", "") + " " + obj.get("metadata", {}).get("namespace", "") + " " + " ".join(c.get("image", "") for c in spec.get("containers", []))
    return bool(re.search(r"volcano|nvidia|vgpu|hami|gpu-operator", text, re.I))


def main():
    global KUBECTL_PREFIX
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig")
    parser.add_argument("--sudo-kubectl", action="store_true")
    args = parser.parse_args()
    if args.kubeconfig: KUBECTL_PREFIX += ["--kubeconfig=" + args.kubeconfig]
    if args.sudo_kubectl: KUBECTL_PREFIX = ["sudo", "-n"] + KUBECTL_PREFIX
    emit({"type": "meta", "collector": "cluster-readonly-v1", "host": socket.gethostname(),
          "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "read_only": True,
          "no_Pods_created": True})
    run("kubectl_context", ["kubectl", "config", "current-context"])
    run("kubernetes_version", ["kubectl", "version", "-o", "json", "--request-timeout=10s"], lambda d:d)
    run("nodes", ["kubectl", "get", "nodes", "-o", "json", "--request-timeout=10s"],
        lambda d:[{"metadata": metadata(n), "labels": n.get("metadata", {}).get("labels", {}),
                   "unschedulable": n.get("spec", {}).get("unschedulable", False), "taints": n.get("spec", {}).get("taints", []),
                   "status": {k:n.get("status", {}).get(k) for k in ["capacity", "allocatable", "conditions", "addresses", "nodeInfo"]}}
                  for n in d.get("items", [])])
    pods = run("pods", ["kubectl", "get", "pods", "-A", "-o", "json", "--request-timeout=10s"],
        lambda d:[{"metadata": metadata(p), "spec": spec_summary(p.get("spec", {})), "component_candidate": relevant(p),
                   "status": {k:p.get("status", {}).get(k) for k in ["phase", "conditions", "containerStatuses", "initContainerStatuses"]}}
                  for p in d.get("items", [])]) or []
    controllers = run("controllers", ["kubectl", "get", "deployments,daemonsets", "-A", "-o", "json", "--request-timeout=10s"],
        lambda d:[{"kind":p.get("kind"), "metadata":metadata(p), "spec": component_spec(p.get("spec", {}).get("template", {}).get("spec", {})),
                   "desired_replicas": p.get("spec", {}).get("replicas"), "status":p.get("status", {})}
                  for p in d.get("items", []) if relevant(p)]) or []
    for ident, resource, project in [
        ("runtimeclasses", "runtimeclasses.node.k8s.io", lambda d:d.get("items", [])),
        ("volcano_crds", "customresourcedefinitions.apiextensions.k8s.io", lambda d:[{"name":n["metadata"]["name"], "versions":n.get("spec", {}).get("versions"), "conditions":n.get("status", {}).get("conditions")} for n in d.get("items", []) if "volcano" in n["metadata"]["name"]]),
        ("queues", "queues.scheduling.volcano.sh", lambda d:[{"metadata":metadata(n), "spec":n.get("spec"), "status":n.get("status")} for n in d.get("items", [])]),
        ("podgroups", "podgroups.scheduling.volcano.sh", lambda d:[{"metadata":metadata(n), "spec":n.get("spec"), "status":n.get("status")} for n in d.get("items", [])]),
    ]:
        run(ident, ["kubectl", "get", resource, "-A", "-o", "json", "--request-timeout=10s"], project)
    run("helm_releases", ["helm", "list", "-A", "-o", "json"], lambda d:d)
    maps = set()
    for p in controllers:
        namespace = p["metadata"]["namespace"]
        for name in p["spec"]["configMaps"]: maps.add((namespace, name))
    for p in pods:
        if p["component_candidate"]:
            for name in p["spec"]["configMaps"]: maps.add((p["metadata"]["namespace"], name))
    # Default name is a fallback query, not an assumption that the map exists.
    maps.add(("volcano-system", "volcano-scheduler-configmap"))
    for namespace, name in sorted(maps)[:30]:
        run("configmap:"+namespace+"/"+name,
            ["kubectl", "get", "configmap", name, "-n", namespace, "-o", "json", "--request-timeout=10s"],
            lambda d:{"metadata":metadata(d), "data":d.get("data", {})})
    candidates = [p for p in pods if p["component_candidate"] and re.search(r"scheduler|controller|admission|device-plugin|gpu-operator|hami", p["metadata"]["name"], re.I)]
    for p in candidates[:12]:
        for container in p["spec"]["containers"][:1]:
            run("logs:"+p["metadata"]["namespace"]+"/"+p["metadata"]["name"]+"/"+container["name"],
                ["kubectl", "logs", p["metadata"]["name"], "-n", p["metadata"]["namespace"], "-c", container["name"], "--tail=60", "--timestamps", "--request-timeout=10s"])


if __name__ == "__main__":
    main()
