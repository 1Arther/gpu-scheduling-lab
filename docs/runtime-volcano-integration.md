# 第五轮 Volcano vGPU 最小集成设计与已实现接口

本轮实现的是可调用的 Python 分类/设备选择模块、JSON 调用处理器、并发预留模拟账本和 Volcano 模拟适配器。**没有修改、编译或替换在线 Go 调度器，没有启动线上推理服务，没有 SSH 或调用 Kubernetes API。**

当前模型拟合的是第三轮保留至第四轮的合成 FP32 理论合同，不能保证真实训练/推理性能。下文明确区分本地源码已确认事实与后续建议。

## 1. 本地源码可确认的范围

已检查工作区所有 `.go` 和 `go.mod` 文件。可读源码只有两个目录中的8个Go片段，部分重复；没有完整Go模块、设备结构实现和HAMi分配后端。

- `../../audit/custom-volcano/`：`deviceshare.go`、`utils.go`、`type.go`、`dqn_client.go`。
- `../../audit/scheduler-source/`：vGPU的`utils.go`、`type.go`、`dqn_client.go`，以及scheduler options片段。
- 本轮保存逐文件SHA256和实际函数声明/行号：[源码审计](../results/offline-v5-final/volcano-local-source-audit.json)。

用户提供集群为Volcano v1.13.1，历史离线记录也包含对应镜像标签。但是历史二进制`--version`返回Git SHA，目录名/镜像标签不能证明这批片段等同于官方v1.13.1完整源码或正在运行的定制二进制。因此这里不声称已经确认完整版本调用链。

| 环节 | 本地实际文件与行号 | 可确认事实 |
|---|---|---|
| 插件初始化 | `audit/custom-volcano/deviceshare.go:74` `enablePredicate` | 读取VGPUEnable、SchedulePolicy/Weight、GPUSelectPolicy及旧DQN端点 |
| 节点过滤注册 | 同文件`:133` `OnSessionOpen`，`:135` `AddPredicateFn` | 遍历`node.Others`，`:153`调用设备接口`FilterNode` |
| 节点评分注册 | 同文件`:174` `AddNodeOrderFn` | 调用`getDeviceScore`，然后乘ScheduleWeight |
| 节点评分转换 | 同文件`:121` `getDeviceScore` | 调用设备接口`ScoreNode`，`:130`把float四舍五入为int64 |
| 节点设备解析 | `audit/scheduler-source/pkg/scheduler/api/devices/nvidia/vgpu/utils.go:87` `decodeNodeDevices` | 解析UUID、Number、Memory、Type、Health与共享模式，`:131`构造设备 |
| Pod资源解析 | 同文件`:289` `resourcereqs` | 通过`getConfig()`取得资源名称，对每个常规container解析数量、显存、百分比、cores |
| 卡型约束 | 同文件`:403` `checkGPUtype`、`:456` `checkType` | NVIDIA类型检查；大小写不敏感子串白/黑名单，白名单优先 |
| 设备排序 | 同文件`:595` `orderedGPUIndexes` | 当前含original/binpack/spread/random/dqn/dqn-job分支 |
| 试分配与设备分数 | 同文件`:768` `checkNodeGPUSharingPredicateAndScore` | 在快照中遍历请求及排序索引，检查资源、模式、卡型，再调用Sharing.TryAddPod |
| UUID实际来源 | 同文件`:856`调用TryAddPod，`:867`记录ContainerDevice.UUID | UUID来自共享后端返回值，不能直接把数组位置或GPU index当UUID |
| 分配编码 | 同文件`:173` `encodeContainerDevices`、`:189` `encodePodDevices` | 编码UUID、Type、Usedmem、Usedcores |
| Pod注解键 | 同目录`type.go:21–35` | `volcano.sh/vgpu-ids-new`、`volcano.sh/devices-to-allocate`、`volcano.sh/vgpu-node`等 |

本地**没有**`FilterNode`、`ScoreNode`、`Allocate`、`addResource/subResource`的方法定义和HAMi后端实现。`checkNodeGPUSharingPredicateAndScore`的注释提到了Allocate/addResource，但注释不是实际实现证据。不能虚构这些函数所在文件或声称其所有副作用已经审查。

局部调用图如下；问号表示定义缺失，必须用完整定制源码补齐：

```text
deviceshare.OnSessionOpen
  AddPredicateFn -> api.Devices.FilterNode (?)
  AddNodeOrderFn -> getDeviceScore -> api.Devices.ScoreNode (?)

已知vGPU辅助函数：
  checkNodeGPUSharingPredicateAndScore
    -> resourcereqs / getGPUDeviceSnapShot
    -> orderedGPUIndexes
    -> 资源及卡型检查
    -> Sharing.TryAddPod（后端实现缺失）
    -> ContainerDevice{UUID, Usedmem, Usedcores}

最终 Allocate (?) -> 注解/真实资源账本更新 (?)
```

