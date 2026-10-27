"""Export local Git changes and provenance without touching online files."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT=Path(__file__).resolve().parents[1]
SRC=ROOT/'scheduler/volcano-rf'
OUT=ROOT/'results/integration-v6'
BASE='20396e2d4742929feb761aca80424d2f487b7a8d'


def main():
    def git(*args):
        return subprocess.check_output(['git','-c',f'safe.directory={SRC.as_posix()}','-C',str(SRC),*args])
    if git('status','--porcelain').strip():raise ValueError('Commit/review source modifications before export.')
    head=git('rev-parse','HEAD').decode().strip()
    patch=git('diff','--binary',BASE,head)
    (OUT/'rf-job.patch').write_bytes(patch)
    git('apply','--reverse','--check',str(OUT/'rf-job.patch'))
    (OUT/'rf-git-log.txt').write_bytes(git('log','--format=fuller','-3'))
    names=git('diff','--name-only',BASE,head).decode().splitlines()
    manifest={'baseline_commit':BASE,'rf_commit':head,'branch':git('branch','--show-current').decode().strip(),
              'source_clean':True,'patch_sha256':hashlib.sha256(patch).hexdigest(),
              'files':{p:hashlib.sha256((SRC/p).read_bytes()).hexdigest() for p in names},'deployed':False}
    (OUT/'source-version.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    stage=ROOT/'scheduler/src'
    for name in ('rf_job.go','rf_job_test.go','rf_profiles.json'):
        shutil.copyfile(SRC/'pkg/scheduler/api/devices/nvidia/vgpu'/name,stage/name)
    print(json.dumps(manifest,indent=2))


if __name__=='__main__':main()
