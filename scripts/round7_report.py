"""Join real read-only records; separate annotation reconstruction from NVML."""
import argparse
from collections import Counter
import csv
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from gpu_lab.staging import inventory,physical_rows,fragmentation,logical_rates
from gpu_lab.runtime.service import SuitabilityService
from gpu_lab.runtime.volcano import VolcanoRFAdapter


def records(path):
    good=[];bad=0
    for line in path.read_text(encoding='utf-8').splitlines():
        try:good.append(json.loads(line))
        except ValueError:bad+=1
    return good,bad


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--results',type=Path,default=ROOT/'results/round7-preflight-20261019');a=ap.parse_args();out=a.results
    sources={};manifest=[]
    for path in sorted((out/'raw').glob('*.jsonl')):
        rows,bad=records(path);sources[path.stem]={r['id']:r for r in rows}
        manifest.append({'file':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'valid_records':len(rows),'invalid_or_partial_lines':bad,
                         'errors':[{'id':r['id'],'error':r.get('error',r.get('stderr'))} for r in rows if 'error' in r or r.get('exit_code',0) not in (0,None)]})
    management=sources.get('4090-refresh',{})
    if not all(k in management for k in ('nodes','pods','controllers')):management=sources['4090-final']
    nodes=management['nodes']['data'];pods=management['pods']['data']
    gpus,warnings=inventory(nodes,pods);target=[g for g in gpus if g['node'] in ('t3dgq','t3dmg','t3dgq-2')]
    physical={};processes={};owners={};checkpoint_info=[]
    for key in ('4090-final','5090','l40'):
        source=sources.get(key.replace('-final','')+'-refresh',{})
        if not all(k in source for k in ('gpu_query','gpu_processes')):source=sources[key]
        for g in physical_rows(source['gpu_query']['stdout']):physical[g['uuid']]={**g,'physical_observed_utc':source['gpu_query']['started_utc']}
        for row in csv.reader(source['gpu_processes']['stdout'].splitlines()):
            if len(row)==4:
                uuid,pid,name,mem=(s.strip() for s in row)
                owner=source.get('process_owner:'+pid,{}).get('stdout','').split()
                cg=source.get('process_cgroup:'+pid,{}).get('stdout','').strip()
                processes.setdefault(uuid,[]).append({'pid':int(pid),'process':name,'memory_mib':mem,'owner':owner[1] if len(owner)>1 else 'unknown','cgroup':cg})
    uid_to_pod={p['metadata']['uid']:p for p in pods}
    for node,key in [('t3dgq','4090-final'),('t3dmg','5090-supplement'),('t3dgq-2','l40-supplement')]:
        refreshed=sources.get({'t3dgq':'4090-refresh','t3dmg':'5090-refresh','t3dgq-2':'l40-refresh'}[node],{})
        cp=refreshed.get('kubelet_checkpoint',{}).get('data') or sources[key].get('kubelet_checkpoint',{}).get('data',{})
        for e in cp.get('entries',[]):
            p=uid_to_pod.get(e['PodUID'])
            checkpoint_info.append({**e,'node':node,'matching_api_pod':p['metadata']['name'] if p else None,
                'matching_api_phase':p['status'].get('phase') if p else None,'source':'direct kubelet checkpoint; not scheduler quota memory'})
    service=SuitabilityService();adapter=VolcanoRFAdapter(service);combined=[];classifications=[]
    for g in target:
        snap=adapter.snapshot({k:v for k,v in g.items() if k not in ('pods','source','node_ready','free_memory_mib','free_compute_quota','free_instances')})
        cls=service.classify_gpu(snap)
        classifications.append({'gpu_uuid':g['uuid'],'input_source':g['source'],'classification':cls,'not_a_live_scheduler_call':True})
        row={**g,**physical[g['uuid']],'rf_category':cls['label'],'rf_prediction_status':cls['prediction_status'],
             'host_process_count':len(processes.get(g['uuid'],[])),'processes':processes.get(g['uuid'],[]),
             'safe_exclusive_test_device_proven':False}
        combined.append(row)
    save=lambda name,value:(out/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    save('logical-physical-inventory.json',{'gpus':combined,'warnings':warnings,'logical_source':'active API Pod annotation reconstruction, not direct scheduler memory',
        'logical_snapshot_utc':management['pods']['started_utc'],'registered_but_excluded_nodes':['r5300'],
        'isolation_proven':False,'deployment_allowed':False})
    save('read-only-snapshot-classification.json',{'model_version':service.version,'supervision':'synthetic','classifications':classifications})
    save('kubelet-checkpoint-correlation.json',checkpoint_info);save('raw-manifest.json',manifest)
    columns=['node','uuid','model','registered_memory_mib','allocated_memory_mib','free_memory_mib','allocated_compute_quota','free_compute_quota',
             'allocated_vgpu_count','free_instances','physical_total_mib','physical_used_mib','physical_free_mib','physical_utilization_pct',
             'host_process_count','rf_category','rf_prediction_status','source','physical_observed_utc','safe_exclusive_test_device_proven']
    with (out/'gpu-inventory.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=columns,extrasaction='ignore');w.writeheader();w.writerows(combined)
    metrics={'status':'not_executed_stage2_not_authorized','new_real_test_pods':0,'legal_allocation_success_rate':None,
             'rf_allocate_plugin_uuid_consistency_rate':None,'logical_allocation_correctness_rate':None,'overallocation_count':None,
             'release_correctness_rate':None,'dynamic_reclassification_count':None,'rf_on_off_real_uuid_difference_count':None,
             'fallback_success_rate':None,'reason':'No real experiments; no zero-valued success/error claims fabricated.',
             'read_only_annotation_snapshot_rates':logical_rates(target),'read_only_reference_fragmentation':fragmentation(target)}
    save('phase2-metrics-NOT-EXECUTED.json',metrics)
    gate_path=ROOT/'k8s/round7/isolation-gate.json';gate=json.loads(gate_path.read_text(encoding='utf-8'))
    sleep=sources.get('4090-sleep-verified',{})
    sleep_verified=all(sleep.get(k,{}).get('exit_code')==0 for k in ('cached_sleep_ELF','cached_sleep_file'))
    gate['sleep_command_file_verified_on_4090']=sleep_verified
    gate_path.write_text(json.dumps(gate,indent=2),encoding='utf-8')
    start=dt.datetime.fromisoformat(management['meta']['utc']).astimezone(dt.timezone(dt.timedelta(hours=8),'Asia/Shanghai')).isoformat()
    lines=['# 第七轮阶段一：真实集群只读检查与部署准备报告','',
           '**结论：NO-GO。当前没有已证明可安全独占的测试 GPU；不得进入真实部署或提交 Pod。阶段一没有创建、修改或删除集群资源。**','',
           f'集群快照开始时间（北京时间）：{start}。每条物理采样另有 UTC 时间，阶段二前必须重新采样。',
           f"Kubernetes：{management['kubernetes_version']['data']['serverVersion']['gitVersion']}；{len(nodes)} 个注册节点，{sum(any(c['type']=='Ready' and c['status']=='True' for c in n['status']['conditions']) for n in nodes)} 个 Ready。r5300/A40 仍不可用，排除；研究范围为 4090×4、5090×4、L40×3。",'',
           '## 当前 GPU 清单','',
           '逻辑“已分配/剩余”由 API 活跃 Pod 的永久 UUID 注解重建，不是直接读在线 DQN scheduler 内存。终态 Pod 排除；删除中的非终态 Pod继续计入。nvidia-smi 的物理数值单列，未输入 RF。','',
           '| 节点/索引 | GPU UUID | 注册 MiB | 重建已分配 MiB/算力/实例 | 物理已用/空闲 MiB | 实际利用率 | GPU 进程数 |','|---|---|---:|---|---|---:|---:|']
    for g in sorted(combined,key=lambda r:(r['node'],r['index'])):
        lines.append(f"| {g['node']}/{g['index']} | {g['uuid']} | {g['registered_memory_mib']} | {g['allocated_memory_mib']}/{g['allocated_compute_quota']}/{g['allocated_vgpu_count']} | {g['physical_used_mib']}/{g['physical_free_mib']} | {g['physical_utilization_pct']}% | {g['host_process_count']} |")
    lines += ['', 'NVML 还有驱动保留容量，total 不必等于 used+free；表中 free 是直接查询值。逻辑资源看似空闲，并不能证明测试设备无人使用。','',
              '4090 四卡有 root 的 hashcat.bin 用户会话进程；5090 四卡有 sxzhou 的 VLLM worker 用户会话进程；L40 有 infonet/rlj 的推理进程、MPS 及 root Docker 进程。这里只读记录 PID/用户名/cgroup，未停止或调整任何进程。',
              f"按当前 API 注解重建，目标设备活跃 vGPU 分配为 {sum(g['allocated_vgpu_count'] for g in target)}；重建警告 {len(warnings)} 条。这不能排除调度器的未绑定预留或非 Kubernetes GPU 访问。",'',
              '## 调度器、插件与独立构建','']
    for p in management['controllers']['data']:
        name=p['metadata']['name']
        if name in ('volcano-scheduler','volcano-device-plugin','nvidia-device-plugin-daemonset','dqn-scheduler-grpc') or 'nvidia-device' in name:
            lines.append(f"- {p['kind']} `{p['metadata']['namespace']}/{name}`：镜像 `{', '.join(c['image'] for c in p['spec']['containers'])}`；状态 `{json.dumps(p['status'],ensure_ascii=False)}`。")
    lines += ['', '线上 scheduler Pod 虽标记镜像 v1.13.1，实际挂载 `/home/zbs/volcano-v1.13.1-dqn/_output/bin/vc-scheduler`。配置仍为 `deviceshare.GPUSelectPolicy: dqn-job`；部署参数未设置 `--node-selector`，不能把 Deployment 的宿主机 nodeSelector=t3dgq 误解成它只调度 t3dgq。',
              '4090 同时注册普通 nvidia.com/gpu 与 volcano.sh/vgpu-*，kubelet 检查点也列出了同一批 GPU UUID。两套资源名代表重叠设备，不能加总容量或分别预留。',
              '补充只读探测：在线scheduler的8080/metrics连接被拒绝，没有获取到其内部逻辑账本直接指标；因此本报告不把API注解重建值冒充直接观测。原DQN ConfigMap resourceVersion前后仍为21788675；两实验Namespace查询均为NotFound。原二进制只读SHA256为9f2e1ae3359ce234fd048bc48aab082add99a686631808908b5ae518bdebf4f4，未覆盖或执行它。',
              f"检查点共有 {len(checkpoint_info)} 条资源记录，其中 {sum(r['matching_api_pod'] is None for r in checkpoint_info)} 条无法对应本次 API Pod 列表。它们可能是历史/待清理记录，不能当成当前 RF 实验，也不手工修改 checkpoint。L40 检查点当前无 PodDeviceEntries。详见 kubelet-checkpoint-correlation.json。",
              '已有独立第六轮模型/二进制与 Linux 成功记录已只读核验。上轮测试 HTTP 服务已关闭；本轮只在独立 CPU 单元测试期间启动 loopback 模型服务，无在线 RF Deployment。新 Go 参数 `deviceshare.RFPredictionEnabled` 默认 true，便于只切换学习项，模型和评分权重未变。',
              'RFTimeoutMS=500；服务异常/版本不符/非法 UUID 由 Go 确定性选择回退并重新校验资源；模型 pending 时由 Python 保守评分回退。这两种回退必须分开统计。',
              f"冻结模型 `{service.version}` 对当前逻辑快照的离线预测已存 read-only-snapshot-classification.json（不是线上 RF 调度）。初始标签分布 `{dict(Counter(c['classification']['label'] for c in classifications))}`，不代表这些繁忙设备可供实验。",'',
              '## 隔离判断','',
              '不同 Namespace、schedulerName、leader-election Lease 只隔离对象职责，不能隔离物理 GPU 或跨调度器未绑定预留。Volcano 会观察已绑定的其他 scheduler Pod，但看不到对方尚未发布/绑定的本地预留。NodeLock 是节点注解和短时分配协调，不能作为持久设备独占租约。',
              '本地源码当前仅有 GPU 型号 include/exclude 注解，未发现可直接用于双方统一 UUID 分区的规则。不能伪造 nvidia.com/use-gpuuuid 为有效隔离；只在 RF 侧加 UUID 白名单也不会阻止原生 GPU Plugin、DQN 或宿主机进程访问它。',
              '可证明的首选方案：独占整台 GPU 测试宿主机，且不再受原 DQN/default scheduler、普通 GPU 接口或其他宿主机用户管理；使用独立测试集群或由管理员提供已审计的排他分区。当前没有这样的设备，本轮不把任何现有节点切出生产、不改 DQN 选节点规则，也不自行加 taint/cordon。',
              '同集群采用专用节点标签/taint时，它们只是额外约束。还必须有可验证的“其他入口不能预留这些设备”的保护及宿主机使用授权；单独 NoSchedule taint 也不能阻止带通配 toleration、nodeName 直绑或宿主机 GPU 进程。',
              '至少需要多张独占 GPU 才能验证设备选择差异；单卡可检验绑定/释放链路，但无法证明 RF 改选设备。单一同型号的空闲节点中分类相同，UUID 不变也应如实记录。', '',
              '## 文件、执行与回滚','',
              '`k8s/round7/` 包含 Namespace、最小 RBAC、独立 Queue/Quota、四套 ConfigMap、scheduler+RF sidecar Deployment 草案、9 个单 Pod/PodGroup sleep 用例和两份 Dockerfile。Deployment 默认 replicas=0；镜像含 build-required 占位，测试节点为 rf-exclusive-node-required；不得直接 apply。',
              '独立 schedulerName=volcano-rf-test，独立 Lease gpu-rf-system/volcano-rf-test；RF sidecar只监听127.0.0.1:18081，scheduler健康/指标分别18091/18090。RF sidecar没有挂载SA token；PodGroup明确创建，避免修改现有控制器的schedulerName列表。',
              'RBAC仅允许在两实验 Namespace 修改 Pod/PodGroup/事件/绑定；节点写权限限未来指定节点；不允许删除外部 Pod，不启用 preempt/reclaim。cache 启动仍会尝试 Create root 与自定义默认 Queue，因此需要 queues.create 权限；现存 root 返回 AlreadyExists，不会覆盖 root。必须提前核查 root 存在，不能宣称该启动过程完全不触及共享 API 对象。',
              f"sleep 镜像固定为 `{json.loads((ROOT/'k8s/round7/isolation-gate.json').read_text())['sleep_image']}`，4090缓存及 `/bin/sleep`/glibc ELF 文件已读验证={sleep_verified}；5090/L40未确认有该镜像缓存。只执行/bin/sleep，不导入PyTorch、不运行CUDA；仍不能以Running证明CUDA兼容或成功。",
              '详细顺序、潜在写命令及其目标/回滚见 docs/round7-execution-runbook.md。采集：round7_collect.py、round7_capture.py；UUID一致性：gpu_lab/staging.py；回滚：round7_rollback.py 默认只打印，强制核对真实创建UID与所有权，永不删除Node/Namespace/CRD/root队列，也不清理hamivgpu锁。', '',
              '## 证据与验收状态','',
              '本轮真实 Pod 实验尚未执行。分配成功率、UUID一致率、超分配次数、释放正确率、RF on/off真实UUID变化等均为 null/未测，见 phase2-metrics-NOT-EXECUTED.json。已有180组消融和当前CPU回归均属于离线fake-client证据。',
              '模型监督仍为合成理论标签。资源预留率为逻辑已分配/逻辑注册总量；碎片率定义为1−逐卡可容纳参考请求数/聚合资源可容纳请求的上界（分母0为null）；另记录不能放下4096MiB/20配额/1实例请求的卡上残余显存和算力。它们不表示实际GPU利用率或真实性能匹配率。',
              '当前阻碍：11张GPU均有使用者、DQN没有排他节点边界、4090双接口重叠、检查点历史记录尚未解释完毕、实验镜像尚未构建和固定digest。只有管理员完成安全隔离及镜像/权限预检，并且用户另行明确批准阶段二后，才可以开始真实验证。',
              '源码依据：本地 device_info.go 的FilterNode/ScoreNode/Allocate；rf_job.go 的rfPlanFor/rfValidate/allocateRF；hamicore.go账本；cache.go的addEventHandler/newDefaultAndRootQueue；event_handlers.go的nodeCanAddCache/addTask；nodelock.go的LockNode/ReleaseNodeLock；Device Plugin v1.11.0 的Allocate/GetNextDeviceRequest/EraseNextDeviceTypeFromAnnotation。',
              'kubelet检查点解码以[官方Kubernetes v1.35.0 Device Plugin API](https://raw.githubusercontent.com/kubernetes/kubernetes/v1.35.0/staging/src/k8s.io/kubelet/pkg/apis/deviceplugin/v1beta1/api.proto)的ContainerAllocateResponse.envs字段为依据；必须用Pod UID关联，不能把虚拟DeviceID直接当成物理UUID。']
    (ROOT/'docs/round7-preflight-report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps({'deployment_allowed':False,'gpu_rows':len(combined),'gpu_processes_present':sum(g['host_process_count']>0 for g in combined),
                      'annotation_reconstructed_active_vgpu':sum(g['allocated_vgpu_count'] for g in target),'checkpoint_records':len(checkpoint_info),'sleep_elf_verified':sleep_verified},indent=2))


if __name__=='__main__':main()
