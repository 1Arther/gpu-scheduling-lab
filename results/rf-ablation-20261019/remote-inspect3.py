import pathlib,subprocess,json,hashlib
root=pathlib.Path('/home/zbs/rf-ablation-20261019-1425')
print('SOURCE_EXISTS',root.exists())
for p in (pathlib.Path('/opt/anaconda3/bin/python'),pathlib.Path('/home/zbs/.conda/envs/cp2025/bin/python')):
 if not p.exists():continue
 code="import sys,json; r={'python':sys.version,'executable':sys.executable};exec(\"try:\\n import sklearn,numpy,scipy,joblib,yaml,pytest\\n r['packages']={m.__name__:m.__version__ for m in (sklearn,numpy,scipy,joblib,yaml,pytest)}\\nexcept Exception as e:r['error']=str(e)\");print(json.dumps(r))"
 r=subprocess.run([str(p),'-c',code],capture_output=True,text=True,timeout=30)
 print(json.dumps({'path':str(p),'stdout':r.stdout,'stderr':r.stderr,'returncode':r.returncode}))
