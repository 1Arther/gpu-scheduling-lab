#!/usr/bin/env python3
"""Download a pinned official Go toolchain into the project only; verify SHA256."""
import argparse
import hashlib
import json
from pathlib import Path
import urllib.request
import zipfile


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',required=True);ap.add_argument('--version',default='go1.26.3');a=ap.parse_args()
    root=Path(a.output).resolve();root.mkdir(parents=True,exist_ok=True)
    releases=json.load(urllib.request.urlopen('https://go.dev/dl/?mode=json&include=all',timeout=40))
    release=next(r for r in releases if r['version']==a.version)
    entry=next(f for f in release['files'] if f['os']=='windows' and f['arch']=='amd64' and f['kind']=='archive')
    archive=root/entry['filename'];url='https://go.dev/dl/'+entry['filename']
    if not archive.exists():
        with urllib.request.urlopen(url,timeout=60) as response, archive.open('wb') as f:
            while chunk:=response.read(1024*1024):f.write(chunk)
    actual=hashlib.sha256(archive.read_bytes()).hexdigest()
    if actual!=entry['sha256']:raise ValueError('Go archive checksum mismatch')
    if not (root/'go/bin/go.exe').exists():
        with zipfile.ZipFile(archive) as z:z.extractall(root)
    (root/'toolchain-provenance.json').write_text(json.dumps(dict(version=a.version,url=url,sha256=actual,official_file_metadata=entry),indent=2),encoding='utf-8')
    print(root/'go/bin/go.exe')


if __name__=='__main__':main()
