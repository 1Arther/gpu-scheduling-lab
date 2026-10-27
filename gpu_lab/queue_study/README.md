# 第二阶段 HRRN 队列研究：最小可运行实验

仅本地 CPU 离散事件仿真。所有执行时长和工作量均为合成量，不是实测训练/推理性能。第一研究点的 RandomForest 模型、特征、标签、评分服务和第一阶段队列代码保持原样；第二阶段通过独立模块复用接口。

## 运行流程

在项目自带 Python 环境执行；没有新增第三方依赖。

```powershell
Set-Location 'C:\Users\EDY\Documents\ChatGPT\vGPU调度\gpu-scheduling-lab'
$env:OMP_NUM_THREADS='1'
$env:OPENBLAS_NUM_THREADS='1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'

# 不调用旧项目训练流程的测试组合
.\.venv\Scripts\python.exe -m pytest -q tests/test_queue_study.py tests/test_queue_sim.py tests/test_resources.py

# 路径必须是新目录；先开发，后冻结，最后正式运行
.\.venv\Scripts\python.exe -m gpu_lab.queue_study develop --output results/my-stage2-dev --workers 4
.\.venv\Scripts\python.exe -m gpu_lab.queue_study profile --output results/my-stage2-dev
.\.venv\Scripts\python.exe -m gpu_lab.queue_study freeze --development results/my-stage2-dev --output results/my-stage2-test
.\.venv\Scripts\python.exe -m gpu_lab.queue_study test --output results/my-stage2-test --workers 4
.\.venv\Scripts\python.exe -m gpu_lab.queue_study analyze --development results/my-stage2-dev --output results/my-stage2-test

# 校验已保存结果的完整确定性摘要
.\.venv\Scripts\python.exe -m gpu_lab.queue_study replay --output results/my-stage2-test --seed 900001 --condition high_load__independent__under --policy hrrn_easy
```

`--workers 1` 为串行执行。并行只分割独立轨迹，单条轨迹仍是确定性的单线程仿真。正式结果中的墙钟耗时会受 CPU 竞争影响；单独的 `profile` 阶段在单进程下比较开销。

冻结阶段保存开发选择、独立测试输入和评价源码、RF/后端的 SHA256。正式执行前后及分析/重放时均核验；变更后不能静默沿用旧冻结集。正式结果位于本轮 `results/hrrn-stage2-20261026/holdout/`。

## EASY 的具体语义

`easy.py` 只接收 PublicJob、当前真实账本、已观察到的运行作业及其公开预计截止时间；不接收仿真器或其事件堆。

1. 按 HRRN 处理队首，可完整启动时正常提交；遇到第一个受阻且可预约的作业，保留一个预约对象。
2. 用当前时刻和全部运行作业的预计截止时刻构造时间日历；逐个候选时刻检查完整同节点 GPU 组合，最早可行时刻用原有 RF 排序选设备。
3. 回填先满足当前真实的显存/算力/槽位、不同物理 GPU、节点和芯片硬约束，再检查其预计占用是否使预约组合在预约时刻仍完整可执行。
4. 短作业可在预约前结束；长作业也能回填，只要与预约所需的三维容量可共同容纳。使用其他 GPU 的作业同样可继续执行，不用整卡或整节点阻塞制造弱基线。
5. 预约所有者优先启动；取消或目标不可用时清理。提前完成、运行超估计和其他已观察事件都会重新计算最早时刻及组合。更新和预约推迟有显式日志。
6. 到预计截止仍在运行时，预测延长为 `now + max(提交估计, 已运行时长, tau)`。这是明确的无先知扩展规则，可能产生预约延迟，不作绝对时间保证；不会提前物理释放真实资源。

区别：空间容量保护在当前时刻逐维禁止消耗所需余量；EASY 按估计日历判断未来是否影响预约，可以暂时消耗随后预计归还的容量。两者均通过同一个 `Cluster.commit` 和原 `GPUState` 账本完整提交，所有七种策略使用同一 RF 放置接口。

`placement.py` 的缓存保存原冻结接口的确定结果，不改分数；所有资源、任务实例类型、设备健康、节点及请求约束都是键。UID、allocation ID 和 revision 不参与 RF 打分，命中时会重建当前物理 revision 及保护 epoch。测试同时比较缓存与未经缓存的冻结接口，包括异构多卡和多槽位请求。

## 七种对照

FIFO、SJF、原始 HRRN、HRRN+年龄优先、HRRN+EASY、HRRN+整卡保护、HRRN+细粒度 vGPU 保护。FIFO/SJF/HRRN 均允许完整可行旁路。整卡与细粒度使用相同开发阈值，区分资源粒度；年龄对照也使用该阈值。

细粒度保留第一阶段核心：最多一个保护对象、固定完整目标 GPU、三维逐维保护、凑齐后优先执行。调参只改变触发阈值 `min(H_abs,kappa*max(estimate,tau))`，不改变 RF 评分、目标组合选择和已运行作业。

