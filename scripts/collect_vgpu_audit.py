#!/usr/bin/env python3
"""Reuse strict-host-key SSH transport; only audit_vgpu_readonly.py executes remotely."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import sys
import tempfile
from collect_remote import collect, ROOT


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', required=True)
    ap.add_argument('--password-env', default='GPU_LAB_SSH_PASSWORD')
    ap.add_argument('--supplement', action='store_true')
    ap.add_argument('--final-evidence', action='store_true')
    ap.add_argument('--linkage', action='store_true')
    ap.add_argument('--management-only', action='store_true')
    a = ap.parse_args()
    output = Path(a.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    password = os.environ.get(a.password_env)
    if not password:
        raise SystemExit('Supply an SSH password using the named environment variable; not saved.')
    known = ROOT.parent / 'server_known_hosts'
    hosts = json.loads((ROOT / 'scripts/hosts.json').read_text(encoding='utf-8'))
    jobs = []
    for h in hosts['nodes']:
        if a.management_only and h['host'] != hosts['management']['host']:
            continue
        h['collector_arguments'] = ['--cluster','--kubeconfig','/etc/kubernetes/admin.conf','--sudo-kubectl'] if h['host'] == hosts['management']['host'] else []
        if a.supplement:
            h['collector_arguments'].append('--supplement')
        if a.final_evidence:
            h['collector_arguments'].append('--final-evidence')
        if a.linkage:
            h['collector_arguments'].append('--linkage')
        jobs.append(('audit', h, 'audit_vgpu_readonly.py'))
    # Native ssh launches a .cmd askpass helper. Use an ASCII temporary path and
    # base Python executable so Windows cmd does not misdecode a Unicode venv path.
    helper_root = Path(os.environ.get('LOCALAPPDATA', tempfile.gettempdir())) / 'Temp'
    with tempfile.TemporaryDirectory(prefix='vgpu-audit-askpass-', dir=helper_root) as temp:
        p = Path(temp)
        helper = p / 'answer.py'
        helper.write_text('import os,sys\nsys.stdout.write(os.environ.get("GPU_LAB_SSH_PASSWORD", ""))\n', encoding='utf-8')
        executable = p / 'answer.cmd'
        executable.write_text('@echo off\r\n"' + getattr(sys, '_base_executable', sys.executable) + '" "' + str(helper) + '"\r\n', encoding='ascii')
        env = os.environ.copy()
        env.update(SSH_ASKPASS=str(executable), SSH_ASKPASS_REQUIRE='force', DISPLAY='gpu-lab', GPU_LAB_SSH_PASSWORD=password)
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(collect, job, env, known, output) for job in jobs]
            results = []
            for f in as_completed(futures):
                x = f.result(); results.append(x)
                print(json.dumps({k:x.get(k) for k in ('host','status','exit_code','records','error')}, ensure_ascii=False), flush=True)
    (output / 'collection.json').write_text(json.dumps(dict(read_only=True, created_Pods=False, collections=results), ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
