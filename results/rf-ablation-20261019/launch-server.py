import pathlib,subprocess,json,hashlib,os
root=pathlib.Path('/home/zbs/rf-ablation-20261019-1425/lab')
manifest=json.loads((root/'source-manifest.json').read_text())
for name,expected in manifest['source_files'].items():
 p=root/name
 assert root in p.resolve().parents and hashlib.sha256(p.read_bytes()).hexdigest()==expected,name
print('VERIFIED_FILES',len(manifest['source_files']))
bootstrap='''import pathlib,subprocess,json,sys,time
root=pathlib.Path('/home/zbs/rf-ablation-20261019-1425/lab')
def run(name,args):
 (root/'bootstrap-status.json').write_text(json.dumps({'stage':name,'args':args,'time':time.time()}))
 with (root/(name+'.log')).open('wb') as f:
  r=subprocess.run(args,cwd=root,stdout=f,stderr=subprocess.STDOUT)
 if r.returncode:raise RuntimeError(name+' exit '+str(r.returncode))
run('venv',['/opt/anaconda3/bin/python','-m','venv',str(root/'.venv')])
python=str(root/'.venv/bin/python')
run('pip',[python,'-m','pip','install','--disable-pip-version-check','--index-url','https://pypi.tuna.tsinghua.edu.cn/simple','-r','requirements.txt'])
run('verification',[python,'scripts/run_rf_ablation_linux.py','--go','/usr/bin/go','--jobs','2','--output','results/linux-verification'])
run('report',[python,'scripts/report_rf_ablation.py','results/linux-verification'])
(root/'bootstrap-status.json').write_text(json.dumps({'stage':'finished','time':time.time()}))
'''
p=root/'bootstrap.py';p.write_text(bootstrap)
with (root/'bootstrap.log').open('wb') as log:
 proc=subprocess.Popen(['/usr/bin/python3',str(p)],cwd=root,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
(root/'bootstrap-pid.json').write_text(json.dumps({'pid':proc.pid,'root':str(root),'source_files':len(manifest['source_files'])}))
print(json.dumps({'pid':proc.pid,'root':str(root)}))
