"""Freeze deterministic test scenarios BEFORE evaluating any RF predictions."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
PROFILES=[{'id':'empty','free_memory_mib':None,'free_compute_quota':100},
          {'id':'compute25','free_memory_mib':16384,'free_compute_quota':25},
          {'id':'compute40','free_memory_mib':16384,'free_compute_quota':40},
          {'id':'compute60','free_memory_mib':16384,'free_compute_quota':60},
          {'id':'memory6g','free_memory_mib':6144,'free_compute_quota':80},
          {'id':'memory10g','free_memory_mib':10240,'free_compute_quota':60}]
MODELS={'4090':('NVIDIA-NVIDIA GeForce RTX 4090',4,24564),'l40':('NVIDIA-NVIDIA L40',3,46068),'5090':('NVIDIA-NVIDIA GeForce RTX 5090',4,32607)}


def node(model,states):
    name,count,capacity=MODELS[model]
    return {'name':'sim-'+model,'model':name,'capacity_mib':capacity,'states':[states[i%len(states)] for i in range(count)]}


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,default=ROOT/'results/rf-ablation-20261019');a=ap.parse_args();out=a.output;out.mkdir(parents=True,exist_ok=True)
    target=out/'plan.json'
    if target.exists():raise SystemExit('Plan exists; refusing to edit it after results have been observed.')
    cases=[]
    for i,p in enumerate(PROFILES):
        for j,q in enumerate(PROFILES):
            for task in ('training','inference'):
                for memory,compute in ((1024,10),(4096,25)):
                    cases.append({'id':f'verified-{i}-{j}-{task}-{memory}','family':'verified_cross_node',
                                  'task':task,'memory_mib':memory,'compute_quota':compute,
                                  'nodes':[node('4090',[p]),node('l40',[q])]})
    for model in ('4090','l40'):
        for offset in range(len(PROFILES)):
            states=PROFILES[offset:]+PROFILES[:offset]
            for task in ('training','inference'):
                cases.append({'id':f'within-{model}-{offset}-{task}','family':'verified_within_node','task':task,
                              'memory_mib':4096,'compute_quota':25,'nodes':[node(model,states)]})
    for task in ('training','inference'):
        for offset in (0,2,4):
            states=PROFILES[offset:]+PROFILES[:offset]
            cases.append({'id':f'pending-{offset}-{task}','family':'pending_only','task':task,'memory_mib':4096,'compute_quota':25,'nodes':[node('5090',states)]})
        for i in (0,2,4):
            cases.append({'id':f'mixed-{i}-{task}','family':'mixed_pending','task':task,'memory_mib':4096,'compute_quota':25,
                          'nodes':[node('4090',[PROFILES[i]]),node('l40',[PROFILES[2]]),node('5090',[PROFILES[0]])]})
    manifest=json.loads((ROOT/'models/pod-gpu-rf-v1/manifest.json').read_text(encoding='utf-8'))
    data={'protocol':'rf-single-term-ablation/v1','supervision':'synthetic','same_pod_and_snapshot_for_every_pair':True,
          'all_primary_candidates_must_fit':True,'model_version':manifest['model_version'],'artifact_sha256':manifest['artifact_sha256'],
          'weights':json.loads((ROOT/'models/pod-gpu-rf-v1/scoring-policy.json').read_text(encoding='utf-8'))['weights'],
          'rf_off':'subtract ONLY actual provisional RF term; preserve pending conservative proxy; no renormalization',
          'existing_seed_task':'training','profiles':PROFILES,'cases':cases}
    raw=json.dumps(data,indent=2,ensure_ascii=True).encode();target.write_bytes(raw)
    (out/'plan.sha256').write_text(hashlib.sha256(raw).hexdigest()+'\n',encoding='ascii')
    print('Frozen cases:',len(cases),'sha256',hashlib.sha256(raw).hexdigest())


if __name__=='__main__':main()
