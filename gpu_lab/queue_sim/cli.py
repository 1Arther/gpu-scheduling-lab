"""Run mechanism fixtures using inference-only frozen RF placement."""
import argparse
from dataclasses import asdict
from hashlib import sha256
import csv
import gzip
import json
from pathlib import Path
from ..profiles import ROOT, load_profiles
from ..resources import GPUState
from ..runtime.bundle import DEFAULT_BUNDLE
from .models import Config, AvailabilityEvent, job_record, job_from_record
from .placement import Cluster, FrozenPlacement
from .engine import Simulator
from .policies import POLICIES
from .scenarios import SCENARIOS
from .audit import verify_result


def write_json(path, value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')


def frozen_hashes(bundle):
    files=list(Path(bundle).glob('*'))
    for directory in ('runtime','crossgpu','nvidia'):
        files += list((ROOT/'gpu_lab'/directory).glob('*.py'))
    files += [ROOT/'gpu_lab'/name for name in ('resources.py','profiles.py','fleet.py','scoring.py','features.py')]
    return {str(p.resolve()):sha256(p.read_bytes()).hexdigest() for p in sorted(files) if p.is_file()}


def input_record(fleet,jobs,availability,config):
    return dict(schema='hrrn-queue-mvp/v1',data_kind='synthetic_only_not_GPU_performance',
        devices=[dict(gpu_id=k,node=k.rsplit('/gpu-',1)[0],profile_id=g.profile.profile_id,max_instances=g.max_instances)
                 for k,g in sorted(fleet.items())],
        jobs=[job_record(j) for j in jobs],availability=[asdict(e) for e in availability],config=asdict(config))


def load_input(path):
    data=json.loads(Path(path).read_text(encoding='utf-8'))
    if data.get('schema')!='hrrn-queue-mvp/v1' or data.get('data_kind')!='synthetic_only_not_GPU_performance':
        raise ValueError('Expected explicitly synthetic hrrn-queue-mvp/v1 input')
    profiles=load_profiles();fleet={};nodes={}
    for g in data['devices']:
        if g['gpu_id'] in fleet:raise ValueError('Duplicate GPU ID')
        fleet[g['gpu_id']]=GPUState(g['gpu_id'],profiles[g['profile_id']],g['max_instances']);nodes[g['gpu_id']]=g['node']
    return fleet,tuple(job_from_record(j) for j in data['jobs']),tuple(AvailabilityEvent(**e) for e in data['availability']),Config(**data['config']),nodes


def run_suite(out,case_names,seed,policies,bundle=DEFAULT_BUNDLE,trace=None):
    out=Path(out).resolve();out.mkdir(parents=True,exist_ok=False)
    before=frozen_hashes(bundle);placement=FrozenPlacement(bundle)
    rows=[];audits=[];inputs={};diagnostics=[]
    for case in case_names:
        if trace:
            fleet,jobs,availability,config,nodes=load_input(trace)
        else:
            fleet,jobs,availability=SCENARIOS[case](seed) if case=='seeded_smoke' else SCENARIOS[case]()
            config=Config();nodes=None
        payload=input_record(fleet,jobs,availability,config)
        if nodes is not None:
            for d in payload['devices']:d['node']=nodes[d['gpu_id']]
        write_json(out/f'{case}-input.json',payload)
        inputs[case]=sha256((out/f'{case}-input.json').read_bytes()).hexdigest()
        for policy in policies:
            simulator=Simulator(Cluster(fleet,nodes),jobs,placement,policy,config,availability)
            result=simulator.run()
            diagnostics.append(dict(case=case,policy=policy,decisions=simulator.planning_diagnostics))
            audit=verify_result(result,jobs,fleet,nodes);audits.append(dict(case=case,policy=policy,**audit))
            with gzip.open(out/f'{case}-{policy}.json.gz','wt',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,allow_nan=False)
            m=result['metrics'];by_id={r['job_id']:r for r in result['jobs']}
            row=dict(case=case,policy=policy,mean_wait=m['mean_wait'],p95_wait=m['p95_wait'],
                long_wait_ratio=m['long_wait_ratio'],completed_count=m['completed_count'],completed_total=m['completed_total'],
                cancelled=m['cancelled_count'],rejected=m['rejected_count'],pending=m['pending_count'],
                large_start=by_id.get('10-large-two-gpu',{}).get('start_at'),spare_start=by_id.get('30-spare-capacity',{}).get('start_at'),
                **{f'logical_reserved_rate_{n}':v for n,v in m['logical_reserved_rate'].items()},
                **{f'protected_idle_capacity_time_{n}':v for n,v in m['protected_idle_capacity_time'].items()})
            rows.append(row)
            print(f"{case:14} {policy:15} large_start={row['large_start']} mean_wait={m['mean_wait']} p95={m['p95_wait']} completed={m['completed_count']}/{m['completed_total']}",flush=True)
    after=frozen_hashes(bundle)
    if before!=after:raise RuntimeError('Existing RF artifacts/source changed')
    keys=list(dict.fromkeys(k for row in rows for k in row))
    with (out/'summary.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=keys);writer.writeheader();writer.writerows(rows)
    write_json(out/'verification.json',dict(success=True,runs=len(rows),
        events_checked=sum(a['events_checked'] for a in audits),atomic_starts=sum(a['atomic_starts'] for a in audits),
        guarded_starts=sum(a['guarded_starts'] for a in audits),audits=audits,existing_frozen_files_unchanged=True))
    write_json(out/'runtime-diagnostics.json',dict(deterministic=False,used_by_scheduler=False,
        semantics='wall-clock planning measurements only; excluded from reproducible result and synthetic time',runs=diagnostics))
    sources={p.relative_to(ROOT).as_posix():sha256(p.read_bytes()).hexdigest() for p in sorted(Path(__file__).parent.glob('*.py'))}
    write_json(out/'manifest.json',dict(schema='hrrn-queue-mvp/v1',synthetic_only=True,seed=seed,
        model_version=placement.version,model_usage='load and inference only',frozen_files_sha256=before,
        inputs_sha256=inputs,queue_sources_sha256=sources,policies=list(policies),case_names=case_names,
        formal_benchmark=False,multi_seed_efficacy_experiment=False))
    table='\n'.join(f"| {r['policy']} | {r['large_start']} | {r['spare_start']} | {r['mean_wait']} | {r['p95_wait']} | {r['long_wait_ratio']} | {r['completed_count']}/{r['completed_total']} |" for r in rows if r['case']=='blocking')
    report=f'''# HRRN队列仿真MVP

已实现六种队列策略、完整同节点多GPU分配、固定目标保护、取消/可用性失效处理和独立事件回放。RF模型仅加载和推理，原有冻结文件哈希不变。

所有执行时间均为合成时间单位，不是真实训练或推理时长。以下是机制用例，不是冻结性能测试；不据此声明方法普遍优于HRRN或EASY。

| 策略 | 大作业启动 | 余量作业启动 | 平均等待 | P95等待 | 长等待比例 | 窗口完成/排空完成 |
|---|---:|---:|---:|---:|---:|---:|
{table}

默认统计窗口[0,12]，提交停止后继续排空，等待指标覆盖完整轨迹。评价长等待阈值固定为4，与保护触发阈值0.5分开。取消前未启动的等待显式标为删失，主均值/P95返回null，同时提供已启动样本统计和观察等待下界。

在阻塞用例中，请同时看大作业等待、普通小作业的等待代价和固定窗口完成数；不能仅以大作业更早启动就宣布整体公平和效率同时提升。整卡保护和细粒度保护的余量作业启动时间用于验证资源粒度差异。

summary.csv给出全部策略的指标。每个.json.gz包含逐Job状态、完整事件、GPU账本、保护epoch/目标和资源时间积分。verification.json是独立整数账本回放结果。

logical_reserved_rate=(实际分配给Job的逻辑容量时间+受保护空闲容量时间)/配置容量时间，按显存/核心配额/槽位分别给出。另有logical_allocation_rate和logical_protection_reservation_rate，防止把虚拟保护当成真实GPU利用率。protected_idle_capacity_time的单位分别为MiB×合成时间、核心配额单位×合成时间、槽位×合成时间。

范围：单租户/单队列、同优先级、正核心配额、每张目标GPU相同聚合请求、不同物理GPU、同一节点。多槽位请求由原有GPUState检查并记账；冻结RF接口仍按一次聚合设备请求评分，不将该抽象冒充原生多容器HAMi部署。节点CPU/内存假定充足。设备不可用事件禁止新分配，已有合成作业按原定完成或取消释放，不模拟真实硬件故障恢复。

尚未实现：同vGPU粒度EASY强对照、系统多负载/估计误差/多种子效果实验、跨租户DRF保护额度、多Pod异构请求、零核心原生语义、Volcano集成、PPO/GNN。没有修改线上Kubernetes或部署调度器。
'''
    (out/'report.md').write_text(report,encoding='utf-8')
    return rows


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',choices=tuple(SCENARIOS)+('all',),default='blocking')
    parser.add_argument('--policy',choices=POLICIES+('all',),default='all')
    parser.add_argument('--seed',type=int,default=20261023)
    parser.add_argument('--bundle',type=Path,default=DEFAULT_BUNDLE)
    parser.add_argument('--trace',type=Path,help='Replay a previously saved synthetic input JSON')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    cases=['replay'] if args.trace else list(SCENARIOS) if args.case=='all' else [args.case]
    policies=POLICIES if args.policy=='all' else (args.policy,)
    run_suite(args.output,cases,args.seed,policies,args.bundle,args.trace)

if __name__=='__main__':main()
