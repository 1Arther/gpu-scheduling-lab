# 第二研究点第四阶段：Volcano 原生 HRRN 本地实现与验证

2026-10-26。代码分支 `codex/hrrn-native-v1.13.1`，冻结基线 `1a5caec7cb44b7a939f6413057596af3cd1ec19a`。这是项目已经定制的 Volcano 源码，API 依赖版本为 v1.13.1，不把它冒称为未经修改的上游发行版。

**结论：已在实际 Go 调度路径中实现默认关闭、可回退的 HRRN 排序，并验证它在前置约束平局时改变真实 fake-binder 绑定结果；完整作业模式可以回滚不完整规划并旁路后续 Job。尚未证明真实 Kubernetes 容器执行顺序、整体等待改善或生产可用性。**

本轮没有训练 PPO/DQN/GNN，没有修改 RandomForest 模型、评分、标签、推理服务或 GPU 资源账本，没有访问生产集群。冻结的模型、运行时和资源后端共 **83 个文件逐字节 SHA256 一致**，见 `frozen-verification.json`。随机森林模型 SHA 为 `ee35cfabe30fffc28ff0be09d3738587d4f9bffbe26386e2a9fae5c5b9e80748`。

## 1. 从真实源码得到的流程

`pkg/scheduler/framework/session_plugins.go` 中 Session.JobOrderFn 按 tier 和插件配置顺序调用回调，第一个非零比较结果即决定先后。全部平局后才回退 JobInfo.CreationTimestamp、JobInfo.UID。因此不能把原生 Volcano 简化成 FIFO。

在本轮保留的顺序中：priority 先比较 Job.Priority；gang 将尚未满足 Gang ready 的 Job 排在已满足的 Job 前；drf 比较当前已分配的主导份额。**这些前置判断不会被 HRRN 覆盖**。proportion 主要负责 QueueOrder、Overused 和 Allocatable：队列优先级、份额、开放状态、deserved/capability 仍使用原生实现。QueueInfo 是队列约束和统计载体；JobInfo 是 PodGroup 及成员 Task 的缓存对象；调度插件读取的 Job 元数据实际来自 PodGroup。

原生 allocate 先排 Queue，再在 Queue 内排 Job，然后排 Task；执行 PrePredicate、Predicate、节点评分和节点选择，再以 Statement 暂存分配。predicates 的设备回调调用 deviceshare 选择的设备后端，rf-job 在原生资源可行后通过冻结模型为候选设备评分，验证完整单 Pod 多 GPU 请求，提交 UUID 与 native AddPod 账本。NodeOrder 中的 `binpack` 插件和 `deviceshare.GPUSelectPolicy=binpack` 是不同配置：前者参与节点评分，后者选择节点内 GPU；两者都不与 HRRN JobOrder 同层。

enqueue 负责从 Pending PodGroup 进入 Inqueue，检查原生入队条件及 MinResources。allocate 本来就会在某个 Job 放不下时继续处理其他 Job。本轮没有把这一已有旁路机制当作新算法。backfill 原生 action 处理 BestEffort 待调度任务，**不是 EASY 时间预约**。

### 配置差异表

| 配置来源/实验组 | actions | tier 1 | tier 2（顺序不可随意改动） | GPU 选择 | HRRN / 全成员规划 |
|---|---|---|---|---|---|
| 源码内置 DefaultSchedulerConf | enqueue, allocate, backfill | priority, gang, conformance | overcommit, drf, predicates, proportion, nodeorder | 不包含 deviceshare 配置 | 无 |
| 仓库 Helm 默认配置 | 同上 | 同上 | 上述五个 + binpack | 不包含 deviceshare 配置 | 无 |
| 历史 round7 RF 草案 | enqueue, allocate | 同上 | deviceshare, predicates | rf-job | 无；**不是当前集群快照** |
| A：本轮原生排序 + RF | enqueue, allocate, backfill | 同上 | overcommit, drf, predicates, proportion, nodeorder, binpack, deviceshare, hrrn | rf-job | false / false |
| B：HRRN + RF | 与 A 一致 | 与 A 一致 | 与 A 一致 | 与 A 一致 | true / false |
| C：HRRN + 完整规划旁路 + RF | 与 A 一致 | 与 A 一致 | 与 A 一致 | 与 A 一致 | true / true |
| D：原生排序 + Binpack | 与 A 一致 | 与 A 一致 | 与 A 一致 | binpack；RFEnable=false | false / false |
| E：原生排序 + Spread | 与 A 一致 | 与 A 一致 | 与 A 一致 | spread；RFEnable=false | false / false |

