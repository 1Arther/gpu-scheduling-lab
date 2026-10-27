"""CPU-only preflight regression; NEVER starts the scheduler against a cluster."""
import datetime as dt
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
    if sys.platform!='linux':raise SystemExit('Actual Linux required')
    out=ROOT/'results/phase1-cpu';out.mkdir(parents=True,exist_ok=False)
    sys.path.insert(0,str(ROOT))
    from gpu_lab.runtime.service import SuitabilityService
    from gpu_lab.runtime.http_server import make_server
    from gpu_lab.runtime.audit_server import audit_record
    def hashes():return {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'models/pod-gpu-rf-v1').iterdir() if p.is_file()}
    before=hashes();service=SuitabilityService()
    if service.model is None:raise SystemExit(service.model_error)
    log=(out/'rf-advice.jsonl').open('w',encoding='utf-8');lock=threading.Lock()
    def observer(p,r):
        with lock:log.write(json.dumps(audit_record(p,r),ensure_ascii=False)+'\n');log.flush()
    server=make_server(service,port=0,observer=observer);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    old=Path('/home/zbs/rf-ablation-20261019-1425/lab')
    env=os.environ.copy();env.update(GOOS='linux',GOARCH='amd64',CGO_ENABLED='0',GOMAXPROCS='2',
        GOCACHE=str(old/'.cache/go-build'),GOMODCACHE=str(old/'.cache/go-mod'),
        KUBECONFIG=str(ROOT/'nonexistent-offline-kubeconfig'),RF_REQUIRE_LINUX='1',
        RF_LIVE_ENDPOINT=f'http://127.0.0.1:{server.server_port}/v1/rank',
        RF_ABLATION_PLAN=str(ROOT/'results/rf-ablation-20261019/plan.json'),RF_ABLATION_OUTPUT=str(out),RF_DEMO_OUTPUT=str(out/'native-round6-regression'))
    meta={'started_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'hostname':platform.node(),'uname':list(platform.uname()),
          'root':str(ROOT),'python_executable':sys.executable,'model_version':service.version,'before_model_sha256':before,
          'cluster_resources_changed':False,'real_kubernetes_client':False,'model_trained':False,'commands':[]}
    commands=[('pytest',[sys.executable,'-m','pytest','tests/test_round7.py','tests/test_round5.py','tests/test_round6.py','tests/test_rf_ablation.py','-q','--junitxml='+str(out/'pytest.xml')],ROOT),
              ('go-tests',['/usr/bin/go','test','-p','2','-count=1','-json','./pkg/scheduler/api/devices/nvidia/vgpu','./pkg/scheduler/plugins/deviceshare','./pkg/scheduler/plugins/predicates'],ROOT/'scheduler/volcano-rf'),
              ('scheduler-build',['/usr/bin/go','build','-p','2','-o',str(out/'vc-scheduler'),'./cmd/scheduler'],ROOT/'scheduler/volcano-rf')]
    try:
        for name,cmd,cwd in commands:
            meta['stage']=name;(out/'execution.json').write_text(json.dumps(meta,indent=2))
            with (out/(name+'.log')).open('wb') as f:r=subprocess.run(cmd,cwd=cwd,env=env,stdout=f,stderr=subprocess.STDOUT)
            meta['commands'].append({'name':name,'command':cmd,'exit_code':r.returncode})
            if r.returncode:raise RuntimeError(name+' failed')
        meta['success']=True
    except Exception as exc:meta.update(success=False,error=str(exc))
    finally:
        server.shutdown();server.server_close();thread.join(5);log.close()
        meta.update(stage='finished',after_model_sha256=hashes(),finished_utc=dt.datetime.now(dt.timezone.utc).isoformat())
        meta['frozen_model_unchanged']=before==meta['after_model_sha256']
        (out/'execution.json').write_text(json.dumps(meta,indent=2))
    print(json.dumps(meta,indent=2));raise SystemExit(0 if meta['success'] else 1)


if __name__=='__main__':main()
