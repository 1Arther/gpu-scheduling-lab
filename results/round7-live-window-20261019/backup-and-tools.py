import datetime,json,os,pathlib,shutil,subprocess,hashlib
root=pathlib.Path('/home/zbs/rf-round7-live-window-20261019');root.mkdir(exist_ok=False)
k=['sudo','-n','kubectl','--kubeconfig=/etc/kubernetes/admin.conf']
def call(cmd):
 r=subprocess.run(cmd,capture_output=True,text=True,timeout=30);return {'command':cmd,'exit_code':r.returncode,'stdout':r.stdout,'stderr':r.stderr}
info={'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'root':str(root),'tools':{n:shutil.which(n) for n in ['docker','podman','buildah','nerdctl','ctr','crictl','go','curl']},'backups':{}}
for kind,name in [('deployment','volcano-scheduler'),('configmap','volcano-scheduler-configmap')]:
 r=call(k+['get',kind,name,'-n','volcano-system','-o','json','--request-timeout=15s'])
 if r['exit_code']:raise RuntimeError(r['stderr'])
 path=root/(kind+'-original.json');path.write_text(r['stdout']);obj=json.loads(r['stdout']);info['backups'][kind]={'path':str(path),'uid':obj['metadata']['uid'],'resourceVersion':obj['metadata']['resourceVersion'],'sha256':hashlib.sha256(r['stdout'].encode()).hexdigest()}
 if kind=='deployment':info['original_replicas']=obj['spec']['replicas']
for ident,cmd in [('docker_info',['sudo','-n','docker','info','--format','{{.ServerVersion}}']),('docker_images',['sudo','-n','docker','images','--format','{{.Repository}}:{{.Tag}} {{.ID}}']),('disk',['df','-h',str(root)]),('processes',['pgrep','-af','gpu_lab.runtime|vc-scheduler-rf|round7'])]:
 info[ident]=call(cmd)
(root/'backup-manifest.json').write_text(json.dumps(info,indent=2));print(json.dumps(info,ensure_ascii=False))
