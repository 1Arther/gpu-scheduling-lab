#!/usr/bin/env python3
"""Read-only targeted follow-up for mounted scheduler binary and runtime setup."""
import datetime
import argparse
import hashlib
import json
import socket
import subprocess


def emit(item):
    print(json.dumps(item, ensure_ascii=False), flush=True)


def run(ident, argv, project=None):
    try:
        result = subprocess.run(argv, capture_output=True, timeout=16)
        stdout = result.stdout.decode(errors="replace")
        item = {"type":"command", "id":ident, "command":argv, "exit_code":result.returncode,
                "stderr":result.stderr.decode(errors="replace")[:3000]}
        if result.returncode == 0 and project:
            item["data"] = project(stdout)
            item["raw_stdout_sha256"] = hashlib.sha256(result.stdout).hexdigest()
        else:
            item["stdout"] = stdout[:20000]
        emit(item)
        return item.get("data")
    except Exception as exc:
        emit({"type":"command", "id":ident, "command":argv, "exit_code":None, "error":str(exc)})


def runtime_lines(text):
    section = ""; result = []
    for line in text.splitlines():
        if line.strip().startswith("["):section=line.strip()
        if any(k in line for k in ["imports =", "default_runtime_name", "runtime_type", "BinaryName", "enable_cdi", "cdi_spec_dirs"]):
            result.append({"section":section, "line":line.strip()})
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--provenance-only", action="store_true")
    parser.add_argument("--binary-version-only", action="store_true")
    args = parser.parse_args()
    emit({"type":"meta", "collector":"targeted-readonly-v1", "host":socket.gethostname(),
          "utc":datetime.datetime.now(datetime.timezone.utc).isoformat()})
    if args.binary_version_only:
        # Path was obtained from the existing Deployment's read-only hostPath mount.
        if socket.gethostname().lower() == "t3dgq":
            run("scheduler_binary_version", ["/home/zbs/volcano-v1.13.1-dqn/_output/bin/vc-scheduler", "--version"])
        return
    if not args.provenance_only:
        run("effective_containerd_config", ["sudo","-n","containerd","config","dump"], runtime_lines)
        run("cdi_devices", ["nvidia-ctk","cdi","list"])
        run("docker_gpu_images", ["sudo","-n","docker","image","ls","--format","{{.Repository}}:{{.Tag}} {{.ID}}"],
            lambda text:[line for line in text.splitlines() if any(t in line.lower() for t in ["cuda","pytorch","nvidia","volcano","hami"])])
    if socket.gethostname().lower() != "t3dgq":return
    base=["sudo","-n","kubectl","--kubeconfig=/etc/kubernetes/admin.conf"]
    def controllers(text):
        result=[]
        for d in json.loads(text).get("items",[]):
            meta=d["metadata"];spec=d.get("spec",{}).get("template",{}).get("spec",{})
            result.append({"name":meta["name"], "labels":meta.get("labels",{}),
                "helm_annotations":{k:v for k,v in meta.get("annotations",{}).items() if k.startswith("meta.helm.sh/")},
                "managed_by_commands":sorted({x.get("manager","") for x in meta.get("managedFields",[])}),
                "has_last_applied_annotation":"kubectl.kubernetes.io/last-applied-configuration" in meta.get("annotations",{}),
                "hostPathVolumes":[{"name":v["name"],"hostPath":v["hostPath"]} for v in spec.get("volumes",[]) if v.get("hostPath")],
                "container_mounts":[{"name":c["name"],"mounts":c.get("volumeMounts",[])} for c in spec.get("containers",[])]})
        return result
    info=run("volcano_deployment_provenance",base+["get","deployments","-n","volcano-system","-o","json","--show-managed-fields=true","--request-timeout=10s"],controllers) or []
    if args.provenance_only:return
    for item in info:
        if item["name"]=="volcano-scheduler":
            for volume in item["hostPathVolumes"]:
                if volume["name"]=="local-vc-scheduler":
                    filename=volume["hostPath"]["path"]
                    run("scheduler_binary_hash",["sudo","-n","sha256sum",filename])
    run("dqn_service",base+["get","service","dqn-scheduler-grpc","-n","volcano-system","-o","json","--request-timeout=10s"],
        lambda text:{k:json.loads(text).get(k) for k in ["metadata","spec"]})
    run("dqn_endpoints",base+["get","endpointslices.discovery.k8s.io","-n","volcano-system","-l","kubernetes.io/service-name=dqn-scheduler-grpc","-o","json","--request-timeout=10s"],
        lambda text:[{"name":d["metadata"]["name"],"endpoints":d.get("endpoints"),"ports":d.get("ports")} for d in json.loads(text).get("items",[])])
    run("recent_gpu_warnings",base+["get","events","-A","--field-selector=type=Warning","-o","json","--request-timeout=10s"],
        lambda text:[{"namespace":e["metadata"].get("namespace"),"reason":e.get("reason"),"message":e.get("message"),
                      "lastTimestamp":e.get("lastTimestamp"),"object":e.get("involvedObject")} for e in json.loads(text).get("items",[])
                     if any(t in (e.get("message","")+e.get("involvedObject",{}).get("name","")).lower() for t in ["gpu","nvidia","volcano","containerd"])][-40:])


if __name__ == "__main__":
    main()
