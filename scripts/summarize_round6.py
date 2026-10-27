"""Summarize recorded tests/native experiments; no training or cluster calls."""
from pathlib import Path
import csv
import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'results/integration-v6'
SOURCE=ROOT/'scheduler/volcano-rf'


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    data=json.loads((OUT/'native-experiments.json').read_text(encoding='utf-8'))
    exchanges=json.loads((OUT/'rf-http-exchanges.json').read_text(encoding='utf-8'))
    by_request={e['request']['request_id']:e for e in exchanges}
    uuid_evidence=[]
    for decision in data['decisions']:
        plan=next(c['plan'] for c in decision['candidate_nodes'] if c['node']==decision['node'])
        raw=by_request[plan['RequestID']]
        rf_uuid=raw['response']['assignments'][0]['gpu_uuid']
        actual=decision['allocate_annotation_uuid']
        if rf_uuid!=actual:raise ValueError('HTTP RF UUID differs from native Allocate annotation')
        uuid_evidence.append({'stage':decision['stage'],'request_id':plan['RequestID'],'http_status':raw['http_status'],
                              'python_response_uuid':rf_uuid,'native_allocate_annotation_uuid':actual,'equal':True})
    with (OUT/'http-to-allocate-uuid.csv').open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(uuid_evidence[0]));w.writeheader();w.writerows(uuid_evidence)
    events=[]
    for line in (OUT/'go-tests.jsonl').read_text(encoding='utf-8-sig').splitlines():
        try:events.append(json.loads(line))
        except json.JSONDecodeError:raise ValueError('incomplete Go test output; wait for completion')
    packages=[e for e in events if e['Action'] in ('pass','fail','skip') and not e.get('Test')]
    cases=[{'package':e['Package'],'test':e['Test'],'status':e['Action'],'elapsed_s':e.get('Elapsed')}
           for e in events if e.get('Test') and e['Action'] in ('pass','fail','skip')]
    if not packages or not cases or not any(r['test']=='TestRFLivePythonNativeExperiments' and r['status']=='pass' for r in cases):
        raise ValueError('Go test file is not complete or real Python integration did not pass. Wait/re-run tests first.')
    with (OUT/'go-test-cases.csv').open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['package','test','status','elapsed_s']);w.writeheader();w.writerows(cases)
    xml=ET.parse(OUT/'pytest-all.xml').getroot()
    suites=[xml] if xml.tag=='testsuite' else list(xml)
    py={k:sum(int(s.get(k,0)) for s in suites) for k in ('tests','failures','errors','skipped')}
    manifest=json.loads((ROOT/'models/pod-gpu-rf-v1/manifest.json').read_text(encoding='utf-8'))
    frozen={p:sha(ROOT/'models/pod-gpu-rf-v1'/p)==h for p,h in manifest['artifact_sha256'].items()}
    baseline=json.loads((OUT/'source-baseline-complete/source-provenance.json').read_text(encoding='utf-8'))
    changed=[]
    for row in baseline['files']:
        p=SOURCE/row['path']
        if not p.exists() or sha(p)!=row['sha256']:changed.append(row['path'])
    dqn={p:sha(SOURCE/p)==next(r['sha256'] for r in baseline['files'] if r['path']==p) for p in baseline['untracked_source']}
    binary=OUT/'bin/vc-scheduler-rf-linux-amd64'
    state=[]
    for row in data['dynamic_transitions']:
        for g,c in zip(row['ledger'],row['classification']):
            cls=c['classification']
            state.append({'stage':row['stage'],'uuid':g['uuid'],'free_memory_mib':g['registered_memory_mib']-g['allocated_memory_mib'],
                          'free_compute_quota':100-g['allocated_compute_quota'],'instances':g['allocated_vgpu_count'],
                          'training':cls['training_adapted'],'inference':cls['inference_adapted'],'category':cls['label'],
                          'status':cls['prediction_status'],'score':row['score']})
    with (OUT/'dynamic-states.csv').open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(state[0]));w.writeheader();w.writerows(state)
    choices=[{k:r[k] for k in ('stage','task','node','recommended_uuid','allocate_annotation_uuid','score','fallback')} for r in data['decisions']]
    with (OUT/'uuid-choices.csv').open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(choices[0]));w.writeheader();w.writerows(choices)
    summary={'evidence_level':data['evidence_level'],'python':py,'go_packages':packages,
             'go_passed_cases_including_subtests':sum(r['status']=='pass' for r in cases),
             'go_failed_cases':sum(r['status']=='fail' for r in cases),'go_skipped_cases':sum(r['status']=='skip' for r in cases),
             'frozen_artifacts_match':frozen,'original_dqn_files_unchanged':dqn,'modified_original_source_files':changed,
             'source_original_git_head':baseline['git_head'],'source_file_count':len(baseline['files']),
             'native_uuid_match_all':all(r['recommended_uuid']==r['allocate_annotation_uuid'] for r in data['decisions']),
             'actual_http_exchanges':len(exchanges),'http_response_uuid_matches_native_annotation':True,
             'binary':{'path':str(binary.relative_to(ROOT)),'size':binary.stat().st_size,'sha256':sha(binary)},
             'model_version':data['model_version'],'live_cluster_deployed':False,'supervision':'synthetic'}
    (OUT/'verification-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    md=['# 第六轮离线联调实测记录','','本文件仅从实际测试记录和原生 Allocate 输出生成。没有部署集群。',
        '',f'Python：{py}；Go 含子测试通过 {summary["go_passed_cases_including_subtests"]}，失败 {summary["go_failed_cases"]}，跳过 {summary["go_skipped_cases"]}。',
        '', '## 原生最终 UUID', '', '|阶段|任务|推荐 UUID|Allocate 注解 UUID|分数|','|---|---|---|---|---|']
    md += [f'|{r["stage"]}|{r["task"]}|{r["recommended_uuid"]}|{r["allocate_annotation_uuid"]}|{r["score"]:.4f}|' for r in choices]
    md += ['', '## L40 逐步分类', '', '|阶段|剩余 MiB|剩余配额|实例|类别|状态|','|---|---|---|---|---|---|']
    md += [f'|{r["stage"]}|{r["free_memory_mib"]}|{r["free_compute_quota"]}|{r["instances"]}|{r["category"]}|{r["status"]}|' for r in state]
    md += ['', '## Go 包', '', '|包|结果|秒|','|---|---|---|']
    md += [f'|{r["Package"]}|{r["Action"]}|{r.get("Elapsed")}|' for r in packages]
    md += ['', '冻结文件全部通过原 manifest SHA256 检查：'+str(all(frozen.values())),
           '；原 DQN 客户端/生成代码全部未改：'+str(all(dqn.values()))+'。',
           '', 'Windows 当前没有可用的 C 编译器；未运行 Go -race。普通多 Pod 并发与 stale/retry 单测已执行，不等同于线上多调度器竞态证明。',
           '', '详细原始记录：native-experiments.json、go-tests.jsonl、go-test-cases.csv、pytest-all.xml。']
    (OUT/'test-results.md').write_text('\n'.join(md)+'\n',encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
