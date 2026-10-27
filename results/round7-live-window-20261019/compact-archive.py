import hashlib,io,json,pathlib,sys,tarfile
root=pathlib.Path('/home/zbs/rf-round7-live-window-20261019-attempt2');folder=root/'execution'
with tarfile.open(fileobj=sys.stdout.buffer,mode='w|gz') as tf:
 def put(name,data):
  data=data if isinstance(data,bytes) else data.encode();m=tarfile.TarInfo(name);m.size=len(data);tf.addfile(m,io.BytesIO(data))
 for p in sorted(folder.glob('*')):
  if not p.is_file() or p.name.startswith('command-') or p.suffix not in ['.json','.yaml','.jsonl','.txt']:continue
  put('execution/'+p.name,p.read_bytes())
 for name in ['deployment-original.json','configmap-original.json','authorization.json','execution-console.log','pytest-fix-linux.xml','pytest-fix-linux.log','build/record.json']:
  put(name,(root/name).read_bytes())
 native=set();rf=set();hashes={}
 for p in folder.glob('*.log'):
  raw=p.read_bytes();hashes[p.name]=hashlib.sha256(raw).hexdigest()
  for line in raw.decode().splitlines():
   if any(s in line for s in ['rf-job UUID allocation','rf-job logical release','rf-job deterministic fallback']):native.add(line)
   if '"event": "rf_advice"' in line or '"event": "model_loaded"' in line:rf.add(line)
 put('execution/native-events.log','\n'.join(sorted(native))+'\n')
 put('execution/rf-events.log','\n'.join(sorted(rf))+'\n')
 put('execution/raw-log-sha256.json',json.dumps(hashes,indent=2))
