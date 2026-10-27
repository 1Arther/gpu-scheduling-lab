"""Build/import only the independent approved experiment images. No scheduler startup."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--root',type=Path,required=True);a=ap.parse_args()
    root=a.root.resolve();lab=root/'lab';out=root/'build';out.mkdir(exist_ok=True)
    record={'started_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'images':{},'commands':[],'cluster_resources_changed':False}
    def run(name,cmd,timeout=600):
        with (out/(name+'.log')).open('w') as f:
            p=subprocess.run(cmd,cwd=lab,stdout=f,stderr=subprocess.STDOUT,timeout=timeout)
        record['commands'].append({'name':name,'command':cmd,'exit_code':p.returncode})
        (out/'record.json').write_text(json.dumps(record,indent=2))
        if p.returncode:raise RuntimeError(name+' failed; see '+str(out/(name+'.log')))
        return (out/(name+'.log')).read_text()
    binary=Path('/home/zbs/rf-round7-reassess-20261019/results/reassess-release-cpu/vc-scheduler')
    assert hashlib.sha256(binary.read_bytes()).hexdigest()=='501e9ece8f838fc2abcc82f5ea731e399178705f7a0bda0d6c02c8eac6b0d7e9'
    assert 'statically linked' in run('binary-file',['file',str(binary)])
    (lab/'build/round7').mkdir(parents=True,exist_ok=True);shutil.copyfile(binary,lab/'build/round7/vc-scheduler')
    (lab/'Dockerfile.scheduler.live').write_text('FROM scratch\nCOPY --chmod=0555 build/round7/vc-scheduler /vc-scheduler-rf\nUSER 1000:1000\nENTRYPOINT ["/vc-scheduler-rf"]\n')
    # The numerical stack versions match the frozen bundle's tested environment.
    (lab/'requirements-runtime.txt').write_text('numpy==2.2.6\nscipy==1.16.3\nscikit-learn==1.7.2\njoblib==1.5.2\nthreadpoolctl==3.6.0\nPyYAML==6.0.3\n')
    (lab/'Dockerfile.rf.live').write_text('''FROM docker.m.daocloud.io/library/python@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements-runtime.txt /app/requirements-runtime.txt
RUN pip install --no-cache-dir --index-url https://pypi.tuna.tsinghua.edu.cn/simple -r requirements-runtime.txt
COPY gpu_lab /app/gpu_lab
COPY models/pod-gpu-rf-v1 /app/models/pod-gpu-rf-v1
COPY data/synthetic/scoring-policy.json /app/data/synthetic/scoring-policy.json
USER 1000:1000
ENTRYPOINT ["python", "-m", "gpu_lab.runtime.audit_server"]
''')
    for component in ['scheduler','rf']:
        repository='docker.io/gpu-lab/'+('volcano-rf' if component=='scheduler' else 'rf-model')
        tag=repository+':live-20261019'
        run('docker-build-'+component,['sudo','-n','docker','build','--pull=false','-f','Dockerfile.'+component+'.live','-t',tag,'.'])
        archive=out/(component+'-image.tar')
        run('docker-save-'+component,['sudo','-n','docker','save','-o',str(archive),tag])
        run('ctr-import-'+component,['sudo','-n','ctr','-n','k8s.io','images','import',str(archive)])
        listing=run('ctr-list-'+component,['sudo','-n','ctr','-n','k8s.io','images','list'])
        line=next(s for s in listing.splitlines() if s.split() and s.split()[0]==tag)
        digest=line.split()[2];assert digest.startswith('sha256:') and len(digest)==71
        immutable=repository+'@'+digest
        run('ctr-tag-'+component,['sudo','-n','ctr','-n','k8s.io','images','tag',tag,immutable])
        inspect=run('cri-inspect-'+component,['sudo','-n','crictl','inspecti','-o','json',immutable])
        record['images'][component]={'tag':tag,'immutable_reference':immutable,'cri_image_id':json.loads(inspect)['status']['id']}
    # Run only a CPU image smoke test, with no GPU/device mount or Kubernetes token.
    run('rf-image-smoke',['sudo','-n','docker','run','--rm','--network=none','--read-only','--tmpfs','/tmp',
        '--entrypoint','python',record['images']['rf']['tag'],'-c',
        'from gpu_lab.runtime.service import SuitabilityService; s=SuitabilityService(); assert s.model is not None, s.model_error; print(s.version)'])
    record.update(finished_utc=dt.datetime.now(dt.timezone.utc).isoformat(),success=True)
    (out/'record.json').write_text(json.dumps(record,indent=2));print(json.dumps(record,indent=2))


if __name__=='__main__':main()
