# 第八轮动态类别切换：待批准的单节点逻辑实验

**未获执行授权，当前未查询或修改集群。不能 `kubectl apply -f 本目录`。**
本目录有 8 个 Pod/PodGroup 文件，必须逐个执行并删除 observer，最多同时存在 6 个自己的 Pod。
复用第七轮已停止的 RF Deployment、已验证镜像、队列和采集代码；不修改模型、算法或评分。

## 为什么不能直接在 YAML 指定 UUID

核查源码 `scheduler/volcano-rf` 提交 `1a5caec7cb44b7a939f6413057596af3cd1ec19a`：

- `vgpu/utils.go:403 checkGPUtype` 支持型号注解，`:456 checkType` 调用它，未实现请求 UUID 白名单。
- `vgpu/type.go:26–28` 的 `vgpu-ids-new`、`devices-to-allocate`、`vgpu-node` 是分配输出。
- `volcano.sh/gpu-index` 在 vGPU 包内只是常量；真正读取它的是另一条 **gpushare** 路径，不能混用。
- 不通过填分配注解、修改 NVIDIA_VISIBLE_DEVICES、伪造 NodeLock 来指定 GPU。

## 使用原生约束形成唯一候选

`vgpu/utils.go:573 canFitForOrder` 对 100 cores 申请要求设备尚无实例；满核设备不能承接正算力申请。
顺序提交三个 `64 MiB /100 cores /1 vGPU` 的 blocker sleep Pod，确认它们落在三个不同 UUID。
从四卡清单中识别唯一未占位 UUID 作为目标。**不能预先指定是哪张卡**。
以后正算力申请只能合法落在目标卡；如果不再只有一个合法候选，立即停止。
这属于人工构造逻辑状态，不能把其设备选择差异解释为 RF 收益。

|主步骤|目标卡剩余 MiB|剩余 cores|实例数|冻结 RF 离线预测|
|---|---:|---:|---:|---|
|初始|24564|100|0|训推一体|
|compute：申请1000/60|23564|40|1|训练优先|
|memory：再申请17564/5|6000|35|2|不满足参考负载|
|删除 compute|7000|95|1|推理优先|
|删除 memory|24564|100|0|训推一体|

当前真实实验未执行。上述分类是冻结服务的实际离线输出，不是对未来集群结果的保证。
memory Pod 虽标为 inference，申请5 cores 仍可合法准入；标签用于软排序，不等于请求满足参考推理负载。
所有 Pod 只 sleep，不调用 CUDA；占位器同样不会“实际使用100% GPU”。

## 预算与节点使用范围

第七轮预算为4 Pod /4实例 /16384 MiB /80 cores，**不足以做本实验**。
待批准合并补丁 `quota-spec-merge-patch.json` 改为：6 Pod、6实例、18820 MiB、366 cores。
峰值为三个 blocker（192/300）、两个主 Pod（18564/65）加一个 observer（64/1）。
只使用 t3dgq，逻辑全集群该节点容量为98256 MiB/400 cores。占位期间高逻辑预留会阻止其他 GPU Pod，
必须重新协调普通 GPU、vGPU、DRA、自动提交器的短时窗口。宿主机进程不清理，物理显存不要求空闲。
检查现有配额是否有额外 scope 或 admission 限制；若有，不能照搬补丁绕过它。

## 新授权后执行顺序（以下命令本轮均未执行）

在管理节点，进入已同步的项目目录；`LAB` 为实际目录，`B` 为新备份目录。
只在确认协调窗口、四张健康4090、无外部 GPU/DRA Pod/在途预留、无待消费分配/NodeLock、缓存镜像与权限有效后继续。
仅 API 的零 Pod 快照不足以替代管理协调和在途检查。若不满足，停止，不清理历史锁或 checkpoint。

```bash
LAB=/path/to/gpu-scheduling-lab
B="$LAB/results/round8-live-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$B"
kubectl -n volcano-system get deployment volcano-scheduler -o json > "$B/original-scheduler.json"
kubectl -n volcano-system get configmap -o json > "$B/volcano-configmaps.json"
kubectl -n gpu-rf-system get deployment volcano-rf-test -o json > "$B/rf-deployment.json"
kubectl -n gpu-rf-tests get resourcequota rf-v7-test-budget -o json > "$B/quota.json"
kubectl get nodes -o json > "$B/nodes-before.json"
kubectl get pods -A -o json > "$B/pods-before.json"
```

