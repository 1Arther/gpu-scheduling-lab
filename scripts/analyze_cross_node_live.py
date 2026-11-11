"""Verify cross-node real scheduling evidence and write CSV/report."""
import argparse
import csv
import json
from pathlib import Path

def analyze(root):
    run=root/'rf-cross-node-20261028';out=run/'execution'
    load=lambda p:json.loads(p.read_text(encoding='utf-8'))
    s=load(out/'state.json');r=load(out/'restore-confirmation.json');extra=load(out/'cross-restoration.json')
    assert s['experiment_success'] and s['restored'] and all(extra.values())
    rows=[];scores=[]
    for case in ['training','inference','5090-path','l40-path']:
        p=load(out/(case+'-uuid-proof.json'))
        assert p['rf_uuids']==p['allocate_uuids']==p['plugin_uuids'] and len(p['allocate_uuids'])==1
        assert not p['native_receipt']['fallback_used'] and p['phase']=='Running' and not p['cuda_executed']
        before=load(out/(case+'-before-ledger.json'))['devices'];after=load(out/(case+'-after-ledger.json'))['devices']
        assert before==load(out/(case+'-released-ledger.json'))['devices']
        target=p['allocate_uuids'][0];old={g['uuid']:g for g in before}
        for g in after:
            delta=tuple(g[k]-old[g['uuid']][k] for k in ['allocated_memory_mib','allocated_compute_quota','allocated_vgpu_count'])
            assert delta==((4096,20,1) if g['uuid']==target else (0,0,0))
        unique={}
        for a in p['all_node_advices']:
            nodes=tuple(sorted({g['node'] for g in a['gpus']}))
            unique[nodes]=a
        for nodes,a in unique.items():
            for c in a['classifications']:
                cl=c['classification'];sc=c['request_specific_scores'][p['task_type']]
                decomposition=sc['score_decomposition']
                scores.append(dict(case=case,node=';'.join(nodes),gpu_uuid=c['gpu_uuid'],node_score=a['node_score'],
                    classification=cl['label'],prediction_status=cl['prediction_status'],ranking_basis=sc['ranking_basis'],
                    rf_contribution=decomposition['applied_rf_contribution'],heuristic_contribution=decomposition['heuristic_contribution'],
                    fallback_contribution=decomposition['conservative_fallback_contribution'],total_score=sc['score'],detail_json=json.dumps(c,ensure_ascii=False)))
        if case in ['training','inference']:
            assert {n for ns in unique for n in ns}=={'t3dgq','t3dmg','t3dgq-2'},'Missing node RF evidence'
        g=next(g for g in after if g['uuid']==target)
        rows.append(dict(case=case,pod_uid=p['pod_uid'],task_type=p['task_type'],node=p['node'],gpu_uuid=target,
            constraint=p['node_constraint'],uuid_three_way_equal=True,free_memory_before_mib=old[target]['free_memory_mib'],free_memory_after_mib=g['free_memory_mib'],
            free_compute_before=old[target]['free_compute_quota'],free_compute_after=g['free_compute_quota'],released=True))
    for filename,data in [('allocations.csv',rows),('candidate-scores.csv',scores)]:
        with (root/filename).open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(data[0]));w.writeheader();w.writerows(data)
    summary=dict(real_kubernetes=True,pods=len(rows),uuid_consistency=len(rows),ledger_correct=len(rows),releases_correct=len(rows),
        observed_overallocation=0,restoration=r,rbac_and_rf_selector_restoration=extra,allocations=rows)
    (root/'verification.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    text='''# RF 跨节点真实 Pod 选择验收

2026-10-28。候选节点 t3dgq（4090×4）、t3dmg（5090×4）、t3dgq-2（L40×3），共 11 张物理 GPU。全部容器只运行 /bin/sleep 600，请求 1 vGPU、4096 MiB、20 算力配额。

training / inference 两个案例在同等请求下允许三节点参与，未指定最终节点。5090-path / l40-path 另以节点亲和性验证对应节点的容器和设备插件链路；后两项不作为 RF 自主节点优选证据。

|案例|Pod UID|最终节点|GPU UUID|三方 UUID|释放|
|---|---|---|---|---|---|
'''
    for row in rows:text+=f"|{row['case']}|{row['pod_uid']}|{row['node']}|{row['gpu_uuid']}|一致|恢复|\n"
    text+='\n## 自由选择时的节点评分\n\n|案例|节点|分类|预测状态|RF项|启发式项|总分|\n|---|---|---|---|---:|---:|---:|\n'
    seen=set()
    for row in scores:
        key=(row['case'],row['node'])
        if row['case'] not in ['training','inference'] or key in seen:continue
        seen.add(key)
        text+=f"|{row['case']}|{row['node']}|{row['classification']}|{row['prediction_status']}|{row['rf_contribution']:.6f}|{row['heuristic_contribution']:.6f}|{row['total_score']:.6f}|\n"
    text+='''
## 验收结果

4/4 真实 Pod 绑定并启动，RF 推荐、Volcano Allocate 永久注解和实际目标节点 kubelet AllocateResponse.NVIDIA_VISIBLE_DEVICES 一致。每次只在选中 GPU 扣减 4096 MiB / 20 算力 / 1 实例，其他十张卡不变；原生提交日志与 API 注解重建账本独立核对，删除后全部恢复。

两个自由选择案例保存了三节点实际 RF 请求/响应和评分，见 `candidate-scores.csv` 与 `*-uuid-proof.json` 的 `all_node_advices`。本次三个节点资源充足时，RF 均返回 balanced / provisional，学习项贡献均为 0.3，没有触发 pending_verification 回退。因此本次节点排序的差别来自硬件与资源启发式项；它证明了三节点候选评分与真实分配链路，不单独证明 RF 学习项改变了节点选择。

## 恢复与范围

原 DQN scheduler 临时停止，待其退出且账本、NodeLock 清空后启动独立 RF。仅临时将 RF 的节点候选范围及 ClusterRole 节点 update/patch resourceNames 扩大到三个节点。全部自己的 Pod/PodGroup 清理后，RF 恢复 0，原 scheduler 恢复 1/1；RF Deployment 原 spec、原节点权限、原配置、模型、二进制及配额恢复/核对一致。没有修改驱动、Device Plugin、运行时或原 DQN 配置，也没有终止宿主机进程。

测试使用三个节点已缓存的 Device Plugin 镜像作为普通 sleep 镜像：显式覆盖其入口为 /bin/sleep，不启动插件进程，不挂载插件管理目录，不授予特权。原有插件实例不受此测试容器管理。

该实验验证 Pod 可被选择到不同节点，并完成设备级逻辑配额分配。单个 Pod 仍只运行在一个节点；不涉及跨节点合并设备、CUDA 计算或训练/推理吞吐量。

复核：`python scripts/analyze_cross_node_live.py results/cross-node-live-20261028`。
'''
    (root/'REPORT.md').write_text(text,encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('root',type=Path);analyze(p.parse_args().root)