本轮 A 是 **Helm 原生排序链 + 显式冻结 RF 放置**，不是声称直接运行了未修改的 Helm 默认配置。A/B/C 只改变 hrrn 参数，D/E 作为整体系统参考单独解释。Go manifest 测试检查五组配置除上述允许字段外完全相同。完整配置在 `k8s/hrrn-stage4/configs/`；每条原生比较记录也保存实际 tiers。

## 2. HRRN 插件与元数据

新增 `pkg/scheduler/plugins/hrrn/`，注册到 factory，默认 `enabled=false`。不改 Helm 默认文件。参数：`enabled`、`complete-job`、`tau-seconds`（默认 1）、`default-duration-seconds`（默认 60）。

HRRN = 1 + waiting / max(estimated_service, tau)。仅同 Queue、同 Priority 且 priority/gang/drf 比较为平局时使用；再按 ready_at、JobInfo.UID 稳定排序。配置把 HRRN 放到这些比较器之前时，插件拒绝启用。完整规划还要求 predicates 在 HRRN 之前且 enableJobReady 不为 false。

自定义 Job annotations：

```yaml
metadata:
  annotations:
    gpu-lab/estimated-duration-seconds: "30"
    gpu-lab/total-pods: "2"
```

这是项目协议，不是 Kubernetes 原生字段。Job controller 在创建 PodGroup 时复制 Job.Annotations；插件读取 PodGroup.Annotations。现有 controller 的更新分支不会自动同步任意 annotation，因此实验创建后冻结估计；动态更新需显式更改 PodGroup 并记录。完整协议见 `k8s/hrrn-stage4/METADATA.md`。

完整就绪定义为：调度器观察到精确声明数量的 Pod，成员不缺失、不删除、不 Failed、没有 schedulingGate。不是 Pod 创建时间、容器 Ready 或资源就绪。每个 Session 只读取一次时钟，冻结本轮评分；进程内 Tracker 保存跨 Session 的首次完整就绪时间。成员 UID 替换、缺失、取消会重置/清理；PodGroup 重建不继承旧历史。缺失/无效估计确定性回退 60 秒并记录原因。

**限制：** ready_at 不是持久化状态，重启/leader 切换后保守重新计时。适用固定成员的有限 Job；本轮没有验证弹性扩缩容、失败重试图或跨重启等待公平性。

## 3. 完整可行性与旁路的实际语义

原生 Gang 的 minAvailable 可以小于 Job 的全部 replicas。allocate 一旦 ssn.JobReady 为真，就可能提交满足最小 Gang 的部分成员。这不是完整 Job 分配。

C 在原生路径上适配完整规划：要求全体声明成员已就绪；JobReady 必须满足全部成员，前置 Gang 仍必须通过；沿用同一 TaskOrder、节点/GPU 可行性、评分及 Statement。完整规划失败时原生 Discard 释放资源，队列继续尝试后续 Job。

必须额外处理 JobPipelined：原生方法可能在前一个 tier 的 Permit 后直接返回。单纯在最后一个 tier 加 Reject 不足以阻止部分规划被保留。新增 `AddJobPipelinedVetoFn` 是 **只可拒绝、不可授予资源的前置 veto**；没有注册 veto 时原生行为不变。C 不保留部分规划的未来资源 pipeline。

原生 vGPU Allocate 回调会在 Statement.Commit 前 patch Pod 的 UUID。原 Release 回调为空，Discard 虽会还原账本，但不足以清理 API metadata。因此 HRRN 完整模式在原生 predicates Deallocate 后，仅恢复未绑定 Pending Pod 本轮改动的分配元数据；校验 UID/NodeName/ResourceVersion，不触碰 RF 评分与 AddPod/SubPod 逻辑。反复分配—回滚—重试测试验证账本与 UUID 没有累积。

这是一种**沿原生放置路径试规划后提交的完整性检查**，不是无副作用的查询，也不是枚举所有 GPU/节点组合的完备可行性求解。原生贪心路径失败不代表不存在另一种可行组合。当前完整模式拒绝无资源请求的 BestEffort Job，防止原生 backfill 绕过完整检查。

**绝不称为跨 Pod 原子提交：** Statement.Commit 逐个把 Pod 送入绑定队列，后续 API/绑定失败仍可能留下已绑定成员；UUID 清理也可能遭遇 API 冲突或服务不可用。本轮未实现分布式事务或真实集群故障恢复。相关限制是集群试验前的检查项。

## 4. 本地原生对照结果

实际执行 Windows Go 交叉编译产生的 Linux/amd64 测试程序，运行环境为本机 WSL；使用真实 SchedulerCache、enqueue/allocate/backfill、原生插件和 fake Kubernetes/Volcano client、FakeBinder，以及本机 CPU 冻结 RF Python HTTP 服务。没有使用 Python 队列仿真替代系统路径。

### 顺序案例

