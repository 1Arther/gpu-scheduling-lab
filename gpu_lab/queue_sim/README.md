# HRRN 与长等待资源保护：第一阶段队列仿真 MVP

研究定义来自 `docs/HRRN公平调度设计方案_20261023.txt`，实现与芯片感知模块分离。仅加载已有 `models/pod-gpu-rf-v1` 的 RandomForest 模型并调用 `SuitabilityService.rank_gpus`，复用 `GPUState.can_allocate/allocate/release`。不训练、替换或修改原有模型与放置服务。

全部到达、执行时长和结果均为**合成时间单位**，不能解释成真实训练/推理时延或 GPU 性能测量。

## 运行

以下 PowerShell 命令在已有项目环境中执行。`.venv` 使用项目 `requirements.txt` 的依赖，没有新增依赖。

```powershell
Set-Location 'C:\Users\EDY\Documents\ChatGPT\vGPU调度\gpu-scheduling-lab'
$env:OMP_NUM_THREADS='1'
$env:OPENBLAS_NUM_THREADS='1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'

# 队列功能测试 + 已有资源账本测试；此组合不训练 RF
.\.venv\Scripts\python.exe -m pytest -q tests/test_queue_sim.py tests/test_resources.py

# 两张 GPU 的最小机制案例，六种方法使用同一轨迹、估计与 RF 放置
.\.venv\Scripts\python.exe -m gpu_lab.queue_sim --case blocking --output results/hrrn-local-blocking

# 五类功能案例 × 六种方法，每次运行均独立审计
.\.venv\Scripts\python.exe -m gpu_lab.queue_sim --case all --output results/hrrn-local-all

# 使用保存的完全相同输入重放
.\.venv\Scripts\python.exe -m gpu_lab.queue_sim --trace results/hrrn-local-all/blocking-input.json --policy hrrn_protect --output results/hrrn-local-replay
```

输出目录必须尚不存在，避免覆盖旧实验。可用参数由 `python -m gpu_lab.queue_sim --help` 列出。正式交付结果在 `results/hrrn-mvp-20261023/final`；`fixtures`、`validation` 是本轮早期开发检查，`final-replay` 是最终版本的相同输入复现。

## 六种策略

| CLI 名称 | 排序和旁路 | 等待时的保护 |
|---|---|---|
| `fifo` | ready 时间、Job ID | 无 |
| `sjf` | 预计时长、ready 时间、Job ID | 无 |
| `hrrn` | 响应比降序、ready 时间、Job ID | 无 |
| `hrrn_age` | 超阈值者按 ready 年龄优先，其余 HRRN | 无 |
| `hrrn_protect` | HRRN；可启动的保护对象先执行 | 固定目标 GPU 上所需的显存、算力和实例槽位 |
| `hrrn_whole_gpu` | 与细粒度保护相同 | 固定目标整卡容量，作为粗粒度消融 |

全部允许跳过当前不可完整执行的 Job，继续检查后续作业；FIFO 也是带完整可行旁路的 FIFO。所有策略均调用同一冻结 RF 接口，不可通过换放置器获得收益。

`W=t-ready_at`，`HRRN=1+W/max(estimated_service,tau)`。保护触发阈值为 `min(H_abs,kappa*max(estimated_service,tau))`。默认 `tau=0.1, H_abs=0.5, kappa=2`，只是机制案例参数；评价长等待阈值独立固定为 4。

超过阈值后，从有完整总容量可行计划的作业中选择 ready 最早者。全局最多一个保护对象，在该保护 epoch 内固定节点和完整 GPU UUID 组合。选定目标后，只在这组 GPU 同时满足全部请求时启动，其他可行组合后来变空也不换卡。对象取消或目标不可用时终止旧 epoch 并重新选择。

目标 GPU 的真实空闲向量为 F，保护请求为 R，后来作业只能使用逐维 `max(F-R,0)`。真正被保护且空闲的量是 `min(F,R)`；不能把尚未释放的在用容量再计为空闲。实际 RF 评分使用真实账本，保护约束在候选组合过滤时单独应用，被过滤的最高分候选之后仍会检查其他合法候选。总容量查询使用只读的空账本视图，不修改物理占用。

提交时同时核对所有目标 GPU 的资源版本和保护 epoch，逐卡暂存账本，全部通过后一次发布；不存在部分成功的 Job。取消/完成时完整释放。仿真单线程顺序执行，不提供真实分布式事务或跨 Pod 绑定。

## 输入及生命周期

`Job` 提供 `job_id, arrival_at, ready_at, task_type, estimated_service, actual_service, request, gpu_count`，可选 `allowed_profiles, node, tenant, priority, estimate_source, cancel_at`。`request` 是每张目标 GPU 上的相同聚合请求，含 `memory_mib, compute_units, instances`；多 GPU 请求使用不同物理 GPU 且必须在同一节点。