片段里的注释说明曾修复“试分配修改真实账本，再AddPod重复记账”。RF接入必须保持**过滤/评分只操作快照，真实账本只更新一次**，但仍需完整后端源码验证该修复的实际行为。

## 2. 已实现 Python API

代码在`gpu_lab/runtime/`，不依赖旧DQN包：

```python
from gpu_lab.runtime import classify_gpu, rank_gpus
from gpu_lab.runtime.service import SuitabilityService

classification = classify_gpu(gpu_snapshot)
ranking = rank_gpus(pod_yaml_text_or_mapping, gpu_snapshots)

# 调用量较大时持有一个实例；joblib只在初始化加载一次。
service = SuitabilityService()
classification = service.classify_gpu(gpu_snapshot)
ranking = service.rank_gpus(pod, gpu_snapshots)
```

`schema_version=pod-gpu-api/v1`。输入示例见[11卡快照](../results/offline-v5-final/gpu-list.json)。每个设备独立按UUID标识，包含：

- `uuid/node/device_id`及七项静态`hardware`；原始硬件不会随分配变化。
- `dynamic.free_memory_mib/free_compute_quota/allocated_vgpu_count/running_training_count/running_inference_count`。
- `healthy/managed/plugin/backend/sharing_mode/quota_semantics/max_instances/revision/node_labels`。
- RF的`running_*_count`保留训练时的“按vGPU实例”语义；模拟账本另外记录按独立Pod统计的`running_*_pod_count`。两者不能混用。
- 账面预留资源不等于真实GPU利用率。已有无标签任务保留unknown计数，不自动归为训练或推理。

分类输出包含两个适配判断、RF原始输出、综合类、`prediction_status`、相关验证依据、模型版本及限制。状态包括`provisional`、`pending_verification`、`invalid_input`。

模型为`models/pod-gpu-rf-v1/`中冻结的第四轮`known_states`。选择该模型是因为三种集群GPU都在拟合覆盖及数值范围内，初始状态验证依据通过；不是因为其测试准确率最高。模型清单保存原joblib校验值、特征顺序、训练参数、数据集版本和标签合同。每次调度不训练、不下载模型。

## 3. Pod资源语义与最小兼容范围

任务类型只读`gpu-lab/workload-type: training|inference`。缺失则走普通资源装箱；非法值明确错误。完全没有vGPU资源申请时交回普通调度器。

默认资源键使用用户给定的`volcano.sh/vgpu-number/memory/cores/memory-percentage`；真正Go代码通过设备ConfigMap取名字，接入时必须把运行配置映射到API，不能假定永远是默认名称。

本地`resourcereqs`确认的行为：

- `limits`中number或memory触发vGPU解析；memory-only默认一张。
- memory/cores可以从requests补取；API拒绝不一致的requests/limits。
- 没有显存大小和百分比时按100%处理；绝对显存优先于百分比。
- cores默认0；它表示没有保证的原生配额请求，不是分配到100份独占算力。API如实预留0配额、扣减显存和实例，并对这类占用后的RF结论降为待验证。
- 100 cores要求空GPU；UsedCore已100时不能放入0-core请求。
- 每个container的Nums不能超过本节点物理卡数；排序循环对每张GPU最多选一次。所以number=2会在同一节点选两张不同物理GPU，并非把同卡两个实例简单合并。不同container可在资源允许时共享同卡。

API资源输入采用整数计量；示例按离线约定MiB解释memory。Go片段本身只存raw整数，注释写“通常MB”，**当前没有完整device-plugin单位实现证据**。上线前必须确认Memory/UsedMem/资源请求的MB或MiB口径并在Go边界转换；不能把`8Gi`字符串直接当`8192`或把字节当MiB。API目前拒绝带Mi/Gi/m后缀的原始扩展资源输入，避免隐式错误换算。

GPU initContainer生命周期、MIG/MPS、复杂affinity/topology spread不在MVP内，不近似实现或伪造支持。CPU、内存、taint、gang/queue与其他Volcano predicates继续由原调度器处理。

Go侧应在现有`task.Pod`上直接读取`task.Pod.Labels["gpu-lab/workload-type"]`；这是建议增加的读取逻辑，当前片段没有RF分支。不要从Pod名称、镜像或旧DQN的Job分组猜任务类型。缺失Label保留unknown，非法值返回明确校验错误。

设备快照的拟议字段映射如下，依据是本地代码中实际使用的字段；完整结构定义仍缺失：