一张逻辑 GPU，两 Job 都请求 100 核心配额。long 更早创建；二者首次完整观察后共同等待 20 秒。预计时长 long=100、short=10，所以 HRRN 分别为 1.2、3。Job 优先级、Queue、Gang readiness、DRF 份额均平局。

| 组 | 实际 fake-binder 首个绑定 Pod | GPU UUID | 绑定后核心配额 |
|---|---|---|---:|
| A 原生 + RF | long-0 | SIM-HRRN-GPU-0 | 100 |
| B HRRN + RF | short-0 | SIM-HRRN-GPU-0 | 100 |
| C HRRN 完整规划 + RF | short-0 | SIM-HRRN-GPU-0 | 100 |
| D 原生 + Binpack | long-0 | SIM-HRRN-GPU-0 | 100 |
| E 原生 + Spread | long-0 | SIM-HRRN-GPU-0 | 100 |

数据：`native-comparisons.json/csv`。这证明 **Go 调度选择和绑定请求发生变化**，不只是评分日志变了；fake-client 不会真正拉镜像/启动容器，不能声称已证实集群实际运行先后。

### 完整 Job 案例

一张 GPU，大 Job 两个 Pod 各需 60 核心配额，Gang minAvailable=1、total-pods=2；后续小 Job 需要 100。

| 组 | 大 Job 已分配/绑定成员 | 小 Job 已分配/绑定成员 | GPU 核心配额 |
|---|---:|---:|---:|
| A 原生 + RF | 1 / 2 | 0 / 1 | 60 |
| B HRRN + RF | 1 / 2 | 0 / 1 | 60 |
| C 完整规划 + RF | 0 / 2（已回滚） | 1 / 1 | 100 |

另验证：两 Pod 各需 40 时 C 可以完整提交；单 Pod 请求两张 GPU 而仅一张可用时不部分启动，后续单卡 Job 可被调度；两张均可用时收到两个不同 UUID。Gang minAvailable=全部成员时，**原生本身也会旁路** 放不下的 Job。

数据：`bypass-comparisons.json/csv`。这些是完整性案例，不是吞吐量改善统计。

### 设备策略参考

两张同型号 GPU：GPU-0 已有逻辑显存 8192 MiB、核心配额 40、实例 2；GPU-1 空闲。新 Job 单卡需 4096 MiB、20 核心配额。

| 设备策略 | 原生路径写入的 UUID |
|---|---|
| Binpack | SIM-HRRN-GPU-0 |
| Spread | SIM-HRRN-GPU-1 |
| 冻结 RF | SIM-HRRN-GPU-1 |

数据：`placement-comparisons.json/csv`。RF 在这个案例与 Spread 相同，不据此宣称两者整体等价或 RF 更优。主 HRRN 测试记录 19 次真实 RF UUID 分配，非空 fallback 为 0，见 `hrrn-rf-evidence.json`。设备后台负载全部是合成逻辑状态，不是 GPU 性能采样。

记录了 actionsWallSeconds（真实 Go 动作执行墙钟，含 RF HTTP）和 executeAndCheckWallSeconds（另含测试绑定检查等待）。本轮是单次功能验证，且 RF 有缓存、服务预热、WSL 开销，不能据此排列 A/B/C 的调度速度优劣。

## 5. 测试结果与失败保留

主测试记录共 **349 个叶级 case 通过、1 个失败、0 个跳过**；父测试与子测试不重复算作叶 case。Gang 包本身没有独立测试文件，Gang 约束由 HRRN/allocate 集成案例覆盖。

| 范围 | 通过叶 case | 失败 | 说明 |
|---|---:|---:|---|
| 新 HRRN 包 | 23 | 0 | 14 个顶层测试；包括真实 Go 类型解析配置/Job YAML |
| framework | 9 | 0 | 包含 tier Permit 前的硬 veto 和不能越权 grant |
| priority / drf / proportion | 11 | 0 | 原有公平机制回归 |
| deviceshare / predicates | 8 | 0 | 保留原后端 |
| allocate | 32 | 1 | 下述原有 PVC 不稳定案例 |
| enqueue / backfill | 6 | 0 | 原生动作回归 |
| vGPU / RF | 260 | 0 | 27 顶层测试，含真实 Python 服务、原有冻结消融、CEL、Linux 平台检查；无 skip |
| 离线时间采集器（Python，另计） | 3 | 0 | 合成时间戳 fixture，不是新集群结果 |

`TestAllocateWithPVC/static_pv_matched_and_node_with_enough_resources` 偶发预期 Binding=2、实际为 0。当前整包一次失败、单独运行曾通过；改前冻结基线整包曾通过，但随后 **同一原有 PVC 顶层测试重复 20 次有 6 次失败**。当前整包重复三轮也有一次相同失败。源码注释已说明该测试涉及 mock PV/controller 时序，但本轮没有证明具体根因，不把它武断归咎于 sync.Once，也没有改变或弱化原测试断言。

