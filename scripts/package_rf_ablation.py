"""Package only source, frozen model and the preregistered plan; no credentials."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'results/rf-ablation-20261019'


def main():
    source = ROOT/'scheduler/volcano-rf'
    git = ['git','-c','safe.directory='+str(source).replace('\\','/'),'-C',str(source)]
    names = subprocess.check_output(git+['ls-files','-z']).decode().split('\0')
    names += subprocess.check_output(git+['ls-files','--others','--exclude-standard','-z']).decode().split('\0')
    media = {'.png','.jpg','.svg','.gif','.pdf','.jpeg','.webp','.ico','.mp4'}
    paths = {source/n for n in names if n and (source/n).is_file() and (source/n).suffix.lower() not in media}
    for directory in ('gpu_lab','models/pod-gpu-rf-v1','data','examples'):
        paths.update(p for p in (ROOT/directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    paths.update(ROOT/'tests'/n for n in ('test_round5.py','test_round6.py','test_rf_ablation.py'))
    paths.update(ROOT/'scripts'/n for n in ('run_rf_ablation_linux.py','report_rf_ablation.py'))
    paths.update(OUT/n for n in ('plan.json','plan.sha256'))
    paths.update(ROOT/n for n in ('requirements.txt','pyproject.toml'))
    paths = sorted(p for p in paths if p.is_file())
    record = {'base_commit':subprocess.check_output(git+['rev-parse','HEAD']).decode().strip(),
              'branch':subprocess.check_output(git+['branch','--show-current']).decode().strip(),
              'status':subprocess.check_output(git+['status','--short']).decode(),
              'source_files':{p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
              'contains_credentials':False,'online_source_modified':False}
    diff = subprocess.check_output(git+['diff','--binary','HEAD'])
    archive = OUT/'server-verification-source.tar.gz'
    with tarfile.open(archive,'w:gz') as tar:
        for path in paths:
            tar.add(path,arcname=path.relative_to(ROOT).as_posix(),recursive=False)
        for name,data in [('source-manifest.json',json.dumps(record,indent=2).encode()),('native-changes.diff',diff)]:
            info=tarfile.TarInfo(name);info.size=len(data);tar.addfile(info,io.BytesIO(data))
    (OUT/'server-package-manifest.json').write_text(json.dumps({**record,'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest()},indent=2),encoding='utf-8')
    print(json.dumps({'path':str(archive),'bytes':archive.stat().st_size,'files':len(paths)}))


if __name__ == '__main__':
    main()
