import datetime,json,pathlib,subprocess
root=pathlib.Path('/home/zbs/rf-round7-live-window-20261019')
cmd=['sudo','-n','docker','pull','docker.m.daocloud.io/library/python:3.12-slim-bookworm']
with (root/'pull-python.log').open('wb') as f:r=subprocess.run(cmd,stdout=f,stderr=subprocess.STDOUT,timeout=180)
print(json.dumps({'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'command':cmd,'exit_code':r.returncode,'log':(root/'pull-python.log').read_text()[-6000:]}))