保留 `actions-allocate-tests.jsonl`、`allocate-current-repeat.jsonl`、`allocate-baseline-full.jsonl`、`allocate-baseline-repeat.jsonl`、`allocate-baseline-pvc-repeat.jsonl` 及单独运行日志。基线通过 Go overlay 恢复三个原文件并屏蔽新增 guard 文件，未覆盖工作区。**不能宣称整个扩展 Go 套件全绿。** 重新执行主脚本会保留非零退出码，但继续运行其他包以提供完整结果。

完整统计：`test-summary.json/csv`、`diagnostic-test-summary.json`。初次命令行/编译调试记录不作为通过证据；HRRN 包使用 shuffle=42、count=3 重新运行，**三轮共 69 个叶 case 全部通过、无跳过**，单独保存在 `hrrn-repeat.jsonl`，未覆盖五组主结果。

## 6. 交付与复现

主要文件：

- `scheduler/volcano-rf/pkg/scheduler/plugins/hrrn/{hrrn.go,rollback.go,hrrn_test.go,manifests_test.go}`。
- `pkg/scheduler/framework/job_plan_guard.go`、对应测试及 Session 的小范围修改；factory 注册插件。没有改 allocate/设备/RF 算法。
- `source.patch` 可应用于上述冻结基线；已做反向 `git apply --check` 检查。
- `bin/vc-scheduler-hrrn` 已生成 Linux ELF；`scheduler-build.log`、`binary-build-info.txt`、`binary-source-inputs.json`、`scheduler-binary-sha.json` 记录编译与摘要。执行 `--help` 已验证可启动参数解析，没有连接集群。二进制 SHA256：`dbedbd61bb774a12c276f520d6fcdf990d262c8fb46cec57f59e098bede695fd`。
- `k8s/hrrn-stage4/` 包含五种配置、独立 namespace/RBAC/Queue/配额、**replicas=0** 的 scheduler 草案、六个 finite sleep Job、Dockerfile、元数据协议和集群待执行 RUNBOOK。
- `scripts/collect_hrrn_timing.py` 只读本地导出的 JSON/日志，将排队、调度决策、Pod 启动、容器运行、完成及逻辑释放分列；未测到的值留空。A/B/C 正式比较用同源外部完整就绪观察，不将 Pod 创建时间当作 ready_at。

在项目根目录，已有 Go/WSL/Python 工具链的本机复现：

```powershell
# 生成准备材料；只写本地文件
.venv/Scripts/python.exe scripts/prepare_hrrn_artifacts.py
# 启动专用本地冻结 RF 服务，运行所有原生测试，结束时关闭自己启动的服务。
# 已知 PVC 不稳定测试若失败，命令会明确非零退出，不抹去失败。
./scripts/run_hrrn_native.ps1
# 构建可单独执行；不构建镜像、不推送、不部署
./scripts/build_hrrn_native.ps1
.venv/Scripts/python.exe -m pytest tests/test_hrrn_stage4_artifacts.py -q
# 调试原有 PVC 基线问题
./scripts/test_hrrn_baseline_pvc.ps1
```

`run_hrrn_native.ps1` 使用现有本地 WSL `Codex-RF-Verify-20261019` 和 `.tools/go1.26.3`。测试 RF 服务仍绑定 Windows loopback；额外传输桥仅绑定本地 WSL 私网适配器并只接受该 WSL 客户端地址。它不是部署组件。其他 Linux 环境可直接在源码目录以 go test 运行相同包，并按测试提示设置本地 RF/manifest/冻结 plan 环境变量。测试日志详细记录实际执行，交叉编译成功本身不算测试通过。

## 7. 已实现与未实现的论文边界

可写入论文：原生排序流程分析；保持 Priority、Gang、DRF、proportion 前置约束的解释性 HRRN；明确完整成员就绪协议和同 Session 时间快照；完整规划、失败回滚及原生旁路适配；冻结 RF 设备选择复用；Go/fake-client 的执行路径差异和失败案例。

暂不能写为已实现/已验证：跨 Pod API 原子提交；完整 GPU 组合的全局最优或完备搜索；跨进程持久等待；真实设备插件的并发释放正确性；真实 Kubernetes 容器先后；真实 GPU 性能；大样本队列等待/吞吐/公平性提升；生产部署就绪。镜像尚未构建，真实集群未执行。原有 PVC 回归问题也尚未修复。

固定容量保护和 EASY 未接入真实 Volcano，PPO/DQN/GNN 训练与开发已停止。下一步应先解决/隔离原生 PVC 测试不稳定因素、审核专用节点及恢复路径，再按 RUNBOOK 做小规模 sleep 执行顺序验证；不因本轮功能案例成功就追加算法复杂度或替换生产 Scheduler。
