"""Incremental delivery archive for the existing frozen project."""
import argparse
from pathlib import Path
from zipfile import ZipFile,ZIP_DEFLATED
from .experiment import ROOT,DEFAULT,dump,digest,verify_freeze

def package(out=DEFAULT):
    out=Path(out);verify_freeze(out)
    sources=[p for p in (ROOT/'gpu_lab/queue_rl').rglob('*') if p.is_file() and '__pycache__' not in str(p)]
    sources+=[ROOT/'tests/test_queue_rl.py']
    results=[p for p in out.rglob('*') if p.is_file() and p.name not in ('stage3-code-and-results.zip','release-manifest.json','package-info.json')]
    paths=sorted(set(sources+results))
    dump(out/'release-manifest.json',{str(p.relative_to(ROOT)).replace('\\','/'):digest(p) for p in paths})
    target=out/'stage3-code-and-results.zip'
    with ZipFile(target,'w',ZIP_DEFLATED,compresslevel=6) as z:
        for p in paths+[out/'release-manifest.json']:z.write(p,str(p.relative_to(ROOT)))
    dump(out/'package-info.json',dict(archive=str(target),bytes=target.stat().st_size,sha256=digest(target),files=len(paths)+1,
        incremental=True,requires='Existing frozen gpu-scheduling-lab including RF bundle and prior stage artifacts',
        excludes=['virtual environments','excluded pilot','original first/second stage files']))
    print(target)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,default=DEFAULT);a=p.parse_args();package(a.out)
