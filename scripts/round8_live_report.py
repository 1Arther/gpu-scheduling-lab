"""Render actual round-8 results from validated receipts; no new RF predictions."""
import csv
import datetime as dt
import json
import os
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'results/round8-live-20261020'
os.environ.setdefault('MPLCONFIGDIR',str(ROOT/'.tmp/matplotlib-round8'))
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def load(path):return json.loads(path.read_text(encoding='utf-8'))


def rows(name):
    with (OUT/'verified'/name).open(encoding='utf-8-sig') as f:return list(csv.DictReader(f))


def main():
    state=load(OUT/'evidence/attempt2/execution/state.json');summary=load(OUT/'verified/summary.json')
    first=load(OUT/'evidence/attempt1/execution/state.json')
    states=rows('states.csv');pods=rows('allocations.csv');restore=summary['restore_confirmation']
    plt.rcParams.update({'font.sans-serif':['Microsoft YaHei','DejaVu Sans'],'axes.unicode_minus':False,'svg.fonttype':'path'})
    fig,axs=plt.subplots(2,1,figsize=(10,6.2),sharex=True,height_ratios=[2,1])
    x=list(range(5))
    axs[0].plot(x,[int(s['free_memory_mib'])/24564*100 for s in states],'o-',label='剩余逻辑显存比例')
    axs[0].plot(x,[int(s['free_compute_quota']) for s in states],'s-',label='剩余算力配额比例')
    axs[0].set(ylabel='剩余比例 (%)',ylim=(-3,108),title='真实 Kubernetes sleep Pod：同一 RTX 4090 的动态 RF 分类')
    axs[0].legend(loc='lower left');axs[0].grid(alpha=.2)
    names={'balanced':'训推一体','training_preferred':'训练优先','unavailable':'参考负载不足','inference_preferred':'推理优先'}
    palette={'balanced':'#3b9d84','training_preferred':'#427ab3','unavailable':'#8b8f98','inference_preferred':'#e69636'}
    zh=[names.get(s['label'],s['label']) for s in states]
    colors=[palette.get(s['label'],'#af73b2') for s in states]
    for i,(text,color) in enumerate(zip(zh,colors)):
        axs[1].bar(i,1,color=color,width=.85);axs[1].text(i,.5,text,ha='center',va='center',color='white')
    axs[1].set(yticks=[],ylabel='冻结 RF 实际返回',xticks=x,xticklabels=['S0 初始','S1 compute 后','S2 memory 后','S3 释放 compute','S4 释放全部'])
    fig.text(.5,.01,'真实逻辑配额与实际 RF 输出；不代表 CUDA、训练或推理性能；三张满配额卡仅用于固定目标设备',ha='center',fontsize=9)
    fig.tight_layout(rect=(0,.04,1,1));folder=OUT/'figures';folder.mkdir(exist_ok=True)
    for ext in ('png','svg'):fig.savefig(folder/('real-dynamic-transition.'+ext),dpi=190,bbox_inches='tight')
    plt.close(fig)
    def local(utc):return dt.datetime.fromisoformat(utc).astimezone(dt.timezone(dt.timedelta(hours=8))).isoformat()
    duration=(dt.datetime.fromisoformat(state['restored_utc'])-dt.datetime.fromisoformat(state['stopped_utc'])).total_seconds()
    state_table='\n'.join('|'+ '|'.join([s['state'],s['free_memory_mib'],s['free_compute_quota'],s['allocated_vgpu_count'],zh[i],s['prediction_status'],s['pod_name']])+'|' for i,s in enumerate(states))
    pod_table='\n'.join('|'+ '|'.join([p['pod_name'],p['pod_uid'],p['request_memory_mib']+'/'+p['request_compute_quota'],p['allocate_uuid']])+'|' for p in pods)
    text=f'''# 第八轮真实 Kubernetes 动态 vGPU 分类验证结果

本次实际在 t3dgq 的四张4090上运行。授权记录 `user-round8-live-20261020`，全部容器仅执行 `/bin/sleep 600`。
未重新训练模型，未改分类门槛、评分权重、Go二进制、DQN服务配置、驱动、Device Plugin或运行时。

## 结果

目标真实 GPU UUID：`{state['target_uuid']}`。
三个 blocker 分别占据另外三张卡的100算力逻辑配额，主过程只有一张硬合法候选卡。
下面类别直接取自实际RF HTTP响应，以原生Allocate的requestID和Pod UID关联，未用离线期望替换输出。

|状态|逻辑剩余MiB|剩余算力|实例数|实际RF类别|状态|触发观测的Pod|
|---|---:|---:|---:|---|---|---|
{state_table}

S0由compute提交前的快照观测；S1由memory提交前快照观测；S2～S4由各observer提交前快照观测。
observer自身64MiB/1core另行记账，取证后删除，再进入下一主步骤。
compute申请1000MiB/60cores，memory申请17564MiB/5cores。类别确实完成四类切换，并在资源释放后恢复。
原始硬件特征不变；逻辑注册容量24564MiB，既有模型参考容量映射22888MiB原样保留。
所有预测均provisional，是冻结合成参考合同适配输出，不是实测训练/推理性能标签。

![真实动态类别切换](../results/round8-live-20261020/figures/real-dynamic-transition.png)

## 八次实际分配

以下每一行的 RF 推荐UUID、原生Allocate永久UUID注解、插件消费UUID均一致。
插件证据是与Pod UID、sleep容器、vgpu-number资源匹配的kubelet checkpoint AllocateResponse.NVIDIA_VISIBLE_DEVICES，
没有将虚拟DeviceIDs或Pod Running当作GPU UUID证据。

|Pod|真实Pod UID|申请MiB/cores|最终UUID（RF=Allocate=Plugin）|
|---|---|---|---|
{pod_table}

独立核对结果：分配合法8/8，三方UUID一致8/8，原生配额增量正确8/8，观察到显存/算力/实例超分配0次，
8个Pod删除与释放均有对应UID原生日志，最终全部GPU逻辑预留归零，NodeLock为空，自己的PodGroup为0。
八次分配均无原生服务异常回退；之前已经验证的故障回退本轮没有重新制造。

原生logicalBefore/After属于实际分配对象，不冒充唯一全局cache事务日志。
API注解重建账本单独标注；SubResource可能出现在多个克隆视图，日志行数不当成释放次数。

## 清理与恢复

主成功窗口暂停原Scheduler：{local(state['stopped_utc'])}；恢复完成：{local(state['restored_utc'])}，约{duration:.2f}秒。
之后独立只读复核记录见 `evidence/attempt2/final-independent-readonly.json`。

- 原Scheduler恢复1/1，Deployment spec与备份一致。
- 原ConfigMap内容相同，resourceVersion仍为 `{restore['original_config_rv']}`。
- 原ResourceQuota完整spec恢复，pods、vgpu-number、vgpu-memory、vgpu-cores的used全部为0。
- RF Deployment恢复0副本，规格与实验前相同；DQN服务保持1/1、spec相同。
- RF模型包全部文件前后SHA一致，joblib为 `ee35cfabe30fffc28ff0be09d3738587d4f9bffbe26386e2a9fae5c5b9e80748`。
- 原Scheduler实际运行的可执行文件SHA前后相同：`9f2e1ae3359ce234fd048bc48aab082add99a686631808908b5ae518bdebf4f4`。
- 不终止宿主机GPU进程，不清checkpoint/NodeLock，不修改二进制、模型权重或GPU组件。

## 保留的首次失败

首次尝试创建了2个blocker，两个均获得一致的RF/原生/插件UUID；随后采集器发生 `{first['error']}`。
原因是错误地要求RF请求包含全部注册卡，而实际 `rfPlanFor` 在HTTP前过滤不适合当前Pod的设备。
该次自动清理了2个Pod与PodGroup，并恢复配额和原Scheduler，原 `experiment_success:false` 原样保留。
修正的是证据采集器：核对RF输入等于原生硬合法子集，并逐项比较该子集当前资源；完整原生前后账本继续核对全部四卡。
没有修改Go策略、模型输入数值、权重或标签。独立attempt2重新做当前检查后获得以上完整八例。
合计本任务创建10个自己的Pod，两个尝试均正常清理恢复；不能把首次未完成轨迹记录成成功实验。

后处理另正确识别API规范化的 `1k` 为1000个自定义资源单位，即1000MiB，不是1000字节。
Pod YAML和Go协议仍使用原先的整数MiB约定，未因此改模型或在线接口。

## 证据、复现与解释边界

- `verified/states.csv`：S0～S4、原始类别、适配、pending状态、RF/启发式/总分与三方UUID。
- `verified/allocations.csv`：8个完整UID、申请、永久注解、节点注解、插件证据。
- `verified/candidate-scores.csv`：实际可行候选的两种任务评分及RF原始结果。
- `verified/native-ledger-transitions.csv`：全部四卡、8次原生Allocate前后计数。
- `verified/release-checks.csv`：8个UID实际释放证据；`verified/summary.json`：验收指标和恢复回执。
- `evidence/attempt1`、`evidence/attempt2`：133个关键源文件文本快照，内容摘要和传输压缩摘要均已校验；
  派生CSV的CRLF被文本传输规范为LF，详见critical-evidence-manifest.json。
- 全量原始档案仍保存在服务器 `/home/zbs/rf-round8-live-20261020-raw.tar.gz` 与
  `/home/zbs/rf-round8-live-20261020-attempt2-raw.tar.gz`，原始字节SHA见final-package.jsonl；不将不完整的大文件下载当成本机完整档案。

本轮证明了真实API、原生逻辑配额、冻结RF动态分类与设备UUID传递的完整链路。
因为另外三张卡被逻辑满核占位，**不能用本轮唯一候选的UUID选择证明RF相对启发式的独立优选收益**。
独立贡献仍以第六轮180组原生Go消融中的37组变化为证据，且那是fake-client实验。
本轮不测CUDA成功、物理显存预分配、训练时间、推理延迟或吞吐量，不宣称真实性能提升。
各状态评分对应不同触发Pod，不能把跨状态总分差当成仅由类别造成的因果效应。

本次授权窗口已完成，执行门已关闭，防止把本次授权用作未来重复实验的长期许可。
本地只读重验：`python -m pytest tests/test_round8_live_receipts.py tests/test_round8_real_evidence.py -q`。
原始大日志仍在服务器，不需要重新切换Scheduler即可取证。
'''
    report=ROOT/'docs/round8-real-kubernetes-report.md';report.write_text(text,encoding='utf-8')
    (OUT/'README.md').write_text('''# 第八轮真实实验结果

已完成真实同卡S0～S4分类变化；所有实验对象清理，原Scheduler与Quota恢复。

- [实际执行报告](../../docs/round8-real-kubernetes-report.md)
- [五状态表](verified/states.csv)
- [八Pod UID与三方UUID](verified/allocations.csv)
- [候选评分](verified/candidate-scores.csv)
- [原生配额前后记录](verified/native-ledger-transitions.csv)
- [释放证据](verified/release-checks.csv)
- [指标与恢复回执](verified/summary.json)
- [关键证据内容摘要](critical-evidence-manifest.json)
- [独立只读最终复核](evidence/attempt2/final-independent-readonly.json)

首次采集器失败也完整保留并已恢复；大文件传输不完整的临时文件不属于有效证据集。
所有分类监督仍为合成理论合同；本轮GPU逻辑状态、Pod、RF响应、原生与插件UUID是实际观测。
''',encoding='utf-8')
    print(report)


if __name__=='__main__':main()
