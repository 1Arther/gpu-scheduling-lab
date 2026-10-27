"""SFTP/scp download of this experiment's evidence only, strict host key."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    ap=argparse.ArgumentParser();ap.add_argument('archive',choices=['rf-round8-live-20261020-raw.tar.gz','rf-round8-live-20261020-attempt2-raw.tar.gz'])
    ap.add_argument('output',type=Path);a=ap.parse_args()
    root=Path(__file__).resolve().parents[1]
    if not os.environ.get('GPU_LAB_SSH_PASSWORD'):raise SystemExit('SSH password environment required; never persisted')
    with tempfile.TemporaryDirectory(prefix='rf8-fetch-') as tmp:
        p=Path(tmp);(p/'answer.py').write_text('import os,sys\nsys.stdout.write(os.environ["GPU_LAB_SSH_PASSWORD"])\n')
        (p/'answer.cmd').write_text('@echo off\r\n"'+getattr(sys,'_base_executable',sys.executable)+'" "'+str(p/'answer.py')+'"\r\n')
        env={**os.environ,'SSH_ASKPASS':str(p/'answer.cmd'),'SSH_ASKPASS_REQUIRE':'force','DISPLAY':'rf8-fetch'}
        cmd=['scp','-q','-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile='+str(root.parent/'server_known_hosts'),
             '-o','ConnectTimeout=20','-o','NumberOfPasswordPrompts=1','-o','PreferredAuthentications=password','-o','PubkeyAuthentication=no',
             'zbs@172.16.10.33:/home/zbs/'+a.archive,str(a.output)]
        subprocess.run(cmd,env=env,check=True,timeout=180)
    print(a.output,a.output.stat().st_size)


if __name__=='__main__':main()