## 工作负载与参数选择

新的 `hrrn-stage2-20261026-v1` 命名空间与 SHA256 派生随机流，不读取第一研究点或第一阶段的实验轨迹。

| 维度 | 设置 |
|---|---|
| 集群 | 两节点各两 GPU，RTX4090 与 L40，共用已有异构模型 |
| 轨迹长度 | 每条24个有限、不可抢占 Job，提交结束后排空 |
| 请求 | 显存/核心配额/实例槽位，1或2卡，训练/推理标签与请求规模独立 |
| 到达与混合 | steady、bursty、large_heavy、small_heavy、multi_heavy、low_load、high_load、overload |
| 资源与实际合成时长 | 独立、正相关、负相关 |
| 时长估计 | 名义诊断、0.5倍低估、2倍高估、对数随机误差 |
| 开发集 | 4种子×8条件，12组 H_abs/kappa，仅此集选择参数 |
| 正式集 | 20独立种子块×33条件×7策略=4620次；660条输入 |

不是完整全因子：8场景×3相关性使用随机误差；高负载场景另补3相关性×名义/低估/高估。误差实验只在高负载的完全配对条件间比较，不能声称已覆盖所有场景的全部误差组合。

每个种子块包含全部条件；同一条件的策略共享到达、资源、预计时长、实际合成时长。同一场景/相关性下不同误差条件也共享基础轨迹。低负载目标逻辑核心负载0.2，高负载1.6，暂时超载1.15并将大部分到达集中在前段，其余默认0.8；这些是生成器的 offered load 参数，受多维碎片等影响，不是实际利用率。

开发网格：H_abs={0.5,2,6,12}，kappa={0.5,2,8}。先满足相对 HRRN 的工作量不低于98%、平均bounded slowdown不高于105%、小作业P95不高于110%+0.25；在合格者中按预先记录的 P95 与长等待目标选择。如果无人合格，保留最少违反者并明确报告失败。目标和规则保存在开发运行前的 protocol 文件中，正式集不再调参。

## 指标与解释

- 等待从 `ready_at` 起算，报告全体及小/中/大、多卡/单卡的均值、P95/P99、长等待比例；长等待阈值在开发前固定为8。
- 作业规模为相对集群最小设备三维容量的主导请求乘 GPU 数；大作业≥1，小作业≤0.4。此“大作业”定义基于总需求，不等于所有大显存单卡请求；与多 GPU 分组有重叠。
- P95/P99 是每条轨迹的分位数再宏平均，不是把所有 Job 混在一起求分位数；24个 Job 的 P99 靠近最大值。
- bounded slowdown 的评价下限固定为1，所有策略相同；完成工作量为完整完成 Job 的 GPU数×核心配额×合成时长，不是物理 FLOPs。
- 完成 Job 数和工作量按同一固定观察窗计算；等待按全部排空作业计算。正式有限轨迹要求全部完成，不丢弃长等待者。
- 实际逻辑分配率、空间保护空闲容量时间和空闲保护率分别报告；EASY 的未来预约不计成当前空间保护。当前闲置的效率影响仍反映在实际分配率和完成工作量中。
- 主统计按场景等权，先在种子内平均场景内条件，再平均8场景；避免额外估计误差条件提高高负载权重。20个完整种子块配对bootstrap，输出均值、标准差、范围、95%区间。
- 三个主要指标（P95、长等待比例、完成工作量）分别对 HRRN/EASY/整卡作比较，共9项，另给 Bonferroni 校正的近似bootstrap区间；其余为探索性分析。
- 保护诊断记录已积累比例、固定组合预测等待、最早其他组合预测等待及其差，按预测等待≤2或>2区分接近/远离资源凑齐。预测不是实际释放的先验信息。

## 文件与验证

`simulator.py` 是第一阶段引擎的子类，`easy.py` 不接触环境私有 Job；`audit.py` 在独立资源账本回放上重新检查预测、预约与每次安全回填。工程测试包括多卡完整提交、三维预约/保护、取消/失效/释放、版本、复现、同一后端、缓存等价及无未来实际完成信息的前缀一致性。

正式输出：`freeze.json`、`verification.json`、全部 `runs.csv`、均值/波动/CI 的 `summary.csv`、配对比较、逐场景/条件结果、每Job压缩记录。`details/` 保存事前指定首个种子块的完整事件；其他轨迹仍全部经过内存独立回放，并保存确定性摘要，支持 `replay` 验证。

`figures/` 同时提供 PNG/SVG：等待与完成工作量、规模尾部、实际分配与空闲保护、开发参数敏感性、估计误差、失败场景、相关性、串行开销、近/远保护成本。

范围仍为单租户同优先级、同节点多卡，CPU/主机内存充足，不实现抢占、多Pod原子绑定或生产规模集群。没有重新训练第一研究点，没有修改真实 Kubernetes/Volcano，也没有运行 GPU 计算任务。
