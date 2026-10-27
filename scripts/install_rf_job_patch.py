"""Apply exported RF patch only to a clean independent baseline worktree.

Never resets or overwrites an existing changed checkout. No cluster operations.
The active development repository is already patched and is left untouched.
"""
import argparse
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]
BASE='20396e2d4742929feb761aca80424d2f487b7a8d'


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--target',type=Path,default=ROOT/'scheduler/volcano-rf')
    a=ap.parse_args();target=a.target.resolve()
    if not target.is_relative_to(ROOT):raise SystemExit('Target must stay inside this offline project.')
    def git(*args):
        return subprocess.run(['git','-c',f'safe.directory={target.as_posix()}','-C',str(target),*args],check=True,capture_output=True)
    head=git('rev-parse','HEAD').stdout.decode().strip()
    if (target/'pkg/scheduler/api/devices/nvidia/vgpu/rf_job.go').exists():
        raise SystemExit('RF code already exists; refusing to overwrite. Review git diff/log in this checkout.')
    if head!=BASE or git('status','--porcelain').stdout.strip():
        raise SystemExit('Expected preserved clean baseline. Existing changes must be retained/reviewed first.')
    patch=ROOT/'results/integration-v6/rf-job.patch'
    git('apply','--check',str(patch))
    git('apply',str(patch))
    print('Applied reviewable RF patch to',target,'; no commit or deployment performed.')


if __name__=='__main__':main()
