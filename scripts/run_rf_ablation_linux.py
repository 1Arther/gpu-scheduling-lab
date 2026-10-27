"""Run frozen-model/native Volcano verification in an isolated Linux source copy.

No Kubernetes client configuration, GPU API, pip install, or model fitting is used.
Dependencies must already be available in this directory's independent environment.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import threading
import time


ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--go', default=shutil.which('go'))
    ap.add_argument('--jobs', type=int, default=2)
    ap.add_argument('--output', type=Path, default=ROOT/'results/linux-verification')
    a = ap.parse_args()
    if sys.platform != 'linux':
        raise SystemExit('Requires actual Linux execution; cross-compilation is insufficient.')
    if ROOT == Path('/home/zbs/volcano-v1.13.1-dqn') or not a.go:
        raise SystemExit('Use a separate source copy and a Go >=1.24 executable.')
    out = a.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    source = ROOT/'scheduler/volcano-rf'
    bundle = ROOT/'models/pod-gpu-rf-v1'
    plan = ROOT/'results/rf-ablation-20261019/plan.json'
    expected_plan = (plan.parent/'plan.sha256').read_text().strip()
    if sha(plan) != expected_plan:
        raise SystemExit('Frozen plan checksum mismatch')
    manifest = json.loads((bundle/'manifest.json').read_text())
    before = {p.name: sha(p) for p in bundle.iterdir() if p.is_file()}
    for name, expected in manifest['artifact_sha256'].items():
        if before[name] != expected:
            raise SystemExit('Frozen artifact changed: '+name)
    packages = ['numpy','scipy','sklearn','joblib','yaml','pytest']
    versions = {}
    import importlib
    for name in packages:
        versions[name] = getattr(importlib.import_module(name), '__version__', 'unknown')
    sys.path.insert(0, str(ROOT))
    from gpu_lab.runtime.service import SuitabilityService
    from gpu_lab.runtime.http_server import make_server
    service = SuitabilityService()
    if service.model is None or service.model_error:
        raise SystemExit('Frozen model failed to load: '+str(service.model_error))
    server = make_server(service, port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    endpoint = f'http://127.0.0.1:{server.server_address[1]}/v1/rank'
    env = os.environ.copy()
    env.update(GOOS='linux', GOARCH=platform.machine().replace('x86_64','amd64'),
               CGO_ENABLED='0', GOMAXPROCS=str(a.jobs),
               GOCACHE=str(ROOT/'.cache/go-build'), GOMODCACHE=str(ROOT/'.cache/go-mod'),
               GOPROXY=env.get('GOPROXY','https://goproxy.cn,direct'),
               RF_REQUIRE_LINUX='1', RF_LIVE_ENDPOINT=endpoint,
               RF_ABLATION_PLAN=str(plan), RF_ABLATION_OUTPUT=str(out),
               RF_DEMO_OUTPUT=str(out/'round6-regression'))
    # Never inherit a user's live kubeconfig into subprocess tests.
    env['KUBECONFIG'] = str(ROOT/'nonexistent-offline-kubeconfig')
    metadata = {'started_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
                'hostname':platform.node(), 'uname':list(platform.uname()),
                'python':sys.version, 'python_executable':sys.executable, 'packages':versions,
                'root':str(ROOT), 'model_version':service.version, 'plan_sha256':sha(plan),
                'bundle_sha256_before':before, 'endpoint':endpoint,
                'go_version':subprocess.check_output([a.go,'version'],text=True).strip(),
                'real_kubernetes_client':False, 'gpu_workloads':False, 'model_training':False,
                'commands':[]}
    def save(stage):
        metadata['stage'] = stage
        (out/'execution.json').write_text(json.dumps(metadata,indent=2),encoding='utf-8')
    def run(name, command, cwd):
        save(name)
        started = time.time()
        with (out/(name+'.log')).open('wb') as log:
            process = subprocess.Popen(command,cwd=cwd,env=env,stdout=log,stderr=subprocess.STDOUT)
            metadata['child_pid'] = process.pid
            save(name)
            code = process.wait()
        metadata.pop('child_pid',None)
        metadata['commands'].append({'name':name,'command':command,'cwd':str(cwd),
                                     'exit_code':code,'elapsed_s':round(time.time()-started,3)})
        save(name+'-finished')
        return code
    try:
        code = run('pytest',[sys.executable,'-m','pytest','tests/test_rf_ablation.py',
                   'tests/test_round6.py','tests/test_round5.py','-q',
                   '--junitxml='+str(out/'pytest.xml')],ROOT)
        if code:raise RuntimeError('Python tests failed')
        code = run('go-tests',[a.go,'test','-p',str(a.jobs),'-count=1','-json',
                   './pkg/scheduler/api/devices/nvidia/vgpu',
                   './pkg/scheduler/plugins/deviceshare','./pkg/scheduler/plugins/predicates'],source)
        if code:raise RuntimeError('Go tests failed; inspect go-tests.log')
        code = run('scheduler-build',[a.go,'build','-p',str(a.jobs),'-o',str(out/'vc-scheduler'),
                   './cmd/scheduler'],source)
        if code:raise RuntimeError('Scheduler build failed')
        metadata['success'] = True
    except Exception as exc:
        metadata['success'] = False
        metadata['error'] = type(exc).__name__+': '+str(exc)
    finally:
        server.shutdown();server.server_close();worker.join(timeout=5)
        metadata['bundle_sha256_after'] = {p.name:sha(p) for p in bundle.iterdir() if p.is_file()}
        metadata['frozen_artifacts_unchanged'] = before == metadata['bundle_sha256_after']
        metadata['actual_model_inference_calls'] = service.inference_calls
        metadata['finished_utc'] = dt.datetime.now(dt.timezone.utc).isoformat()
        save('finished')
    print(json.dumps(metadata,indent=2),flush=True)
    raise SystemExit(0 if metadata.get('success') and metadata['frozen_artifacts_unchanged'] else 1)


if __name__ == '__main__':
    main()