| API字段 | Go权威缓存字段/计算 | 需要确认的边界 |
|---|---|---|
| uuid/node/model | `GPUDevice.UUID/Node/Type` | UUID不能用设备map index替代；Type要做经核实的型号/SKU映射 |
| healthy/managed | `Health`及`node.Others`中已初始化的HAMi设备对象 | 还需完整实现中的健康更新和资源插件管理状态 |
| free_memory_mib | `Memory-UsedMem`转换单位 | MB/MiB需核实，负值属于非法快照 |
| free_compute_quota | `100-UsedCore` | 是账面配额，不是监测算力利用率 |
| allocated_vgpu_count/max_instances | `UsedNum/Number` | Number是实际插件配置，不能强行写成模型实验中的10 |
| 已有任务类型/独立Pod数量 | `PodMap`对应的Pod缓存及Label | GPUUsage/PodMap key定义缺失，需完整源码确认连接方式；未知Label保留unknown |
| BW/FP32等硬件参数 | 本轮冻结硬件库或经过核实的静态画像 | 设备注解本身不包含完整算力/BW；未知型号不猜峰值 |
| revision | 原账本更新处维护的generation | 不是只依赖Node.resourceVersion |

现有实验使用最多10实例与合成配额缩放。实际Number、动态状态或共享方式超出该训练/验证配置时，不应宣称已获得新的验证依据；即使硬资源可分配，分类仍只可作带限制的诊断/保守建议。本轮没有验证更多插件共享配置。

## 4. 硬过滤与可解释排序

顺序为后端→健康与插件管理→资源语义→显存/配额/实例→100-core独占及其他已实现约束→评分。实现nodeName、nodeSelector、共享模式与卡型白/黑名单。

当前Pod先通过物理约束，不以“是否满足参考负载”作为准入条件。比参考负载小的具体Pod即使落在“当前不满足参考负载”设备上，仍可以按资源可行性参与排序。

有有效暂定预测时使用对应目标的RF边际估计作为软提示，保留原调度的硬件、共享、packing及balance因素。字段分别为：

- `rf_probability`：RF原始边际输出，不是可靠性。
- `ranking_basis=RF_marginal_soft_hint`：真正采用RF提示。
- `fallback_score`：低可信/未知/模型异常时的确定性保守代理，**不是概率**。
- `ranking_basis=deterministic_conservative_proxy`：仅用资源与显式假设排序。
- `components/component_weights`：各项得分和权重；四类只作展示和参考提示。

缺关键性能特征但有可靠资源账本的设备可参与保守资源排序；缺物理容量、无效剩余量或不被资源插件管理的设备不能绕过硬过滤。

多卡计划按节点、container依次进行确定性贪心试放置；整个Pod必须有完整同节点方案才返回recommendation。这个MVP不保证找到所有可能的多容器组合，找不到时安全回退/重试，不输出半个可执行计划。

## 5. 评分UUID如何与最终分配一致

Python返回的recommendation包含每个container/unit的UUID、节点、显存/配额、设备`expected_revisions`、请求指纹、模型版本和计划指纹。

离线`GPUPool`已经实现并测试：

1. 在快照上计算排名，不改账本。
2. 预留时持RLock核验Pod指纹、模型版本、完整槽位、UUID、同节点和per-container不同物理卡约束。
3. 检查每个设备revision和全部硬约束；先在临时副本完成所有扣减，全部成功后原子发布。
4. 更新各物理UUID的资源、实例和任务计数，并重新进入分类API。
5. `commit`必须确认原计划UUID列表；不一致或健康状态改变则回滚，不默默接受另一次选卡。
6. 释放幂等；rollback与过期回收归还资源并再次分类。`reserve_best`暴露有界重试。

这是**一个进程中的模拟账本**，不是分布式锁。真实集成不应在Python再维护第二套权威预留。Go仍是资源唯一权威，必须在其原有设备账本锁/调度事务中完成上述重校验与实际扣减。

建议的Go接入点（待完整源码确认/实施）：

- 在`orderedGPUIndexes`增加独立`rf-adaptive`策略分支，由UUID映射回当前设备索引；不要复用旧DQN返回索引协议。该新策略目前尚未在旧二进制注册，不能现在改在线配置启用。
- 在`checkNodeGPUSharingPredicateAndScore`返回结果旁保存明确的`ContainerDevices`计划，而不是只保存一个节点分数。
- 在Filter/NodeOrder间通过会话级缓存传递计划，键为Task UID + Node + 资源请求指纹 + 模型版本 + 设备账本generation。
- `ScoreNode`最终会被`getDeviceScore`转成int64；RF浮点0..1若直接传入会丢失排序信息。建议显式缩放到0..100，再沿用ScheduleWeight，不能未经验证改变原策略分值含义。
- 在真实Allocate入口取得同一份UUID计划，锁内比较generation及资源量，再按该计划进行TryAddPod/真实addResource。TryAddPod若返回其他UUID必须失败重试；不得评分后又随机选卡。
- generation必须随缓存中的资源预留、释放、健康、管理状态变化递增。Kubernetes Node.resourceVersion不能覆盖调度器内存账本的所有变化，不足以独自充当CAS版本。
- Filter/Score的TryAddPod只能操作隔离快照。真实addResource更新一次；bind失败/事务撤销执行对应subResource，并清理计划。