先检查实际原 Scheduler 副本数、UID/规格、RF Deployment 模板与第七轮已验证版本相符。
RF 容器镜像必须为第七轮真实镜像 digest，不能使用旧草案中的 `build-required` 占位镜像。
原 scheduler 配置、DQN 服务不改；若现存 RF Deployment 已变更，先离线审查，不直接套用本说明。

**窗口写操作，只在新的明确批准后执行：**

```bash
kubectl -n gpu-rf-tests patch resourcequota rf-v7-test-budget --type=merge --patch-file "$LAB/k8s/round8-pending-approval/quota-spec-merge-patch.json"
kubectl -n volcano-system scale deployment volcano-scheduler --replicas=0
kubectl -n volcano-system rollout status deployment/volcano-scheduler --timeout=60s
# 再通过 get pods 确认旧 scheduler 进程确已退出，而不只看 Deployment 的期望副本数。
kubectl -n gpu-rf-system scale deployment volcano-rf-test --replicas=1
kubectl -n gpu-rf-system rollout status deployment/volcano-rf-test --timeout=90s
kubectl apply -f "$LAB/k8s/round8-pending-approval/blocker-0.yaml"
# 验证 Running、UID、原生和插件实际 UUID、逻辑增量64/100/1后才继续。
kubectl apply -f "$LAB/k8s/round8-pending-approval/blocker-1.yaml"
# 同上，UUID 必须与第一个不同。
kubectl apply -f "$LAB/k8s/round8-pending-approval/blocker-2.yaml"
# 同上，得到3个不同 UUID；唯一剩余目标卡必须为24564/100/0。
kubectl apply -f "$LAB/k8s/round8-pending-approval/compute.yaml"
# 保存本次 RF 输入/输出、Allocate 日志、永久注解与插件 checkpoint。
kubectl apply -f "$LAB/k8s/round8-pending-approval/memory.yaml"
# 此次 RF 输入应为23564/40/1；实际分配后目标为6000/35/2。
kubectl apply -f "$LAB/k8s/round8-pending-approval/observer-0.yaml"
# 此次 RF 输入应观察 unavailable；保存证据，删除 observer 并核对其64/1配额释放。
kubectl -n gpu-rf-tests delete pod rf-v8-observer-0 --wait=true --timeout=45s
kubectl -n gpu-rf-tests delete pod rf-v8-compute --wait=true --timeout=45s
kubectl apply -f "$LAB/k8s/round8-pending-approval/observer-1.yaml"
# 观察7000/95/1→inference_preferred，再删除observer。
kubectl -n gpu-rf-tests delete pod rf-v8-observer-1 --wait=true --timeout=45s
kubectl -n gpu-rf-tests delete pod rf-v8-memory --wait=true --timeout=45s
kubectl apply -f "$LAB/k8s/round8-pending-approval/observer-2.yaml"
# 观察24564/100/0→balanced；保存后进入清理。
```

这些删除命令仅是可审阅的步骤；正式执行器应复用第七轮 UID precondition 删除机制，
逐个核对对象 UID/experiment 标签，避免删除同名后建对象。PodGroup 尚存在时不自动视为 GPU 配额占用。
不要沿用只识别第七轮 Pod 名称的旧清理白名单，也不要未经审查直接运行第七轮整套在线脚本。

### 为什么有 observer

当前 Go 分配、释放事件记录配额变化，但 RF 是在下一次 Filter/Score/Allocate 被调用，不存在已实现的 `/v1/classify` HTTP 路由。
observer 是一个真正的64 MiB/1 core sleep Pod，用来触发当前账本上的 RF 请求，不是只读查询。
每个 observer 取证后都要删除并确认其配额恢复，再执行下一步；不能一起提交。
小 observer 合法不代表较大的参考负载适配。离线验证表明这些观察器不改变对应阶段类别，但仍须保留真实输出。

## 采集与验收

复用 `gpu_lab/staging.py` 的 `native_commit_receipt`、`inventory`、`uuid_consistency`，
和 `scripts/round7_analyze_live.py` 中 checkpoint 提取原则。新增 Pod 名称不能套入旧固定六例汇总。
每个主步骤及 observer 之前/之后分别保存：

