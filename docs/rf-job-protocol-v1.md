# rf-job/v1 通信与原生分配合同

只用于 NVIDIA、HAMI-core 的 Pod 级逻辑资源选择。监督标签为 synthetic；不是物理显存、CUDA 成功率或计算性能保证。Python 是无账本的建议服务。

## 进程与接口

`python -m gpu_lab.runtime.http_server --port 18081` 在启动时构造一次 `SuitabilityService`，验证冻结文件 SHA256，加载一次 joblib。GET `/health`；POST `/v1/rank`。默认仅监听回环；生产候选方式为新测试 scheduler 的同 Pod sidecar，共用回环网络。不是每次调度 fork Python。HTTP 不可用时 Go 使用确定性 binpack，不调用 DQN。

冻结版本 `rf-v4-known-states-ee35cfabe30f`。Go 同时校验 schema、模型版本、request_id、snapshot_token、model_ready、分数范围和完整分配。RF 树投票比例不是可靠性概率。

## 请求

| 字段 | 单位 / 含义 |
|---|---|
| schema_version | 固定 `rf-job/v1` |
| model_version | 必须与固定模型相同 |
| request_id | Pod + 模型版本 + 可见快照内容的 SHA256；不是未来任务信息 |
| snapshot_token | 当前节点全部 GPU 逻辑快照哈希，含任务组成、健康和静态画像 |
| memory_unit_mib | 本版固定 1；`vgpu-memory` 的整数单位为 MiB |
| pod | Kubernetes Pod；只读取明确 Label、vGPU requests/limits、容器名和设备约束。原生已处理的 nodeSelector/affinity/topology 不在 Python 重复实现 |
| gpus | 单节点中通过原生硬约束的物理 GPU；原生完整多设备可行性已先检查 |

每个 GPU：`uuid`、`node`、`model`、`hardware`（冻结规格库的七个值；缺失保留 null）、`registered_memory_mib`、`allocated_memory_mib`、`allocated_compute_quota`（0..100）、`allocated_vgpu_count`、`max_instances`、`healthy`、`sharing_mode`、`training_vgpu_count`、`inference_vgpu_count`、`training_pod_count`、`inference_pod_count`、`unrestricted_vgpu_count`。

数值来自原生 GPUDevice/PodMap，**没有 NVML 输入**。training/inference_vgpu_count 是有标签的已分配 vGPU 份额数量；独立 Pod 数量另存。无标签历史分配不会猜测任务类型，分类输出待验证并采用保守评分。零 cores 是“不限额”的原生语义，不能解释为确定的零性能；对应模型结果同样降级。

Go 对 vGPU Quantity 使用 `AsInt64()` 转成 JSON 数字；Kubernetes 可能把 10000 序列化为 `"10k"`，不能直接传给只接受整数资源单位的 Python 解析器。CPU 等其他资源仍采用 Kubernetes 原始表示。

## 容量域的显式转换

冻结模型中的规格容量使用先前审计保留的保守 nominal-GB 换算：4090=22888、5090=30517、L40=45776 MiB。原生设备注册容量属于独立的逻辑资源域，本次离线夹具为 24564、32607、46068 MiB。后者沿用审查中的注册值作为**模拟输入**，不是本轮重新读取的集群状态。

硬约束、请求百分比、装箱和 Allocate 一律使用原生注册容量与配额；不改成旧规格库容量。只有模型特征采用已声明的表示变换：

`model_reference_free_mib = floor(logical_free_mib * frozen_catalog_capacity_mib / registered_logical_capacity_mib)`。

保留原始动态状态，返回 `model_feature_mapping` 中两域数值、转换名称及 `empirically_validated=false`。这是一项与旧模型特征合同兼容的**未经过真实性能验证的表示假设**，不是实时物理显存观测或性能线性缩放的证明。总容量偏差超过 ±15% 不做映射，明确回退，以免把超售配置或不同 SKU 冒充同一规格。0 表示 scheduler 默认配置中未设置 gpuMemoryFactor；本版接受原始整数 MiB；显式 factor>1 回退。不启用逻辑显存超售研究。

## 响应

`status=ranked|unschedulable|invalid_input`；原样回传 request_id、snapshot_token，返回 model_version、model_ready。`score` 为 [0,100] 的可解释策略分数，不是概率。`assignments` 每条包含：container、unit（从零开始）、gpu_uuid、node、memory_mib、compute_quota、instances=1。

`classifications` 包含每 GPU 的原始快照、RF 原始二目标输出、综合类别、prediction_status、限制原因、模型版本，以及针对该 Pod 第一项设备请求的 training/inference 两套有效分数和得分组成。`details` 保留现有 rank_gpus 的候选排序、失败原因和完整同节点分配。请求多容器时完整 assignment 使用每个容器的真实请求；展示用双目标分数明确标为首个容器请求。

RF 低可信度时返回的 `ranking_basis=deterministic_conservative_proxy` 与 `rf_probability` 分开。模型加载失败为 `model_ready=false` 或版本不匹配；Go 不采用此建议。

## 一致性与回退

1. 原生检查共享模式、类型白黑名单、健康、实例、逻辑显存、逻辑核心、单容器多 GPU 的不同 UUID 要求；不合格时不发 RPC。
2. 持 RF 账本读锁复制 native 状态与 PodMap，然后释放锁，再调用 HTTP。没有在该锁或原生 NodeLock 下等待 Python。
3. 相同快照的 Filter / Score / Allocate 重用有限缓存；包含整个 Pod 和模型版本。成功缓存最长 30 秒，失败缓存 500ms 后可重试。状态变化立即更换 key。
4. Allocate 重新读取当前账本，验证 snapshot_token、每个容器/份额、精确 UUID、请求数值、总请求完整性，并在副本执行原生 TryAddPod。RPC 期间状态变化则返回 stale，交给原生流程重试；不会强行使用旧 UUID。
5. 提交锁中走 `gs.addResource → Sharing.AddPod`，通过现有 patchPodAnnotations 写原有注解。patch 失败调用原生 SubResource 逻辑撤销；成功后更新 Task.Pod 注解供原生 UnAllocate/RemoveTask 使用。重复 informer 重放不二次扣配额。
6. 无任务类型 Label 不发 RPC，沿用 original 的 GPU 顺序与原生 GPUScore 节点分数。非法 Label 显式拒绝。服务错误、超时、模型版本错误、非法 UUID/额度/数量回退确定性 binpack；该分配仍通过同一 native 校验与账本。

本版的 RF 锁是本进程内同步，不是替代 Volcano leader election、session/cache 原有生命周期或 Device Plugin 的 NodeLock。本地并发测试不等同于多 scheduler 副本的线上竞态验证。

## 支持范围

主要验收为普通容器、单 vGPU。已测单容器两张不同 GPU 与 CPU 容器占位；所有份额必须完整同节点。多容器复杂组合沿用现有贪心完整可行性检查，可能保守拒绝，本轮没有穷举全局搜索。MIG/MPS、GPU initContainer 明确拒绝；百分比仅在原生 resourceMemoryPercentageName 已配置时支持，默认空配置显式拒绝。不支持或信息不足时不能伪造输入。软件/设备约束均不能被 RF 类别覆盖。
