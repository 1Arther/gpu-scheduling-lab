from pathlib import Path
import shutil,json
old=Path('/home/zbs/rf-round7-preflight-20261019')
new=Path('/home/zbs/rf-round7-reassess-20261019')
assert new.parent == Path("/home/zbs") and new.name == "rf-round7-reassess-20261019"
new.mkdir(exist_ok=True)
assert not (new/"results/phase1-cpu").exists()
for name in ['gpu_lab','models','data','profiles','scheduler','scripts','tests','k8s']:
 if (old/name).exists():shutil.copytree(old/name,new/name,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','.pytest_cache','.git'))
for name in ['pyproject.toml','requirements.txt']:
 if (old/name).exists():shutil.copy2(old/name,new/name)
plan=new/'results/rf-ablation-20261019';plan.mkdir(parents=True,exist_ok=True)
shutil.copy2(old/'results/rf-ablation-20261019/plan.json',plan/'plan.json')
print(json.dumps({'independent_root':str(new),'cluster_resources_changed':False}))
