"""Summarize all fixed native Go trials without selecting only changed decisions."""
import argparse
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path


def csv_write(path, rows):
    if not rows:return
    keys = list(dict.fromkeys(k for row in rows for k in row))
    with path.open('w',newline='',encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f,fieldnames=keys)
        writer.writeheader();writer.writerows(rows)


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('results',type=Path);a=ap.parse_args();out=a.results
    raw=json.loads((out/'native-ablation.json').read_text(encoding='utf-8'))
    execution=json.loads((out/'execution.json').read_text(encoding='utf-8'))
    if raw['runtime_os']!='linux' or not execution.get('success'):
        raise SystemExit('Cannot report complete verification without successful Linux execution')
    groups=defaultdict(dict);rows=[]
    for trial in raw['trials']:
        groups[trial['case_id']][trial['strategy']]=trial
        before={g['uuid']:g for g in trial['before']}
        for c in trial['candidates']:
            cls=c['classification'];rf=cls.get('raw_rf') or {};g=before[c['gpu_uuid']]
            rows.append({'case_id':trial['case_id'],'family':trial['family'],'task':trial['task'],'strategy':trial['strategy'],
                         'gpu_uuid':c['gpu_uuid'],'node':c['node'],'model':c['model'],
                         'free_memory_mib':g['registered_memory_mib']-g['allocated_memory_mib'],
                         'free_compute_quota':100-g['allocated_compute_quota'],
                         'allocated_vgpu_count':g['allocated_vgpu_count'],
                         'raw_rf_label':rf.get('label'),'raw_rf_training':rf.get('training'),'raw_rf_inference':rf.get('inference'),
                         'training_vote_fraction':rf.get('training_vote_fraction'),'inference_vote_fraction':rf.get('inference_vote_fraction'),
                         'training_adapted':cls.get('training_adapted'),'inference_adapted':cls.get('inference_adapted'),
                         'effective_label':cls.get('label'),'prediction_status':cls.get('prediction_status'),
                         'pending_verification':cls.get('prediction_status')=='pending_verification',
                         'reference_training_hard_fit':cls.get('reference_hard_constraints',{}).get('training'),
                         'reference_inference_hard_fit':cls.get('reference_hard_constraints',{}).get('inference'),
                         'rf_contribution':c['rf_contribution'],'potential_rf_contribution':c['potential_rf_contribution'],
                         'heuristic_contribution':c['heuristic_contribution'],'conservative_fallback_contribution':c['conservative_fallback_contribution'],
                         'final_score':c['final_score'],'node_score':c['node_score'],'rounded_node_score':c['rounded_node_score'],
                         'node_rank':c['node_rank'],'device_rank':c['device_rank'],'global_rank':c['global_rank'],
                         'native_hard_fit':c['native_hard_fit'],'classification_used_by_strategy':c['classification_used_by_strategy'],
                         'final_allocate_uuid':trial['allocate_uuid'],'selected':trial['allocate_uuid']==c['gpu_uuid'],
                         'reasons':json.dumps(cls.get('reasons',[]),ensure_ascii=False)})
    csv_write(out/'candidate-scores.csv',rows)
    summaries=[];strong=[];pending_rows=[]
    for case,modes in groups.items():
        on,off,base=(modes[k] for k in ('rf_on','rf_off','binpack'))
        assert len({m['pod_hash'] for m in modes.values()})==1
        assert len({m['snapshot_hash'] for m in modes.values()})==1
        assert all(m['snapshot_hash']==m['restored_hash'] for m in modes.values())
        assert all(c['native_hard_fit'] for m in modes.values() for c in m['candidates'])
        changed=on['allocate_uuid']!=off['allocate_uuid'];task=on['task']
        by_uuid={c['gpu_uuid']:c for c in on['candidates']}
        pure=changed and all(by_uuid[u]['classification']['prediction_status']=='provisional' and
                            by_uuid[u]['classification'].get('reference_hard_constraints',{}).get(task) is True
                            for u in (on['allocate_uuid'],off['allocate_uuid']))
        row={'case_id':case,'family':on['family'],'task':task,'binpack_uuid':base['allocate_uuid'],
             'rf_off_uuid':off['allocate_uuid'],'rf_on_uuid':on['allocate_uuid'],
             'new_strategy_vs_binpack_changed':on['allocate_uuid']!=base['allocate_uuid'],
             'heuristic_vs_binpack_changed':off['allocate_uuid']!=base['allocate_uuid'],
             'rf_independent_uuid_changed':changed,'same_node_rf_uuid_changed':changed and on['allocate_node']==off['allocate_node'],
             'both_selected_devices_reference_hard_fit_and_provisional':pure,
             'all_candidates_native_hard_fit':True,'ledger_restored_all_modes':True,
             'pod_hash':on['pod_hash'],'snapshot_hash':on['snapshot_hash']}
        summaries.append(row)
        if pure:strong.append(row)
        off_by={c['gpu_uuid']:c for c in off['candidates']}
        for uuid,c in by_uuid.items():
            b=off_by[uuid]
            assert abs(c['heuristic_contribution']-b['heuristic_contribution'])<1e-9
            assert abs(c['final_score']-b['final_score']-c['rf_contribution'])<1e-9
            if c['classification']['prediction_status']=='pending_verification':
                assert c['rf_contribution']==b['rf_contribution']==0
                assert c['final_score']==b['final_score']
                pending_rows.append((case,uuid))
    csv_write(out/'decisions.csv',summaries)
    family=[]
    for name in sorted({r['family'] for r in summaries}):
        subset=[r for r in summaries if r['family']==name]
        family.append({'family':name,'cases':len(subset),
                       **{key:sum(r[key] for r in subset) for key in ('new_strategy_vs_binpack_changed','heuristic_vs_binpack_changed','rf_independent_uuid_changed','same_node_rf_uuid_changed','both_selected_devices_reference_hard_fit_and_provisional')}})
    csv_write(out/'summary-by-family.csv',family)
    pending_control=json.loads((out/'pending-control.json').read_text(encoding='utf-8'))
    controls=[]
    for trial in pending_control['trials']:
        before={g['uuid']:g for g in trial['before']}
        for c in trial['candidates']:
            cls=c['classification'];rf=cls['raw_rf'] or {};g=before[c['gpu_uuid']]
            controls.append({'case_id':trial['case_id'],'task':trial['task'],'strategy':trial['strategy'],
                             'gpu_uuid':c['gpu_uuid'],'raw_rf_label':rf.get('label'),
                             'raw_rf_training':rf.get('training'),'raw_rf_inference':rf.get('inference'),
                             'training_adapted':cls['training_adapted'],'inference_adapted':cls['inference_adapted'],
                             'prediction_status':cls['prediction_status'],'pending_verification':True,
                             'rf_contribution':c['rf_contribution'],'heuristic_contribution':c['heuristic_contribution'],
                             'conservative_fallback_contribution':c['conservative_fallback_contribution'],
                             'final_score':c['final_score'],'device_rank':c['device_rank'],
                             'free_memory_mib':g['registered_memory_mib']-g['allocated_memory_mib'],
                             'free_compute_quota':100-g['allocated_compute_quota'],
                             'native_hard_fit':c['native_hard_fit'],'final_allocate_uuid':trial['allocate_uuid'],
                             'reasons':json.dumps(cls['reasons'],ensure_ascii=False)})
    csv_write(out/'pending-control-scores.csv',controls)
    tests=Counter();packages={}
    for line in (out/'go-tests.log').read_text(errors='replace').splitlines():
        try:event=json.loads(line)
        except ValueError:continue
        if event.get('Test') and event['Action'] in ('pass','fail','skip'):tests[event['Action']]+=1
        if not event.get('Test') and event['Action'] in ('pass','fail','skip'):packages[event['Package']]=event['Action']
    summary={'cases':len(groups),'trials':len(raw['trials']),'candidate_rows':len(rows),'families':family,
             'rf_changed_uuid':sum(r['rf_independent_uuid_changed'] for r in summaries),
             'native_strategy_changed_uuid':sum(r['new_strategy_vs_binpack_changed'] for r in summaries),
             'strong_witnesses':strong,'pending_candidate_pairs_unchanged':len(pending_rows),
             'explicit_pending_control_candidate_pairs':len(controls)//2,
             'go_tests_including_subtests':dict(tests),'packages':packages,
             'logical_ledger_restored':True,'real_cluster_deployment':False,'performance_benefit_claimed':False}
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# RandomForest 预测项独立贡献：原生 Volcano 离线消融验证', '',
           f"执行位置：`{execution['hostname']}`，`{execution['root']}`；{execution['go_version']}。",
           '真实 Linux 进程执行 Go 测试，真实常驻 Python/joblib 推理，真实 Volcano FilterNode / ScoreNode / Allocate / SubResource；Kubernetes 使用 client-go fake，未部署线上调度器。','',
           f"冻结计划 SHA256：`{raw['plan_sha256']}`；模型：`{execution['model_version']}`；模型与评分策略文件校验前后不变。",'',
           '## 消融合同','',
           '`S_on = H + R`；`S_off = H`。仅去掉暂定可用设备的学习项 R；不改权重、不重新归一化、不改标签和模型。',
           'H 包括原有硬件/共享、装箱、资源平衡和待验证设备的确定性保守代理项。RF off 仍执行模型以记录完全相同的诊断结果，但 RF 贡献为零。',
           '所有主实验候选均通过具体 Pod 的原生逻辑显存、算力、实例及共享约束；参考负载适配性约束与具体 Pod 准入分开记录。',
           '节点按 deviceshare 的四舍五入分数选择，并用固定节点名破平；节点内 UUID 按 RF 设备排序。此测试调用真实 GPUDevices 方法，未运行完整集群调度循环。','',
           '## 全量结果','',
           '| 场景 | 组数 | 新策略相对 Binpack 改 UUID | 仅移除 RF 后改 UUID | 同节点物理 UUID 改变 | 两候选参考硬条件均满足的 RF 证据 |',
           '|---|---:|---:|---:|---:|---:|']
    for r in family:
        lines.append(f"| {r['family']} | {r['cases']} | {r['new_strategy_vs_binpack_changed']} | {r['rf_independent_uuid_changed']} | {r['same_node_rf_uuid_changed']} | {r['both_selected_devices_reference_hard_fit_and_provisional']} |")
    lines += ['',f"总计 {len(groups)} 组配对输入，{len(raw['trials'])} 次原生分配/释放，{len(rows)} 条逐候选记录。RF 独立改变 UUID 的组数：{summary['rf_changed_uuid']}。",
              '相对 Binpack 的变化同时含启发式、排序和 RF，不能全部归功于 RF；只有同输入 RF on/off 的变化用于归因。','',
              '## 直接证据','']
    if strong:
        witnesses=[strong[0]]
        within=next((r for r in strong if r['same_node_rf_uuid_changed']),None)
        if within and within != strong[0]:witnesses.append(within)
        for witness in witnesses:
            case=witness['case_id']
            lines += [f"固定用例 `{case}`，任务 `{witness['task']}`。两种策略选中的 GPU 都有相关验证覆盖，且目标参考负载硬条件均满足。",'',
                  '| GPU UUID | 模式 | 原始 RF 类别 | 训练/推理判断 | 状态 | RF 贡献 | 启发式贡献 | 总分 | Allocate UUID |','|---|---|---|---|---|---:|---:|---:|---|']
            for r in rows:
                if r['case_id']==case and r['strategy']!='binpack' and r['gpu_uuid'] in (witness['rf_on_uuid'],witness['rf_off_uuid']):
                    lines.append(f"| {r['gpu_uuid']} | {r['strategy']} | {r['raw_rf_label']} | {r['training_adapted']}/{r['inference_adapted']} | {r['prediction_status']} | {r['rf_contribution']:.6f} | {r['heuristic_contribution']:.6f} | {r['final_score']:.6f} | {r['final_allocate_uuid']} |")
            lines.append('')
        lines += ['以上自动展示全量中首个跨节点和首个同节点合格证据；未修改输入、模型或权重。所有其他用例也保留。',
                  '分类会通过对应任务适配布尔值决定 RF 项是否为零，通过投票比例决定非零项大小；综合四类字符串本身不是唯一调度依据。']
    else:
        lines += ['当前固定用例没有满足“两候选参考硬条件均通过”的直接证据，不据此声称已证明该条件下 RF 独立影响。']
    lines += ['', '## 回退、过滤与动态账本','',
              f"原冻结主实验实际待验证候选共 {len(pending_rows)} 对。计划中 pending_only / mixed_pending 只是预先命名的场景组，不是模型输出；本轮完整输入的 5090 实际为 provisional，不能声称这些输入触发了回退。",
              f"因此额外增加独立 pending-control：5090 每卡已有一个缺少 workload-type Label 的 Pod（4096 MiB / 20 配额），或一个已标 training 但 compute quota=0（表示不限制算力）的 Pod。两种原因下分别再提交 training/inference 的 4096 MiB / 25 配额 Pod。{len(controls)//2} 对候选全部满足硬约束，真实触发 pending_verification。未知任务组成时原始 RF 可以为 null（拒绝不完整模型状态）；不限算力时保留原始 RF 诊断但不用于排序。开关两侧 RF 贡献均为零、H 和总分完全相同、最终 UUID 相同。该对照不计入180组，未改变模型、门槛或权重。详见 pending-control.json / pending-control-scores.csv。",
              '补充测试初版错误地要求所有待验证输入都有原始 RF，因任务组成未知的输入会拒绝推断而失败；已修正测试预期，并增加不限算力场景检查“原始 RF 存在但不参与排序”。失败运行与修正后的运行均保留，未为通过测试更改模型行为。',
              '混合场景中，待验证设备自身的 RF 项为零，其他已验证设备的 RF 项仍可能改变整体选择；这不等于待验证设备的原始分类参与了排序。',
              '独立 filter-control.json 记录不满足具体 Pod 准入条件的设备，RF 开关均不能使其被选中；该过滤对照不计入主消融收益。',
              'sequential-ledger.json 记录 A 分配、B 基于新账本分配、释放 A、C 重新预测、全部释放。每次比较从相同当前账本分叉，避免前一个策略的分配轨迹差异混入当前消融。',
              '所有配对分配均从 Allocate 最终写入的 devices-to-allocate 注解读取 UUID，并检查对应设备增量、其他设备不变以及 SubResource 后快照哈希恢复。','',
              '## 验证边界','',
              f"Go 包结果：`{json.dumps(packages,ensure_ascii=False)}`；测试（含子测试）：`{dict(tests)}`。Python 结果见 pytest.log / pytest.xml。",
              '只有逻辑资源快照与合成理论标签；未验证真实训练吞吐、推理延迟、CUDA 成功率或物理显存。RF 投票比例不是预测可靠性。',
              '模型被调用不等于模型改变了决策；贡献相同、分差不足、节点评分取整后相同、所有候选待验证、或硬约束只剩唯一候选时，UUID 可以不变。',
              '真实 Kubernetes 集群调度成功：本轮未执行，不能由离线原生分配推导。','',
              '复现：在独立 Linux 目录安装 requirements.txt 的固定 CPU 依赖，执行 `python scripts/run_rf_ablation_linux.py --go /usr/bin/go --output results/linux-verification-new`，然后 `python scripts/report_rf_ablation.py results/linux-verification-new`。']
    (out/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps({k:summary[k] for k in ('cases','trials','rf_changed_uuid','native_strategy_changed_uuid','go_tests_including_subtests','packages')},indent=2))


if __name__=='__main__':
    main()
