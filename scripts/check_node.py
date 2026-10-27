#!/usr/bin/env python3
"""Read-only GPU/runtime inventory. Prints JSON Lines; never changes node state."""
import datetime
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def run(ident, argv, project=None, privileged_fallback=False, timeout=12):
    started = time.monotonic()
    try:
        result = subprocess.run(argv, capture_output=True, timeout=timeout)
        if result.returncode and privileged_fallback and shutil.which("sudo"):
            emit({"type": "command", "id": ident + "_unprivileged", "command": argv,
                  "exit_code": result.returncode, "stderr": result.stderr.decode(errors="replace")[:4000],
                  "stdout": result.stdout.decode(errors="replace")[:3000]})
            argv = ["sudo", "-n"] + argv
            result = subprocess.run(argv, capture_output=True, timeout=timeout)
        stdout = result.stdout.decode("utf-8", errors="replace")
        item = {"type": "command", "id": ident, "command": argv, "exit_code": result.returncode,
                "seconds": round(time.monotonic()-started, 3), "stderr": result.stderr.decode(errors="replace")[:6000]}
        if project and result.returncode == 0:
            item["data"] = project(json.loads(stdout))
            item["projection"] = "allowlisted runtime/image fields only; credentials omitted"
            item["raw_stdout_sha256"] = hashlib.sha256(result.stdout).hexdigest()
        else:
            item["stdout"] = stdout[:2000000]
            item["stdout_truncated"] = len(stdout) > 2000000
        emit(item)
        return item
    except Exception as exc:
        item = {"type": "command", "id": ident, "command": argv, "exit_code": None,
                "error": type(exc).__name__ + ": " + str(exc)}
        emit(item)
        return item


def runtime_info(data):
    containerd = data.get("config", {}).get("containerd", {})
    return {"status": data.get("status"), "runtimeType": data.get("runtimeType"),
            "defaultRuntimeName": containerd.get("defaultRuntimeName"),
            "runtimes": {name: {k: value.get(k) for k in ["runtimeType", "runtimePath", "options"]}
                         for name, value in containerd.get("runtimes", {}).items()}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gpu-only", action="store_true")
    args = parser.parse_args()
    emit({"type": "meta", "collector": "gpu-node-readonly-v1", "host": socket.gethostname(),
          "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "effective_uid": os.geteuid(),
          "read_only": True, "no_GPU_workloads_started": True})
    run("nvidia_smi_brief", ["nvidia-smi"])
    run("gpu_activity", ["nvidia-smi", "--query-gpu=index,uuid,memory.total,memory.used,memory.free,utilization.gpu", "--format=csv,noheader,nounits"])
    if args.gpu_only:
        return
    for ident, argv in [
        ("hostname", ["hostname", "-f"]), ("os", ["cat", "/etc/os-release"]),
        ("kernel", ["uname", "-srmo"]), ("gpu_list", ["nvidia-smi", "-L"]),
        ("gpu_query", ["nvidia-smi", "--query-gpu=index,name,uuid,memory.total,driver_version,pci.bus_id", "--format=csv,noheader,nounits"]),
        ("gpu_xml", ["nvidia-smi", "-q", "-x"]),
        ("gpu_compute_capability", ["nvidia-smi", "--query-gpu=uuid,compute_cap", "--format=csv,noheader,nounits"]),
        ("cuda_compiler", ["nvcc", "--version"]),
        ("nvidia_ctk", ["nvidia-ctk", "--version"]),
        ("nvidia_container_cli", ["nvidia-container-cli", "--version"]),
        ("nvidia_runtime", ["nvidia-container-runtime", "--version"]),
        ("containerd_version", ["containerd", "--version"]), ("crio_version", ["crio", "--version"]),
        ("docker_version", ["docker", "--version"]), ("runc_version", ["runc", "--version"]),
        ("crictl_version", ["crictl", "--version"]),
        ("services", ["systemctl", "is-active", "containerd", "docker", "crio", "kubelet"]),
    ]:
        run(ident, argv)
    if shutil.which("dpkg-query"):
        run("packages", ["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\n", "nvidia-container-toolkit*", "libnvidia-container*", "containerd*", "nvidia-driver*"])
    elif shutil.which("rpm"):
        run("packages", ["rpm", "-q", "nvidia-container-toolkit", "libnvidia-container1", "containerd", "nvidia-driver"])
    for ident, filename in [("containerd_config", "/etc/containerd/config.toml"), ("docker_config", "/etc/docker/daemon.json")]:
        def project_file(value, config_type=ident):
            if config_type == "docker_config":
                obj = json.loads(value)
                return {k: obj[k] for k in ["default-runtime", "runtimes", "features", "cdi-spec-dirs"] if k in obj}
            lines = []; section = ""
            for line in value.splitlines():
                if line.strip().startswith("["):
                    section = line.strip()
                if any(token in line for token in ["default_runtime_name", "runtime_type", "BinaryName", "enable_cdi", "cdi_spec_dirs", "SystemdCgroup"]):
                    lines.append({"section": section, "line": line.strip()})
            return lines
        # Configuration files can contain registry credentials: never emit full text.
        for command in [["cat", filename], ["sudo", "-n", "cat", filename]]:
            if command[0] == "sudo" and not shutil.which("sudo"): continue
            try:
                proc = subprocess.run(command, capture_output=True, timeout=8)
                if proc.returncode == 0:
                    value = proc.stdout.decode(errors="replace")
                    emit({"type": "command", "id": ident, "command": command, "exit_code": 0,
                          "data": project_file(value), "projection": "runtime fields only; registry credentials omitted",
                          "raw_stdout_sha256": hashlib.sha256(proc.stdout).hexdigest()})
                    break
                emit({"type": "command", "id": ident, "command": command, "exit_code": proc.returncode,
                      "stderr": proc.stderr.decode(errors="replace")[:2000]})
            except Exception as exc:
                emit({"type": "command", "id": ident, "command": command, "exit_code": None, "error": str(exc)})
    for filename in ["/var/lib/kubelet/device-plugins", "/etc/cdi", "/var/run/cdi"]:
        run("path_" + Path(filename).name, ["ls", "-la", filename], privileged_fallback=True)
    run("cri_info", ["crictl", "info", "-o", "json"], runtime_info, privileged_fallback=True)
    run("gpu_cached_images", ["crictl", "images", "-o", "json"],
        lambda data: [{"id": row.get("id"), "repoTags": row.get("repoTags"), "repoDigests": row.get("repoDigests")}
                      for row in data.get("images", []) if any(token in " ".join(row.get("repoTags", [])).lower()
                      for token in ["cuda", "pytorch", "nvidia", "volcano", "vgpu", "hami"])], privileged_fallback=True)


if __name__ == "__main__":
    main()
