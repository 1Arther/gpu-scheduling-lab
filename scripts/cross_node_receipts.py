"""Read target-node kubelet receipts over existing strict-host-key SSH transport."""
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'results/cross-node-live-20261028'
REMOTE='/home/zbs/rf-cross-node-20261028'
TARGETS={'t3dgq':'zbs@172.16.10.33','t3dmg':'bszh@172.16.20.34','t3dgq-2':'bszh@172.16.10.34'}

def ssh(command,*,target=TARGETS['t3dgq'],payload=None,output=None,timeout=70):
    args=[sys.executable,str(ROOT/'scripts/rf_remote.py'),'--target',target,'--command',command,'--timeout',str(timeout)]
    if payload:args+=['--input-file',str(payload)]
    if output:args+=['--output-file',str(output)]
    r=subprocess.run(args,capture_output=True,text=True,timeout=timeout+10)
    if r.returncode:raise RuntimeError(r.stderr[-2000:])
    return r.stdout

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    poll=OUT/'poll.py'
    poll.write_text("import json,pathlib\np=pathlib.Path('"+REMOTE+"');s=json.loads((p/'execution/state.json').read_text());q=p/'checkpoint-request.json';print(json.dumps({'restored':s.get('restored'),'error':s.get('error'),'request':json.loads(q.read_text()) if q.exists() else None}))\n")
    seen=set();started=time.monotonic()
    while time.monotonic()-started<850:
        state=json.loads(ssh('python3 -',payload=poll,timeout=30))
        print(json.dumps(state),flush=True)
        if state.get('restored'):return
        req=state.get('request')
        if req and req['pod_uid'] not in seen:
            case=req['task'];node=req['node']
            if case not in ['training','inference','5090-path','l40-path']:raise RuntimeError('Unexpected case')
            blob=OUT/(case+'-checkpoint.json.gz')
            ssh('sudo -n gzip -c /var/lib/kubelet/device-plugins/kubelet_internal_checkpoint',target=TARGETS[node],output=blob,timeout=70)
            # Validate gzip/JSON before forwarding the exact target-node checkpoint.
            json.loads(gzip.decompress(blob.read_bytes()))
            dest=REMOTE+'/'+case+'-raw-checkpoint.json'
            command="python3 -c 'import gzip,sys,pathlib; p=pathlib.Path(\""+dest+"\"); q=p.with_suffix(\".tmp\"); q.write_bytes(gzip.decompress(sys.stdin.buffer.read())); q.replace(p)'"
            ssh(command,payload=blob,timeout=70)
            seen.add(req['pod_uid']);print('RECEIPT',node,case,req['pod_uid'],flush=True)
        time.sleep(3)
    raise RuntimeError('Receipt collector deadline')

if __name__=='__main__':main()
