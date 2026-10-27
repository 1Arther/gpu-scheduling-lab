import json,pathlib
f=pathlib.Path('/home/zbs/rf-ablation-20261019-1425/lab/results/linux-verification-final/go-tests.log')
for line in f.read_text().splitlines():
 try:r=json.loads(line)
 except:continue
 if r.get('Action')=='fail' or 'Pending5090' in r.get('Test',''):
  print(json.dumps(r))