这些是真正接入前需要实现的变更，当前未对不完整Go片段打补丁或声称已经完成。

## 6. Python/joblib与Go的最小边界

| 方式 | 开销与改动 | 本轮选择 |
|---|---|---|
| 每次启动Python/joblib | 冷启动与加载开销高，取消/并发难管理 | 不采用 |
| 常驻本地Python HTTP/Unix socket JSON | 模型只加载一次；Go标准HTTP客户端可设deadline；容易mock | 推荐下一步，尚未启动HTTP监听 |
| 常驻Python JSON-lines子进程 | 无HTTP依赖，已提供worker；但Go需序列化流、重启及超时后的消息关联 | 已实现可离线运行，用于契约测试 |
| ONNX/PMML或Go重写树 | 需要额外转换与逐样本一致性验证；joblib多目标+imputer+证据门控不能直接当成单树输出 | 暂不增加工作量 |
| Go嵌Python/cgo | 构建、GIL、依赖部署复杂 | 不适合两周MVP |

`adapters.dispatch(service,payload)`是纯JSON处理器，已用于测试，可被未来HTTP handler复用：

```json
{"schema_version":"pod-gpu-api/v1","method":"rank_gpus","pod":{"kind":"Pod","metadata":{},"spec":{}},"gpus":[]}
```

这里的Pod空对象仅展示协议结构，会得到invalid_input，真实完整输入见examples/runtime。请求中不得省略实际资源；响应模型版本、schema、请求指纹和UUID必须由Go校验。

建议初始使用单个Go调度器本地常驻服务，加载只读固定bundle；不要在模型调用中读取集群或更新资源。请求按节点批量给出设备快照，缓存仅在特征/版本完全一致时复用。本机warm-call测量见[API开销](../results/offline-v5-final/inference-overhead.json)，该数字不包括RPC、Go调度周期和真实GPU业务。

## 7. 失败与超时契约

后续HTTP客户端必须使用`context.WithTimeout`和明确响应上限；超时不能阻塞整个调度器。deadline应根据实际冷/热测量与调度周期确定，本轮不声称已验证某个线上毫秒预算。

| 情况 | 行为 |
|---|---|
| 模型文件损坏/加载失败/特征顺序或校验值不符 | 标记pending，使用可信本地保守资源排序；不临时训练 |
| HTTP异常/超时 | 使用Go本地确定性回退完成同样的资源过滤与计划生成，不调用旧DQN |
| schema/模型版本/Pod指纹不匹配、返回未知UUID或重复设备 | 拒绝建议，生成本地回退计划 |
| 分数NaN/Infinity/非法数值 | 拒绝建议；不允许影响原调度器稳定性 |
| Allocate时资源或健康generation变化 | 丢弃旧计划，原子取消预留并重新排队，有限次重试 |
| 试分配与真实分配UUID不同 | 视为失败；撤销对应native事务，重新评分，不能静默换UUID |
| 无Label | 原资源调度/确定性binpack，不推断任务类型 |

`MockVolcanoAdapter`已通过注入TimeoutError和不一致UUID验证回退/回滚。它不是实际HTTP网络超时测试，也不是Go插件端到端测试。

## 8. 下一步接入顺序

1. 获取与当前定制二进制对应的完整源码及commit，确认go.mod、FilterNode/ScoreNode/Allocate/addResource/subResource和设备插件实现。
2. 明确内存单位、ConfigMap资源名、PodMap统计与HAMi-core账本锁，补全设备快照适配。hardware映射用受审计型号/实际静态参数，UUID与资源量来自权威缓存。
3. 在独立分支实现新的RF策略、UUID计划缓存和native Allocate重校验；新增Go单元测试，包括回滚、防重复记账和共享状态变化。
4. 给dispatch套本地常驻服务，固定模型版本；Go接入deadline、本地回退与可观察日志，验证同一输入/UUID计划一致性。
5. 先在本地fake cache或独立测试环境测试；需要部署或修改真实集群时再另行安排。本轮不改在线dqn-job策略。
