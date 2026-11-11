"""Recheck live two-vGPU receipts; emit compact CSV and Chinese report."""
import argparse
import csv
import hashlib
import json
from pathlib import Path


def analyze(root):
    run=root/'rf-multi-vgpu-20261028'
    out=run/'execution'
    read=lambda p:json.loads(p.read_text(encoding='utf-8'))
    state=read(out/'state.json');restore=read(out/'restore-confirmation.json')
    assert state['experiment_success'] and state['restored']
    assert restore['original_scheduler_available'] and restore['rf_replicas']==0
    assert restore['own_pods_remaining']==0 and restore['logical_ledger_restored']
    assert restore['quota_config_dqn_and_model_binary_unchanged']
    rows=[]
    for task in ['training','inference']:
        proof=read(out/(task+'-uuid-proof.json'))
        ids=proof['allocate_uuids']
        assert len(ids)==len(set(ids))==2
        assert ids==proof['rf_uuids']==proof['plugin_uuids']
        assert proof['node']=='t3dgq' and proof['phase']=='Running' and not proof['cuda_executed']
        assert not proof['native_receipt']['fallback_used']
        before=read(out/(task+'-before-ledger.json'))['devices']
        after=read(out/(task+'-after-ledger.json'))['devices']
        released=read(out/(task+'-released-ledger.json'))['devices']
        assert before==released
        byid={g['uuid']:g for g in before}
        classifications={g['gpu_uuid']:g for g in proof['rf_advice'].get('classifications',[])}
        for g in after:
            old=byid[g['uuid']]
            delta=tuple(g[k]-old[k] for k in ['allocated_memory_mib','allocated_compute_quota','allocated_vgpu_count'])
            assert delta==((4096,20,1) if g['uuid'] in ids else (0,0,0))
            if g['uuid'] in ids:
                assert min(g['free_memory_mib'],g['free_compute_quota'],g['free_instances'])>=0
                rows.append(dict(task_type=task,pod_uid=proof['pod_uid'],gpu_uuid=g['uuid'],
                    rf_allocate_plugin_equal=True,free_memory_before_mib=old['free_memory_mib'],
                    free_memory_after_mib=g['free_memory_mib'],free_compute_before=old['free_compute_quota'],
                    free_compute_after=g['free_compute_quota'],instances_before=old['allocated_vgpu_count'],
                    instances_after=g['allocated_vgpu_count'],release_restored=True,
                    classification=classifications.get(g['uuid'],{}).get('classification',{}).get('label')))
    with (root/'allocations.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    summary=dict(real_kubernetes=True,sleep_only=True,pods=2,vgpus_per_pod=2,device_allocations=4,
        successful_pods=2,uuid_consistent_device_allocations=4,ledger_correct_device_allocations=4,
        releases_verified=2,observed_overallocations=0,restored=restore,rows=rows)
    (root/'verification.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    text='''# 单 Pod 多 vGPU 真实集群验收报告

2026-10-28，t3dgq，4 × RTX 4090。已完成两个真实 Kubernetes Pod 的验收：training 与 inference 各一个，顺序提交、分别释放。每个 Pod 仅一个 sleep 容器，请求 2 个 vGPU，每个设备 4096 MiB / 20 算力配额。全程执行 /bin/sleep 600。

使用既有独立 volcano-rf-test、冻结 RF 模型及原生 Allocate/SubResource。原 DQN scheduler 临时缩容为 0，待它退出且无在途分配后启动 RF，结束后恢复。ResourceQuota 无需修改。

## 实际 UUID 与账本

|任务|Pod UID|GPU UUID|显存剩余 MiB（前→后）|算力剩余（前→后）|实例（前→后）|
|---|---|---|---|---|---|
'''
    for r in rows:
        text+=f"|{r['task_type']}|{r['pod_uid']}|{r['gpu_uuid']}|{r['free_memory_before_mib']}→{r['free_memory_after_mib']}|{r['free_compute_before']}→{r['free_compute_after']}|{r['instances_before']}→{r['instances_after']}|\n"
    text+='''
两次分配均为同一节点的两张不同物理 GPU。两种任务选中相同设备，如实保留结果，不以此声称训练/推理设备选择必然不同。

## 证据与验收

- 2/2 Pod 合法绑定并启动，4/4 设备分配的 RF 推荐 UUID、Volcano 永久分配 UUID、Device Plugin 消费 UUID 一致。
- 插件 UUID 来自匹配 Pod UID、sleep 容器和 volcano.sh/vgpu-number 的 kubelet checkpoint AllocateResponse.NVIDIA_VISIBLE_DEVICES；没有用 Running 状态替代 UUID 证据。
- 原生提交日志 logicalBefore / logicalAfter 与 API 活跃 Pod 注解重建账本逐卡核对一致。两类账本来源分别保存在 uuid-proof 和 ledger 文件中。
- 每张选中 GPU 增加 4096 MiB / 20 算力 / 1 实例，其他 GPU 不变；每个 Pod 总计跨两卡预留 8192 MiB / 40 算力。观察到超分配 0 次。
- 两次删除均核对原生 logical release 日志，释放后全部四张卡的逻辑状态恢复，与分配前完整对比一致。
- 两个 Pod 与两个 PodGroup 已清理，实验 quota used 为 0，NodeLock 为空。原 DQN scheduler 恢复 1/1 可用，RF 恢复 0 副本。
- 原 scheduler / RF Deployment spec、两份 ConfigMap data、ResourceQuota spec、DQN 服务 spec、冻结模型及原 scheduler 二进制哈希均与实验前一致。

本次确认了真实单 Pod 双 vGPU 设备分配与释放链路。没有执行 CUDA；不评价多卡计算通信或训练/推理速度。

## 复核命令

```powershell
.\\.venv\\Scripts\\python.exe scripts/analyze_multi_vgpu_live.py results/multi-vgpu-live-20261028
```

`rf-multi-vgpu-20261028/training.yaml` 与 `inference.yaml` 是实际提交文件。`execution/*-uuid-proof.json` 保存三方 UUID、完整 RF 输入输出与原生提交；`*-checkpoint.json` 保存插件回执；`*-released-ledger.json` 和 `restore-confirmation.json` 保存释放与恢复证据。
'''
    (root/'REPORT.md').write_text(text,encoding='utf-8')
    print(json.dumps({k:v for k,v in summary.items() if k!='rows'},ensure_ascii=False,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('root',type=Path);analyze(p.parse_args().root)
