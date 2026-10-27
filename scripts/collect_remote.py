#!/usr/bin/env python3
"""Run read-only Python collectors over strict-host-key SSH; keep results locally."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime
import getpass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shlex
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent


def collect(job, environment, known_hosts, output):
    role, entry, collector = job
    target = entry["user"] + "@" + entry["host"]
    command = ["ssh", "-T", "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + str(known_hosts),
               "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2",
               "-o", "NumberOfPasswordPrompts=1", target, "python3 - " + shlex.join(entry.get("collector_arguments", []))]
    payload = (ROOT / "scripts" / collector).read_bytes().replace(b"\r\n", b"\n")
    stem = role + "-" + entry["host"]
    (output / (stem + ".collector.py")).write_bytes(payload)
    meta = {"role": role, "host": entry["host"], "user": entry["user"],
            "expected_gpu_from_user": entry.get("expected_gpu_from_user"),
            "command": command, "collector_sha256": hashlib.sha256(payload).hexdigest(),
            "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "strict_host_key_checking": True, "no_remote_files_created": True}
    try:
        result = subprocess.run(command, input=payload, capture_output=True, env=environment, timeout=420)
        (output / (stem + ".jsonl")).write_bytes(result.stdout)
        (output / (stem + ".stderr.txt")).write_bytes(result.stderr)
        records = []
        for line in result.stdout.decode("utf-8", errors="replace").splitlines():
            try: records.append(json.loads(line))
            except json.JSONDecodeError: pass
        meta.update(exit_code=result.returncode, records=len(records),
                    status="collected" if result.returncode == 0 and records else "failed",
                    stdout_file=stem + ".jsonl", stderr_file=stem + ".stderr.txt",
                    stdout_sha256=hashlib.sha256(result.stdout).hexdigest())
    except Exception as exc:
        meta.update(status="failed", exit_code=None, error=type(exc).__name__ + ": " + str(exc))
    meta["finished_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    (output / (stem + ".transport.json")).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hosts", default=str(ROOT / "scripts/hosts.json"))
    ap.add_argument("--known-hosts", default=str(ROOT.parent / "server_known_hosts"))
    ap.add_argument("--output")
    ap.add_argument("--password-env", help="Name of a process environment variable; the password is never saved")
    ap.add_argument("--management-kubeconfig", help="Use an existing kubeconfig on the management host, without changing it")
    ap.add_argument("--sudo-kubectl", action="store_true")
    ap.add_argument("--cluster-only", action="store_true")
    args = ap.parse_args()
    hosts = json.loads(Path(args.hosts).read_text(encoding="utf-8"))
    known_hosts = Path(args.known_hosts).resolve()
    if not known_hosts.is_file():
        raise SystemExit("Provide an existing trusted known_hosts file; host verification will not be disabled.")
    output = Path(args.output) if args.output else ROOT / "results" / ("phase1-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    output.mkdir(parents=True, exist_ok=False)
    password = os.environ.get(args.password_env) if args.password_env else getpass.getpass("SSH password (not saved): ")
    if not password:
        raise SystemExit("No password supplied; no connection attempted.")
    hosts["management"]["collector_arguments"] = []
    if args.management_kubeconfig:
        hosts["management"]["collector_arguments"] += ["--kubeconfig", args.management_kubeconfig]
    if args.sudo_kubectl:
        hosts["management"]["collector_arguments"] += ["--sudo-kubectl"]
    jobs = [] if args.cluster_only else [("node", h, "check_node.py") for h in hosts["nodes"]]
    jobs.append(("cluster", hosts["management"], "check_cluster.py"))
    with tempfile.TemporaryDirectory(prefix="gpu-lab-askpass-") as temporary:
        temporary = Path(temporary)
        helper = temporary / "answer.py"
        helper.write_text('import os,sys\nsys.stdout.write(os.environ.get("GPU_LAB_SSH_PASSWORD", ""))\n', encoding="utf-8")
        if os.name == "nt":
            executable = temporary / "answer.cmd"
            executable.write_text('@echo off\r\n"' + sys.executable + '" "' + str(helper) + '"\r\n', encoding="ascii")
        else:
            executable = temporary / "answer"
            executable.write_text('#!' + sys.executable + '\n' + helper.read_text(encoding="utf-8"), encoding="utf-8")
            executable.chmod(0o700)
        environment = os.environ.copy()
        environment.update(SSH_ASKPASS=str(executable), SSH_ASKPASS_REQUIRE="force", DISPLAY="gpu-lab",
                           GPU_LAB_SSH_PASSWORD=password)
        outcomes = []
        print(json.dumps({"output": str(output), "read_only": True}), flush=True)
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(collect, job, environment, known_hosts, output) for job in jobs]
            for future in as_completed(futures):
                value = future.result(); outcomes.append(value)
                print(json.dumps({k:value.get(k) for k in ["role", "host", "status", "exit_code", "records"]}, ensure_ascii=False), flush=True)
    summary = {"phase": 1, "read_only": True, "hosts_file": str(Path(args.hosts).resolve()),
               "known_hosts_file": str(known_hosts), "collections": outcomes, "output": str(output),
               "credentials_saved": False, "created_Pods": False, "modified_cluster_components": False}
    (output / "collection.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
