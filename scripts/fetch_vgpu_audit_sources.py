#!/usr/bin/env python3
"""Fetch pinned official source TEXT into local audit evidence; never execute it."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import hashlib
import json
from pathlib import Path
import urllib.request


def get(url):
    req = urllib.request.Request(url, headers={'User-Agent':'gpu-lab-readonly-source-audit'})
    return urllib.request.urlopen(req, timeout=35).read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', required=True)
    a = ap.parse_args()
    root = Path(a.output); root.mkdir(parents=True, exist_ok=True)
    repos = [('volcano-sh/volcano','v1.13.1','volcano'),
             ('Project-HAMi/volcano-vgpu-device-plugin','v1.11.0','device-plugin'),
             ('Project-HAMi/HAMi-core','3506e6222ca0f235cb745bc805832f422c484c17','hami-core')]
    manifest = []
    for repo, ref, alias in repos:
        url = 'https://api.github.com/repos/' + repo + '/git/trees/' + ref + '?recursive=1'
        raw = get(url); tree = json.loads(raw)
        (root / (alias + '-tree.json')).write_bytes(raw)
        manifest.append(dict(url=url, file=alias+'-tree.json', sha256=hashlib.sha256(raw).hexdigest(), git_tree=tree['sha']))
        paths = []
        for x in tree['tree']:
            p = x['path']
            if x['type'] != 'blob':
                continue
            include = False
            if alias == 'volcano':
                include = p.startswith(('pkg/scheduler/api/devices/nvidia/vgpu/', 'pkg/scheduler/api/devices/config/', 'pkg/scheduler/plugins/deviceshare/')) and not p.endswith('_test.go')
                include |= p in ('go.mod','pkg/scheduler/api/node_info.go','pkg/scheduler/cache/event_handlers.go','pkg/scheduler/cache/cache.go','pkg/scheduler/framework/statement.go')
            elif alias == 'device-plugin':
                include = p.endswith('.go') and p.startswith(('pkg/','cmd/')) and not p.endswith('_test.go')
                include |= p in ('.gitmodules','go.mod','Makefile','volcano-vgpu-device-plugin.yml') or p.startswith('docker/')
            else:
                include = p.startswith('src/') and p.endswith(('.c','.h')) or p in ('CMakeLists.txt','README_CN.md')
            if include:
                paths.append(p)
        def fetch(p):
            u = 'https://raw.githubusercontent.com/' + repo + '/' + ref + '/' + p
            f = root / alias / p; f.parent.mkdir(parents=True, exist_ok=True)
            try:
                b = get(u); f.write_bytes(b)
                return dict(url=u, file=str(f.relative_to(root)), sha256=hashlib.sha256(b).hexdigest(), bytes=len(b), repo=repo, ref=ref)
            except Exception as e:
                return dict(url=u, error=str(e), repo=repo, ref=ref)
        with ThreadPoolExecutor(max_workers=8) as pool:
            records = list(pool.map(fetch, paths)); manifest.extend(records)
        print(json.dumps(dict(repo=repo, ref=ref, fetched=sum('error' not in r for r in records), errors=[r for r in records if 'error' in r])), flush=True)
    # Only device-manager source, not the entire Kubernetes repository.
    for p in ('pkg/kubelet/cm/devicemanager/manager.go','pkg/kubelet/cm/devicemanager/pod_devices.go','pkg/kubelet/cm/devicemanager/endpoint.go'):
        u = 'https://raw.githubusercontent.com/kubernetes/kubernetes/v1.35.0/' + p
        f = root / 'kubernetes' / p; f.parent.mkdir(parents=True, exist_ok=True)
        try:
            b=get(u); f.write_bytes(b); manifest.append(dict(url=u,file=str(f.relative_to(root)),sha256=hashlib.sha256(b).hexdigest()))
        except Exception as e:
            manifest.append(dict(url=u,error=str(e)))
    (root / 'manifest.json').write_text(json.dumps(dict(fetched_utc=dt.datetime.now(dt.timezone.utc).isoformat(), sources=manifest), indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