```bash
kubectl get nodes -o json > "$B/STEP-nodes.json"
kubectl get pods -A -o json > "$B/STEP-pods.json"
kubectl -n gpu-rf-system logs deployment/volcano-rf-test -c scheduler --timestamps > "$B/STEP-native.log"
kubectl -n gpu-rf-system logs deployment/volcano-rf-test -c rf-model --timestamps > "$B/STEP-rf.log"
```

先从保存的 Deployment 确认 RF sidecar 实际名字与第七轮 `rf-model` 相同，不能为采集而改在线容器。
从授权的节点只读读取 kubelet `/var/lib/kubelet/device-plugins/kubelet_internal_checkpoint`，
以 Pod UID + container `sleep` + `volcano.sh/vgpu-number` 查找 AllocateResponse 的 `NVIDIA_VISIBLE_DEVICES`。
不把虚拟 DeviceIDs、Python 推荐或 Pod Running 当作插件消费 UUID 的替代证据。
瞬时注解 `devices-to-allocate` 可能已清除，要结合永久 `vgpu-ids-new` 和 `vgpu-node`。

- RF日志：原始输入的每UUID逻辑状态、两个预测、pending、RF项、启发式项、总分、推荐UUID、模型版本。
- 原生日志：以 Pod UID 对齐 `rf-job UUID allocation` 的 `devices`、`logicalBefore/After`、fallback。
- 本地内存快照属于该分配对象，不冒充唯一全局 cache 事务日志；按 Pod 注解重建须另标来源。
- 每次主分配/释放核对上述表格；观察器单独计入再扣除；三张 blocker UUID保持不变。
- 三方 UUID 一致率分母只包括有RF成功响应的例；失败回退例只验原生→插件与合法账本。
- 参考请求碎片率：按4096 MiB/20 cores/1实例，因任一维不足而不可承接参考实例的剩余显存之和 /98256。
  blocker 的碎片是人工占位造成，不能和第七轮未占位结果当作调度性能对比。

## 停止条件与回滚

任意外部 GPU 请求、原 scheduler 意外恢复、blocker退出、唯一候选不成立、剩余配额不符、UUID三方不一致、
模型哈希或权重变化、Pod超过60秒未正常分配、NodeLock长期不释放、镜像/Runtime/Allocate异常，立即停止新增 Pod。
blocker 已运行超过450秒时不再开启新步骤；整轮预定10分钟内完成，若不能则先清理，不依赖sleep退出自动充当成功证据。
sleep不要求物理显存清空，但容器/插件基本开销、全核逻辑占位和临时暂停其他Volcano任务调度确实存在。

清理顺序：保存证据 → 逐UID删除8个清单中实际创建的自己的Pod → 等待API删除并核对原生SubResource/后续快照 →
删除自己的8个PodGroup → RF副本归0、确认进程退出 → 恢复原配额spec → 恢复原scheduler备份副本数 → 检查Ready及原配置不变。
不存在的对象不算失败；不使用宽泛 `delete -l` 波及上一轮对象，不清 kubelet checkpoint、NodeLock、驱动缓存。
若释放核验失败，不在未解释状态下启动第二个 scheduler，保留证据并告知管理员处理。

```bash
kubectl -n gpu-rf-system scale deployment volcano-rf-test --replicas=0
# 确认RF进程退出且自己的GPU Pod与分配记录已正常释放。
python3 - "$B" <<'PY'
import json,sys
from pathlib import Path
b=Path(sys.argv[1])
q=json.loads((b/'quota.json').read_text())
(b/'restore-quota.json').write_text(json.dumps({'spec':q['spec']}))
PY
kubectl -n gpu-rf-tests patch resourcequota rf-v7-test-budget --type=merge --patch-file "$B/restore-quota.json"
ORIGINAL_REPLICAS=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["spec"].get("replicas",1))' "$B/original-scheduler.json")
kubectl -n volcano-system scale deployment volcano-scheduler --replicas="$ORIGINAL_REPLICAS"
kubectl -n volcano-system rollout status deployment/volcano-scheduler --timeout=90s
```

以上回滚前都核对备份对象 UID、窗口内无他人修改；不对原Deployment整对象apply，不覆盖原DQN配置。
RF本轮不应改变原ConfigMap；若有差异先查明，不直接用旧备份压掉他人更新。
本轮尚无新部署、节点使用或提交授权，`execution-gate.json` 保持 false。