`actual_service` 是环境私有的合成时长，只用于启动后的完成事件。排序和放置只接收不含实际时长、未来取消时间的 `PublicJob`。第一阶段要求提交正的有限预计时长，并明确记录其来源；未知时长的历史估计/回退服务尚未实现。芯片适配评分从未转换成运行秒数。

示例（Python API）：

```python
from gpu_lab.resources import ResourceRequest
from gpu_lab.queue_sim import Job, Cluster, Config, FrozenPlacement, Simulator
from gpu_lab.queue_sim.scenarios import two_gpu_fleet

jobs = (Job('demo', 0, 0, 'training', 2,
            ResourceRequest(memory_mib=4096, compute_units=30, instances=2),
            gpu_count=2, actual_service=3),)
fleet = two_gpu_fleet()
result = Simulator(Cluster(fleet), jobs, FrozenPlacement(), 'hrrn_protect', Config()).run()
```

初始集群必须为空；已有占用通过 t=0 的合成背景 Job 表达。状态依次为 `not_arrived → not_ready → waiting → running → completed`，另有 `rejected/cancelled`。输出含 `start_at, finish_at, released_at, allocation_active`、完整分配和等待原因。完成与释放在同一事件内发生；运行中取消也在该事件内完整释放。

事件使用稳定堆排序，同一时刻按完成、设备可用性、到达、就绪、取消、阈值的顺序全部处理，再运行一次调度。输入 Job 事件按 Job ID 插入，后续完成事件按确定的启动顺序插入，同阶段按插入序号执行；完成与取消同刻时完成优先。设备不可用表示停止新分配，已有作业仍按合成结束时刻释放；不将其解释成真实故障恢复。

## 输出与审计

| 文件 | 内容 |
|---|---|
| `*-input.json` | 明确标为 synthetic 的输入、完整时长、设备和配置，可重放 |
| `*-策略.json.gz` | 逐作业生命周期、事件、真实账本、保护目标/版本、HRRN 与资源积分 |
| `summary.csv` | 所有策略的主要指标和三维资源指标 |
| `verification.json` | 独立事件回放对资源上界、保护、完整提交和释放的检查 |
| `manifest.json` | 原模型/源码、输入、新模块源码哈希和模型版本 |
| `runtime-diagnostics.json` | 每次调度的真实墙钟耗时，单独保存，不影响合成时钟或确定性结果 |
| `report.md` | 本次运行说明与最小案例表 |

`audit.py` 重新维护整数资源账本，不调用仿真器的分配/释放实现，逐事件核对三维容量、固定组合、保护约束、真实合成完成时刻、版本、整作业释放和资源时间积分。随机种子的复现比较排除墙钟耗时与 gzip 文件头，只比较解压后的完整结果。

平均等待、P95 等待和长等待比例覆盖到达停止后排空的全部就绪、非拒绝作业；未启动的取消/待定作业显式报告删失，主均值和 P95 返回 null，另给已启动样本值及观察下界。结构不可行请求单独拒绝，不持有保护。

完成数量和资源积分使用固定窗口 `[0, observation_end]`，默认 `[0,12]`；同时输出排空完成数，避免把推迟完成当成丢弃。没有超时丢弃或 TTL。

按显存、算力配额、槽位分别计算：

- `logical_allocation_rate`：实际分配容量时间 / 配置总容量时间。
- `logical_protection_reservation_rate`：受保护空闲容量时间 / 配置总容量时间。
- `logical_reserved_rate`：以上两项之和，分配占用与保护空闲不重复计数。
- `protected_idle_capacity_time`：分别以 MiB×合成时间、算力配额单位×合成时间、槽位×合成时间给出。

这些都是逻辑资源指标，不是实测 GPU 利用率。

## 已验证边界及后续工作

已覆盖阻塞、保护取消、结构不可行、目标失效和种子复现五类机制用例。最小案例的大作业等待减少，不能代替统计意义上的整体效果；小作业等待、窗口完成数量和空闲保护成本必须一并报告。当前输出不宣称优于同粒度 EASY。

第一阶段限单队列、单租户、同优先级、有限非抢占作业，假定 CPU/主机内存充足。算力必须为正；100 核心请求遵守空卡排他条件。多槽位聚合需求由原资源接口硬检查，但冻结 RF 接口仍按一次聚合设备请求评分；尚无原生多容器 HAMi 或跨 Pod Job 的事务映射。

后续尚未实现：同 vGPU 粒度 EASY 时间预约强对照、正式多负载/估计误差/多随机种子效果实验及置信区间、历史运行时估计服务、多租户 DRF 与保护额度、Volcano 集成、PPO/GNN。未修改线上 Kubernetes，未部署调度器。
