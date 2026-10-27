import pathlib,json,subprocess
root=pathlib.Path('/home/zbs')
print(json.dumps({'home_directories':[p.name for p in root.iterdir() if p.is_dir()],'opt_directories':[p.name for p in pathlib.Path('/opt').iterdir() if p.is_dir()],'python_bins':[str(p) for p in pathlib.Path('/usr/bin').glob('python3*') if p.is_file()]}))
f=root/'.conda/environments.txt'
if f.exists():print('CONDA_ENVS '+f.read_text())
f=root/'.bashrc'
if f.exists():print('CONDA_PATH_LINES '+json.dumps([l.strip() for l in f.read_text().splitlines() if ('/bin/conda' in l or 'conda.sh' in l)]))
for base in ('/opt','/data'):
 p=pathlib.Path(base)
 if p.is_dir():print(base,json.dumps([x.name for x in p.iterdir() if x.is_dir()]))
