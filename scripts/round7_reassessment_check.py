"""Actual Linux CPU regression for both candidates. No real Kubernetes client."""
import datetime as dt
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading

ROOT=Path(__file__).resolve().parents[1]


def main():
    if sys.platform!='linux':raise SystemExit('Run on actual Linux')
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--output-dir',default='results/reassess-final-cpu');a=ap.parse_args()
    sys.path.insert(0,str(ROOT))
    from gpu_lab.runtime.service import SuitabilityService
    from gpu_lab.runtime.http_server import make_server
    from gpu_lab.runtime.audit_server import audit_record
    out=(ROOT/a.output_dir).resolve()
    if not out.is_relative_to(ROOT/'results'):raise SystemExit('Output must be inside this isolated project results directory')
    out.mkdir(parents=True,exist_ok=False)
    def hashes():return {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'models/pod-gpu-rf-v1').iterdir() if p.is_file()}
    before=hashes();service=SuitabilityService()
    if service.model is None:raise SystemExit(service.model_error)
    log=(out/'rf-advice.jsonl').open('w',encoding='utf-8');lock=threading.Lock()
    def observe(p,r):
        with lock:log.write(json.dumps(audit_record(p,r),ensure_ascii=False)+'\n');log.flush()
    server=make_server(service,port=0,observer=observe);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    old=Path('/home/zbs/rf-ablation-20261019-1425/lab')
    env=os.environ.copy();env.update(GOOS='linux',GOARCH='amd64',CGO_ENABLED='0',GOMAXPROCS='2',
      GOCACHE=str(old/'.cache/go-build'),GOMODCACHE=str(old/'.cache/go-mod'),
      KUBECONFIG=str(ROOT/'nonexistent-offline-kubeconfig'),RF_REQUIRE_LINUX='1',
      RF_LIVE_ENDPOINT=f'http://127.0.0.1:{server.server_port}/v1/rank',
      RF_ABLATION_PLAN=str(ROOT/'results/rf-ablation-20261019/plan.json'),RF_ABLATION_OUTPUT=str(out),
      RF_DEMO_OUTPUT=str(out/'native-round6-regression'),RF_WINDOW_CASES=str(ROOT/'k8s/round7-logical-window/policy-cases.json'))
    metadata={'started_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'host':platform.node(),'platform':platform.platform(),
      'root':str(ROOT),'model_version':service.version,'before_model_sha256':before,'commands':[],
      'real_kubernetes_client':False,'cluster_resources_changed':False,'model_trained':False}
    commands=[('pytest',[sys.executable,'-m','pytest',*['tests/'+p for p in ['test_round5.py','test_round6.py','test_rf_ablation.py','test_round7.py','test_round7_reassessment.py']],'-q','--junitxml='+str(out/'pytest.xml')],ROOT),
      ('go-tests',['/usr/bin/go','test','-p','2','-count=1','-json','./pkg/scheduler/api/devices/nvidia/vgpu','./pkg/scheduler/plugins/deviceshare','./pkg/scheduler/plugins/predicates'],ROOT/'scheduler/volcano-rf'),
      ('scheduler-build',['/usr/bin/go','build','-p','2','-o',str(out/'vc-scheduler'),'./cmd/scheduler'],ROOT/'scheduler/volcano-rf')]
    try:
        for name,cmd,cwd in commands:
            with (out/(name+'.log')).open('wb') as f:r=subprocess.run(cmd,cwd=cwd,env=env,stdout=f,stderr=subprocess.STDOUT)
            metadata['commands'].append({'name':name,'command':cmd,'exit_code':r.returncode})
            if r.returncode:raise RuntimeError(name+' failed')
        metadata['success']=True
        metadata['binary_sha256']=hashlib.sha256((out/'vc-scheduler').read_bytes()).hexdigest()
    except Exception as exc:metadata.update(success=False,error=str(exc))
    finally:
        server.shutdown();server.server_close();thread.join(5);log.close()
        metadata.update(finished_utc=dt.datetime.now(dt.timezone.utc).isoformat(),after_model_sha256=hashes())
        metadata['frozen_model_unchanged']=before==metadata['after_model_sha256']
        (out/'execution.json').write_text(json.dumps(metadata,indent=2))
    print(json.dumps(metadata,indent=2));raise SystemExit(0 if metadata['success'] else 1)


if __name__=='__main__':main()
