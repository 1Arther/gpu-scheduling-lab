#!/usr/bin/env python3
"""Locate existing kubeconfig paths without reading credential contents."""
import datetime
import json
import os
from pathlib import Path
import socket
import subprocess


def main():
    print(json.dumps({"type":"meta", "collector":"access-readonly-v1", "host":socket.gethostname(),
                      "utc":datetime.datetime.now(datetime.timezone.utc).isoformat()}), flush=True)
    paths = [str(Path.home()/".kube/config"), "/etc/kubernetes/admin.conf", "/root/.kube/config"]
    print(json.dumps({"type":"paths", "home":str(Path.home()), "KUBECONFIG_path_only":os.environ.get("KUBECONFIG"),
                      "candidates":paths}), flush=True)
    for filename in paths:
        command = ["sudo", "-n", "test", "-r", filename]
        try:
            result = subprocess.run(command, capture_output=True, timeout=8)
            print(json.dumps({"type":"command", "id":"readable_path", "path":filename,
                "command":command, "exit_code":result.returncode, "stderr":result.stderr.decode(errors="replace")[:1500]}), flush=True)
        except Exception as exc:
            print(json.dumps({"type":"command", "id":"readable_path", "path":filename, "command":command, "error":str(exc)}), flush=True)
    for command in [["systemctl","show","containerd","-p","ExecStart","-p","FragmentPath"],
                    ["sudo","-n","ctr","version"], ["sudo","-n","ctr","plugins","list"],
                    ["sudo","-n","ctr","-n","k8s.io","images","list"]]:
        try:
            result = subprocess.run(command, capture_output=True, timeout=12)
            stdout = result.stdout.decode(errors="replace")
            if "images" in command:
                stdout = "\n".join(line for line in stdout.splitlines() if any(s in line.lower() for s in ["cuda", "pytorch", "nvidia", "volcano", "vgpu", "hami"]))
            print(json.dumps({"type":"command", "id":"runtime_probe", "command":command, "exit_code":result.returncode,
                "stdout":stdout[:30000], "stderr":result.stderr.decode(errors="replace")[:2000]}), flush=True)
        except Exception as exc:
            print(json.dumps({"type":"command", "id":"runtime_probe", "command":command, "error":str(exc)}), flush=True)


if __name__ == "__main__":
    main()
