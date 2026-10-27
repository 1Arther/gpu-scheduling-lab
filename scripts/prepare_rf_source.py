#!/usr/bin/env python3
"""Read-only export of the deployed source working tree into an independent local repo.

No command or file is changed remotely. Existing binaries, ignored build output,
and credentials are excluded. Preserve tracked files, untracked Go/proto files,
original Git status/diff/log, file hashes and archive hash locally.
"""
import argparse
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import tempfile

REMOTE = r'''
import hashlib,io,json,os,pathlib,subprocess,sys,tarfile
root=pathlib.Path('/home/zbs/volcano-v1.13.1-dqn')
env=os.environ.copy();env['GIT_OPTIONAL_LOCKS']='0'
def git(*args):
 print('reading git '+str(args),file=sys.stderr,flush=True)
 return subprocess.check_output(['git','-c','core.fsmonitor=false','-C',str(root),*args],env=env,timeout=45)
tracked=git('ls-files','-z').decode().split('\0')
extra=git('ls-files','--others','--exclude-standard','-z','--','pkg','cmd').decode().split('\0')
media={'.png','.jpg','.jpeg','.gif','.webp','.svg','.pdf','.mp4','.ico'}
names=sorted(set(x for x in tracked if x and pathlib.Path(x).suffix.lower() not in media) | set(x for x in extra if x and pathlib.Path(x).suffix in ('.go','.proto')))
files=[]
metadata={'root':str(root),'git_head':git('rev-parse','HEAD').decode().strip(),
          'git_status':git('status','--short','--untracked-files=no').decode(),'git_diff':git('diff','--binary','HEAD').decode(),
          'git_log':git('log','-15','--format=fuller').decode(),'untracked_source':[x for x in extra if x in names],
          'read_only':True,'excluded':'ignored files, build outputs, untracked non-Go/proto files, documentation binary media'}
with tarfile.open(fileobj=sys.stdout.buffer,mode='w|gz') as tar:
 print('exporting '+str(len(names))+' source paths',file=sys.stderr,flush=True)
 for name in names:
  p=root/name
  if not p.exists() and not p.is_symlink():continue
  if p.is_symlink():
   target=p.resolve()
   if root not in target.parents:raise RuntimeError('external symlink '+name)
  elif p.is_file():
   if p.stat().st_size>32*1024*1024:raise RuntimeError('Unexpected large source file '+name)
   files.append({'path':name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
  tar.add(p,arcname='source/'+name,recursive=False)
 metadata['files']=files
 raw=json.dumps(metadata,ensure_ascii=False,indent=2).encode()
 info=tarfile.TarInfo('source-provenance.json');info.size=len(raw);tar.addfile(info,io.BytesIO(raw))
'''


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',required=True);ap.add_argument('--archive-dir',required=True)
    ap.add_argument('--password-env',default='GPU_LAB_SSH_PASSWORD');a=ap.parse_args()
    project=Path(__file__).resolve().parents[1];dest=Path(a.output).resolve();saved=Path(a.archive_dir).resolve()
    if dest.exists() or saved.exists():raise SystemExit('Refusing to overwrite an existing source or archive directory')
    password=os.environ.get(a.password_env)
    if not password:raise SystemExit('Set the password environment variable; credentials are not saved')
    known=project.parent/'server_known_hosts'
    args=['ssh','-T','-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile='+str(known),'-o','ConnectTimeout=8',
          '-o','NumberOfPasswordPrompts=1','zbs@172.16.10.33','python3 -']
    temp_root=Path(os.environ['LOCALAPPDATA'])/'Temp'
    with tempfile.TemporaryDirectory(prefix='rf-source-askpass-',dir=temp_root) as temp:
        p=Path(temp);helper=p/'answer.py';helper.write_text('import os,sys\nsys.stdout.write(os.environ.get("GPU_LAB_SSH_PASSWORD", ""))\n',encoding='utf-8')
        executable=p/'answer.cmd';executable.write_text('@echo off\r\n"'+getattr(sys,'_base_executable',sys.executable)+'" "'+str(helper)+'"\r\n',encoding='ascii')
        env=os.environ.copy();env.update(SSH_ASKPASS=str(executable),SSH_ASKPASS_REQUIRE='force',DISPLAY='gpu-lab',GPU_LAB_SSH_PASSWORD=password)
        saved.mkdir(parents=True)
        archive=saved/'original-working-tree.tar.gz'
        with archive.open('wb') as stdout,(saved/'export.stderr.txt').open('wb') as stderr:
            r=subprocess.run(args,input=REMOTE.encode(),stdout=stdout,stderr=stderr,env=env,timeout=600)
    if r.returncode:raise SystemExit('Read-only source export failed: '+(saved/'export.stderr.txt').read_text(errors='replace'))
    with tarfile.open(archive,mode='r:gz') as tar:
        members=tar.getmembers()
        for m in members:
            parts=PurePosixPath(m.name).parts
            if m.name.startswith('/') or '..' in parts:raise ValueError('Unsafe archive member '+m.name)
        meta=json.load(tar.extractfile('source-provenance.json'))
        dest.mkdir(parents=True)
        for m in members:
            if not m.name.startswith('source/'):continue
            rel=m.name[len('source/'):];target=dest/rel
            if m.isdir():target.mkdir(parents=True,exist_ok=True)
            elif m.isfile():
                target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(tar.extractfile(m).read())
            elif m.issym():
                # Helm directory symlinks are not needed for Go compilation.
                # Preserve their exact original metadata in the archive.
                meta.setdefault('symlinks',[]).append({'path':rel,'target':m.linkname,'local_status':'not materialized on Windows'})
    meta.update(archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),exported_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                local_source=str(dest),source_file_count=len(meta['files']))
    (saved/'source-provenance.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
    for name,value in [('original.diff',meta['git_diff']),('original-status.txt',meta['git_status']),('original-log.txt',meta['git_log'])]:
        (saved/name).write_text(value,encoding='utf-8')
    subprocess.run(['git','init','-b','codex/rf-job-v1.13.1',str(dest)],check=True,capture_output=True)
    common=['git','-c','safe.directory='+str(dest).replace('\\','/'),'-C',str(dest)]
    subprocess.run(common+['add','-A'],check=True,capture_output=True)
    subprocess.run(common+['-c','user.name=GPU Lab','-c','user.email=gpu-lab@local.invalid','commit','-m','Preserve deployed Volcano v1.13.1 custom source baseline'],check=True,capture_output=True)
    print(json.dumps({k:meta[k] for k in ('git_head','source_file_count','archive_sha256','local_source')},ensure_ascii=False))


if __name__=='__main__':main()
