# 第五轮：可复用 Pod–GPU 分类与选择接口

默认版本 `rf-v4-known-states-ee35cfabe30f`，冻结第四轮known_states模型，未重新训练。4090/5090/L40均在拟合型号和数值覆盖中，初始状态相关验证通过。选择依据见 [模型清单](frozen-model-manifest.json)，不是按最高测试准确率选择。

所有分类仅表示合成FP32参考合同适配性，不是真实GPU性能保证。GPU UUID和节点使用SIM前缀的离线身份，不是本次查询的真实设备。

## 已执行的演示

| 场景 | 推荐节点 | UUID | 原因 |
|---|---|---|---|
| training | sim-node-l40 | SIM-GPU-l40-00 | all hard filters passed; maximum explainable node placement score, stable UUID tie-break |
| inference | sim-node-rtx5090 | SIM-GPU-rtx5090-00 | all hard filters passed; maximum explainable node placement score, stable UUID tie-break |
| unlabelled | sim-node-rtx4090 | SIM-GPU-rtx4090-00 | ordinary binpack: no type inference |

training示例请求36864MiB/50配额，仅L40满足显存；inference示例请求4096MiB/20配额，比较所有11卡的推理提示、带宽、共享、装箱与平衡因素。候选、过滤原因、RF概率与实际排序分数都在JSON中。

| 动态事件 | 剩余显存MiB | 剩余配额 | 实例 | 综合分类 | 状态 |
|---|---|---|---|---|---|
| initial | 45776 | 100 | 0 | 训推一体型 | provisional |
| allocate_compute_heavy | 44776 | 40 | 1 | 训练优先型 | provisional |
| allocate_memory_heavy | 6000 | 35 | 2 | 当前不满足参考负载要求 | provisional |
| release_compute_heavy | 7000 | 95 | 1 | 推理优先型 | provisional |
| release_all | 45776 | 100 | 0 | 训推一体型 | provisional |

每步均重新进入分类接口并返回新状态的分类；相同特征允许命中进程内缓存。静态硬件不随分配改变。每步还对完整11卡重新排列后续训练/推理Pod。

## 接口与安全边界

`gpu_lab.runtime.classify_gpu(snapshot)` 与 `rank_gpus(pod,gpus)` 为稳定入口；也可持有SuitabilityService一次加载模型反复调用。dict/JSON是交互边界，Pod支持mapping/YAML文本/Path。资源缺失、非法Label、无效GPU输入分别明确报错或回退。

先执行后端、健康、插件管理、配额语义、显存、算力、实例、100-core独占、节点与卡型条件检查，再打分。四分类不会批准超额请求，也不会拒绝所有比参考负载更小的Pod。未知/低可信型号和模型故障使用保守理论排序，字段ranking_basis、rf_probability、fallback_score分开，不将回退分数伪装为概率。

Pod无Label但有vGPU请求时使用普通装箱，保留unknown类型，绝不默认为training/inference；无vGPU请求时交回普通调度器。非法Label明确错误。

解析兼容本地自定义片段的limits触发、memory/cores从requests补取、memory-only默认一张、无显存默认100%、cores默认0。0核是无保证限额语义，不能当作已预留算力；存在该类实例时RF只作诊断。资源字段使用整数，offline约定MiB；真实插件MB/MiB转换仍需在完整源码/配置确认后接入。

vgpu-number>1在单个container内选同一节点的不同物理GPU；跨container可以在资源允许时共用一张GPU。计划是确定性的贪心可行方案，不保证求出所有可能的多容器组合；失败时安全交回原调度器/重试。RF旧输入计数仍按vGPU实例，独立Pod计数另存running_*_pod_count。

GPUPool是进程内模拟账本：RLock+设备revision校验、全计划原子预留、真实UUID结果确认、幂等释放、失败回滚、未提交预留过期回收。它不构成多进程或集群锁。真正集成时Volcano是唯一账本权威，Python只给建议，不双重计账。

## 仿真兼容性

- binpack: 完成60个Pod，API选择95次，分类更新120次，无超分配，全部资源恢复。
- static_rf: 完成60个Pod，API选择71次，分类更新120次，无超分配，全部资源恢复。
- dynamic_rf: 完成60个Pod，API选择134次，分类更新120次，无超分配，全部资源恢复。

同一60任务序列/seed101与旧独立合成执行模型seed707。未改原仿真器，新增RuntimeScheduler通过choose/on_state_change接入；扣减与释放只由旧仿真器负责。

## Volcano集成状态

本地可读Go片段8个，缺go.mod与Allocate/addResource定义。已确认deviceshare过滤/节点排序调用、设备解析/排序/试分配、UUID编码；不能证明是完整v1.13.1定制二进制对应源码。Go插件没有编译或部署。详见 [集成设计](../../docs/runtime-volcano-integration.md) 与 [源码审计](volcano-local-source-audit.json)。

推荐后续采用单个常驻本地Python推理服务，Go通过有deadline的JSON请求调用，复用本模块dispatch。当前提供Python API、JSON-lines worker和模拟适配器，没有启动HTTP服务或修改在线DQN配置。异常/超时/版本错误回退确定性策略；评分UUID必须传入最终分配计划，在原账本锁下重校验，不可重新随机选卡。

## 产物与复现

- [训练Pod演示](demo-training.json) / [推理Pod演示](demo-inference.json) / [动态演示](demo-dynamic.json)
- [缺Label回退](demo-unlabelled.json) / [模型失败回退](demo-model-failure.json) / [Volcano模拟适配器](demo-volcano-mock.json)
- [GPU输入示例](gpu-list.json) / [仿真回归](simulation-regression.json) / [本机API开销](inference-overhead.json)
- [历史结果完整性](historical-results-preserved.json)

在项目目录运行：

```powershell
$env:TEMP=(Join-Path (Get-Location) '.tmp'); $env:TMP=$env:TEMP
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
.\.venv\Scripts\python.exe -m gpu_lab.runtime.cli demo --output results/offline-v5-replay
.\.venv\Scripts\python.exe -m gpu_lab.runtime.cli rank --pod examples/runtime/inference.yaml --gpus-json results/offline-v5-replay/gpu-list.json
.\.venv\Scripts\python.exe -m pytest -q --basetemp .tmp/pytest-round5-replay
```

## 最终实际验证

全部历史及本轮 pytest：147 项通过，failures=0，errors=0。

- [pytest执行原文](pytest-results.txt) / [XML记录](pytest-results.xml)
- [冻结模型、源码、历史哈希及排名重放验证](verification.json)
- [真实JSON-lines子进程UTF-8测试](json-lines-smoke.json)

验证脚本：`python scripts/verify_offline_round5.py --results results/offline-v5-final`。
