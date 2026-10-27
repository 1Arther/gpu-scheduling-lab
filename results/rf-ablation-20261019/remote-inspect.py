import os,pathlib,subprocess,json,shutil
root=pathlib.Path('/home/zbs')
paths=[root/'miniconda3/bin/python',root/'anaconda3/bin/python']
for d in root.iterdir():
 if d.is_dir() and any(s in d.name.lower() for s in ('gpu','conda','venv')):
  for rel in ('.venv/bin/python','venv/bin/python','bin/python'):
   p=d/rel
   if p.exists():paths.append(p)
for p in sorted(set(paths)):
 if not p.exists():continue
 code="import sys,json; r={'python':sys.version,'executable':sys.executable};exec(\"try:\\n import sklearn,numpy,scipy,joblib,yaml,pytest\\n r['packages']={m.__name__:m.__version__ for m in (sklearn,numpy,scipy,joblib,yaml,pytest)}\\nexcept Exception as e:r['error']=str(e)\");print(json.dumps(r))"
 r=subprocess.run([str(p),'-c',code],capture_output=True,text=True,timeout=20)
 print(json.dumps({'path':str(p),'returncode':r.returncode,'stdout':r.stdout,'stderr':r.stderr}))
print(json.dumps({'nproc':os.cpu_count(),'home':str(root),'envs':str(list((root/'miniconda3/envs').glob('*/bin/python')))}))
