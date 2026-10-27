# GPU Scheduling Lab

基于 GPU 硬件画像与动态 vGPU 逻辑配额的训练/推理适配分类和 Volcano 设备选择实验项目。

## 项目内容

- `gpu_lab/`：数据集、RandomForest 分类、Pod–GPU API、HTTP 推理服务、调度仿真，以及后续队列公平性/HRRN 研究代码。
- `models/pod-gpu-rf-v1/`：已训练的默认 RF 模型、特征顺序、标签合同和版本摘要；推理时直接加载。
- `scheduler/volcano-rf/`：基于 Volcano v1.13.1 的完整 Go 源码快照，含 rf-job、原生分配测试及当前 HRRN 开发代码。
- `tests/`、`scripts/`、`k8s/`：自动化测试、实验工具、部署草案和 sleep Pod 示例。
- `data/`、`results/`、`reports/`：公开参数来源、保留的模型/评价结果、实验汇总和图表生成代码。

## 主要实验入口

- [GPU 分类学习讲义正文与图表源文件](reports/gpu-classification-study-20261026/book.txt)
- [第四轮分类与跨型号测试](results/offline-v4/report.md)
- [RF 独立贡献消融](docs/rf-ablation-native-validation.md)：180 组配对原生 Go 离线测试中，37 组最终 UUID 改变。
- [第八轮真实 Kubernetes 报告](docs/round8-real-kubernetes-report.md)：同一张 4090 完成五状态动态路径，完整窗口 8 次分配的 UUID 和逻辑配额核验通过。
- [Go–Python 协议](docs/rf-job-protocol-v1.md)

分类评价使用合成理论标签；真实 Kubernetes 实验运行 sleep，验证设备选择与逻辑配额，不代表训练/推理计算性能测量。历轮记录保留当时结论，以对应的最终报告为准。

## 本地启动

使用 Python 3.11 或 3.12，在项目根目录创建独立环境：

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m gpu_lab.runtime.cli demo --output .tmp/github-demo
python -m gpu_lab.runtime.http_server --port 18081
```

模型服务只提供推理；不会自动部署 scheduler 或创建 Kubernetes Pod。Volcano 原生 Go 测试应在 Linux 下执行，依赖和命令见各轮报告。队列 RL 扩展另有依赖说明，见对应模块 README。

`k8s/` 与集群操作脚本含实验环境专用名称和历史审批文件，不能直接用于其他集群。历史授权不构成新的线上操作许可。

## 发布范围与来源

本仓库是现有工作目录的代码快照，完整保留自研 Python/Go 源码及测试，包括尚未提交到原内嵌 Git 仓库的 HRRN 修改。原工作目录和原 Git 历史均保留在本地，未被覆盖。

虚拟环境、工具缓存、已编译二进制、重复压缩包、文档渲染中间件、未筛选的集群盘点，以及超过 10 MiB 的单个生成结果不随 Git 上传；可用脚本再生成。详见 [来源记录](publication/source-provenance.json)、[文件摘要](publication/snapshot-files.json) 和 [排除清单](publication/excluded-files.json)。源代码不受生成结果大小限制。

Volcano 上游许可证保留于 [scheduler/volcano-rf/LICENSE](scheduler/volcano-rf/LICENSE)，第三方归属及 notices 保留原样。

## 历史开发记录

以下保留原 README，可能描述对应轮次尚未完成的准备工作。


## 第七轮：真实 Kubernetes sleep 验证已完成，原scheduler已恢复

[真实集群实验报告](docs/round7-real-kubernetes-report.md) · [真实证据核验](results/round7-live-window-20261019/verified/summary.json) · [逐卡评分CSV](results/round7-live-window-20261019/verified/candidate-scores.csv)

在用户批准并确认协调窗口后，临时将原DQN scheduler缩到0，使用独立RF完成6个真实sleep Pod的逻辑分配与清理，原scheduler已恢复1/1 Available，RF为0。原配置、Deployment spec与二进制校验一致；未训练模型、未执行CUDA。

RF/原生/插件响应三方UUID一致5/5（故障回退另计），原生/插件一致6/6。后续Pod读到了分配和释放后的逻辑状态。本次四张4090均预测balanced，RF on/off虽相差0.3评分，最终UUID未变；不宣称本次已证明RF独立选卡收益或真实性能提升。Linux CPU回归97项通过。原始采集失败/回退解析误判和自动恢复记录均保留，详见报告。

以下为审批前的历史准备记录，不能当作当前运行状态。

## 第七轮：4090真实逻辑分配的最后复核（尚未部署）

最新入口：[最小待批准操作](docs/round7-final-approval.md)、[39项Linux准备测试](results/round7-final-review-20261019/linux-preparation-tests.txt)、[YAML草案](k8s/round7-logical-window/README.md)。本轮新采样未发现活跃GPU/vGPU/DRA请求或待消费分配，原DQN未变；不要求清空物理显存。优先评估管理员协调的短时提交窗口，集群级准入不作为默认写操作。

仍待确认协调范围和旧在途预留、构建固定新镜像以及明确集群写授权；真实Pod未执行。默认准备包不包含DQN修改或VAP/Binding。旧逻辑评估、手册及第六轮源码/模型/结果均保留；旧强制准入步骤由最新复核覆盖。

## 第六轮：真实 Volcano Go 设备选择与本地 RF 服务

独立源码在 `scheduler/volcano-rf`，分支 `codex/rf-job-v1.13.1`。新增默认关闭的
`rf-job`，复用冻结模型和原生 HAMI-core 逻辑账本；不读取物理空闲显存，不部署线上。
本轮已包含 Go→Python HTTP、原生 `GPUDevices.Allocate` UUID 注解及分配/释放测试；
这不同于真实 Kubernetes 集群验证。

```powershell
# 终端一，在本项目目录启动常驻服务
.\.venv\Scripts\python.exe -m gpu_lab.runtime.http_server --port 18081
# 终端二
.\scripts\test_round6.ps1
.\.venv\Scripts\python.exe scripts/summarize_round6.py
```

接口与单位见 [协议](docs/rf-job-protocol-v1.md)，实现、限制和后续部署回滚步骤见
[第六轮报告](docs/rf-job-round6-report.md)，真实离线输出见
[测试结果](results/integration-v6/test-results.md)。第五轮以下文字为当轮历史记录；
第六轮已有完整可编译 Go 源码和常驻 HTTP 服务，5090 仍保留模型待验证及保守回退限制。

## 第五轮：可调用的 Pod–GPU 选择接口

新增 `gpu_lab/runtime/`，冻结第四轮 `known_states` 为默认模型，覆盖4090/5090/L40。
模型文件、特征顺序、参数、数据版本与标签合同在 `models/pod-gpu-rf-v1/`；运行时不训练。
所有结果仍属于合成参考合同适配性，不能作为真实性能保证。

```python
from gpu_lab.runtime import classify_gpu, rank_gpus
classification = classify_gpu(gpu_snapshot)
ranking = rank_gpus(pod_yaml_text_or_mapping, gpu_snapshots)
```

也可创建一个 `SuitabilityService` 持续调用，joblib只加载一次。GPU按UUID独立维护状态；
API返回硬过滤原因、分类、RF边际值、独立标记的保守回退得分、排名、节点和明确UUID计划。
无任务Label走普通装箱，非法Label明确错误；四分类不会覆盖具体Pod的资源硬约束。

`GPUPool`提供进程内CAS原子预留、UUID确认、幂等释放、回滚与过期回收；它是离线模拟账本，
真实集成仍由Volcano独占资源记账。多vGPU在同container内按本地源代码选择同节点不同物理卡。
两种实例计数与独立Pod计数分开。API内存单位约定MiB，真实插件单位还需完整源码确认。

本轮交付：[报告](results/offline-v5-final/report.md)、
[Volcano源码审计与集成设计](docs/runtime-volcano-integration.md)。只找到自定义Go片段，
缺完整模块和Allocate实现；没有声称Go插件已经编译/部署，也不复用旧DQN策略。
示例YAML仅为离线解析输入，镜像是占位字符串，不用于本轮部署。

在项目目录复现（输出目录须为新目录）：

```powershell
$env:TEMP=(Join-Path (Get-Location) '.tmp'); $env:TMP=$env:TEMP
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
.\.venv\Scripts\python.exe -m gpu_lab.runtime.cli demo --output results/offline-v5-replay
.\.venv\Scripts\python.exe -m gpu_lab.runtime.cli rank --pod examples/runtime/inference.yaml --gpus-json results/offline-v5-replay/gpu-list.json
.\.venv\Scripts\python.exe -m gpu_lab.runtime.cli classify --gpu-json results/offline-v5-replay/gpu-one.json
.\.venv\Scripts\python.exe -m pytest -q --basetemp .tmp/pytest-round5-replay
```

`python -m gpu_lab.runtime.cli json-lines` 是常驻JSON worker，stdin每行一个带
`schema_version: pod-gpu-api/v1`的classify_gpu/rank_gpus请求。尚未启动HTTP服务或连接集群。

## 第四轮：NVIDIA 未见型号泛化

新增代码在 `gpu_lab/nvidia/`，复用前三轮账本、理论标签和仿真；旧代码和结果保留。
数据库为15款独立NVIDIA型号/配置，新增 RTX A6000、RTX6000 Ada、RTX5080，并通过官方
PDF补齐A40。3款新型号固定排除在所有训练/验证集之外；A10、H200与5090的已观察结果
单独标为诊断。实验计划先冻结在 `data/round4-plan.json`，不依据测试结果改标签或调参。

8组特征消融只用验证集选择，加入有效FP32、有效带宽等输入交互；模型仍是RandomForest。
包含已知状态、同架构跨型号、多架构未见型号、严格Blackwell架构外推和A10诊断。
未知处理按相关验证设备的两个目标预测符号检查，不再使用全局缺少组合类别的拒判开关。
数值超出训练范围仍保守待验证，RF投票不作为可靠性。

**所有监督标签为合成理论标签，不能解释为真实性能准确率。**
完整交付见 `results/offline-v4/report.md`；逐型号误差、概率、拒判原因和类别数量均保留。

在项目目录运行（已有 `.venv`；依赖为 `requirements.txt`，不需要CUDA）：

```powershell
$env:TEMP=(Join-Path (Get-Location) '.tmp'); $env:TMP=$env:TEMP
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
.\.venv\Scripts\python.exe -m gpu_lab.nvidia.run --output results/offline-v4-replay
.\.venv\Scripts\python.exe -m pytest -q --basetemp .tmp/pytest-round4-replay --junitxml results/offline-v4-replay/pytest-results.xml | Tee-Object results/offline-v4-replay/pytest-results.txt
.\.venv\Scripts\python.exe scripts/summarize_offline_round4.py --results results/offline-v4-replay --pytest-xml results/offline-v4-replay/pytest-results.xml
.\.venv\Scripts\python.exe scripts/verify_offline_round4.py --results results/offline-v4-replay
```

每次完整实验使用新的输出目录，避免覆盖历史模型。全程只做本机CPU机器学习与仿真。

## 第三轮：跨型号 RandomForest 原型

第三轮代码位于 `gpu_lab/crossgpu/`，保留前两轮模型、拆分和结果。数据源为
`data/public/round3/source-records.json`：22条设备记录覆盖7个厂商，其中16款具有
当前FP32理论模型必需的容量、带宽和向量FP32；其余记录保留缺失值。
每个参数的原始单位、口径、来源URL及模型屏蔽原因导出到 `parameter-sources.csv`。
未对齐的低精度峰值不强行跨厂商比较，NPU暂不进入统一模型或原NVIDIA执行仿真。

两个RF目标分别判断训练、推理参考合同是否满足，组合成四类；信息不足、数值越界、
未验证后端或验证依据不足时输出第五类“待验证”，同时保留原始RF输出及保守理论回退。
模型使用14个数值特征，不输入型号、厂商或架构字符串。动态演示复用原资源账本，
每次分配/释放都重新预测；物理硬件参数保持不变。

**本轮所有监督标签均为synthetic。** 公开硬件规格不是真实性能标签；RF投票也不是可靠性。
没有真实性能/公共可比基准标签，不能宣称已实现可靠的跨厂商性能预测。
完整结果见 `results/offline-v3-release/report.md`；`results/offline-v3/` 与 `offline-v3-final/`
是开发期完整运行，最终结果另外保存，没有覆盖它和前两轮结果。B测试存在缺少训练优先真值的覆盖缺口，
因此模型对未知型号采用待验证状态，不能把未知型号的高投票当成已验证结论。

从本项目目录运行（仅本机Python，不连接集群）：

```powershell
$env:TEMP=(Join-Path (Get-Location) '.tmp'); $env:TMP=$env:TEMP
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
.\.venv\Scripts\python.exe -m gpu_lab.crossgpu.run --output results/offline-v3-replay
.\.venv\Scripts\python.exe -m pytest -q --basetemp .tmp/pytest-round3-replay
.\.venv\Scripts\python.exe scripts/verify_offline_round3.py --results results/offline-v3-replay
```

每次实验使用新的输出目录，防止覆盖历史结果。依赖版本见 `requirements.txt`，不需要CUDA。
输出包含两套独立模型、拆分样本/清单、原始与守卫预测、评价指标、混淆矩阵、静态/动态/未知
演示和原三策略仿真回归。A测试已知型号新状态，B按整型号组隔离测试；所有集合按轨迹与
全局特征哈希去重，填补器仅在训练集拟合。软件后端只参与适用范围检查，不作为RF分类特征。

目标：基于 RTX 4090、RTX 5090、L40 的异构特征，研究 Volcano 对训练任务与推理任务的差异化 GPU 调度。

当前已实现两轮离线开发：第一轮公开画像、参考合同、RF 状态分类和资源账本；第二轮 Pod 评分、三策略离散事件仿真、独立执行模型、CSV 和图表。纯 Python、CPU 运行；没有连接或修改线上 Kubernetes/Volcano，没有 DQN，没有执行真实 GPU 训练/推理任务。旧目录的模型、源码和标签不用于本项目训练。

2026-10-15 的只读环境检查作为历史证据保留，见 `reports/environment-report.md`；不能将其作为本轮性能测量。

## 第二轮：直接运行

在本项目目录使用现有虚拟环境。新增 matplotlib 已安装；新机器仍用下方 requirements 安装步骤。

```powershell
# 1. 调度参数验证：种子 20261017、20261018，不参与正式评估
.\.venv\Scripts\python.exe -m gpu_lab.validate_policy --output results/offline-v2-policy-validation-final

# 2. 正式评估：种子 11、29、47，四类负载，每条 120 Pod
.\.venv\Scripts\python.exe -m gpu_lab.round2 --output results/offline-v2 --scoring-policy results/offline-v2-policy-validation-final/selected-policy.json --no-rf-policy results/offline-v2-policy-validation-final/selected-no-rf-ablation.json

# 3. 单元测试
.\.venv\Scripts\python.exe -m pytest -q --basetemp .tmp/pytest-v2

# 可选：读取完整事件日志，复核配对公平性、资源账本及第一轮文件
.\.venv\Scripts\python.exe scripts/verify_offline_round2.py --output results/offline-v2
```

第一轮三个数据划分和已训练模型直接复用；不重新生成、重划分或覆盖 `results/offline-v1/`。第二轮会核对原文件 SHA256。若只要小规模检查，可运行 `python -m gpu_lab.round2 --output results/offline-v2-smoke-new --seeds 101 --pods 24 --scenarios steady_mixed --skip-sensitivity`。使用新输出目录保留不同参数的运行记录。

第二轮主要结果：

- `results/offline-v2/label-audit.md`：40% 配额/44,776 MiB 与 95% 配额/7,000 MiB 的逐项解释。
- `classification-comparison.csv/json`：原规则、静态 RF、第一轮动态 RF；包含误分类数、每类指标、混淆矩阵，另有误分类明细 CSV。
- `simulation-metrics.csv`：每个种子/场景/策略的原始指标；`simulation-summary.csv` 为均值和样本标准差；`paired-comparisons.csv` 为配对差值。
- `rf-ablation.csv`：相同启发式和约束下移除 RF 概率排序贡献，不能将混合评分全部收益归因于 RF。
- `traces/`、`runs/`：冻结输入轨迹、逐任务分配/完成记录、逐事件账本及利用率时间线。
- `figures/`：PNG + SVG 分类对比、三策略对比、干扰敏感性和代表性时间线；`report.md` 给出实验结论与边界。
- `policy-validation-*.csv/json`：原评分与三种装箱约束改进的验证记录，包含未胜出的尝试。

文件位置均相对 `results/offline-v2/`。本轮正式回放包含 36 组主比较、18 组干扰敏感性、12 组无 RF 消融，不依赖实验室 GPU 空闲。

## 第二轮定义与实现

四类保留为**固定参考合同的可行性展示**，不代表 GPU 固有的“训练卡/推理卡”。L40 剩 40% 配额时，训练 step 约 157.85 ms，达到 200 ms 门槛；推理约 13.15 ms，超过 10 ms 门槛。剩 95% 配额但只有 7,000 MiB 时，训练受 8,192 MiB 合同限制，推理参考则满足。效率/带宽份额/SLO 都是声明假设，不是真实 GPU 测量；详见标签审查。

`gpu_lab/scoring.py` 提供 `PodScorer.score_many(states, PodRequest(task_type, ResourceRequest(...)))`。输入保留五个硬件特征、剩余显存/算力/实例、已有任务类型，并加入当前 Pod 类型、申请显存/算力/实例数。所有卡先通过账本硬约束；分类 `unavailable` 不排除更小 Pod。只在可行卡中以 RF 软概率、硬件/共享代理、装箱与剩余平衡度排序；不依据单一类别做准入。分配与释放后都会重新获取当前状态预测并记录事件。

RF 仍是四分类器，不是新训练的 Pod 性能回归器。最终评分是 **RF + 明确的人工启发式**；只有 RF 概率项来自学习，其余权重和共享惩罚属于实验策略。候选版本还提供装箱邻域约束，防止贪心抢占大容量卡；本轮经独立验证后保留原混合评分，因为几个候选没有改善综合目标。验证选择不使用正式调度种子。

验证记录中的 `original_v1` 指第二轮初版 Pod 评分，不是第一轮的 GPU 状态分类器。最终已通过 65 项 pytest；完整事件核验和同种子复跑证据在 `verification.json`、`reproducibility-check.json`。

静态 RF 只使用五个硬件特征，不能观察动态状态。两种类别权重仅在原验证集选择，最终采用不加权版本，避免沿用少数类权重造成弱基线；测试集没有参与选择。原规则相对自己生成的标签为 100% 是定义一致性，不代表性能模型真实；动态 RF 高分也不代表调度性能提升。

`gpu_lab/execution.py` 是**独立的合成执行 oracle**，不导入标签、模型或评分模块，也不读取类别/概率/评分。采用硬件调和速度、Amdahl 配额缩放、共享干扰和按 Pod+卡型+种子生成的稳定扰动。参数来自 `data/synthetic/execution-model.json`，不是第一轮的 roofline 标签公式。调度器只能看 `PodRequest`，看不到 oracle、隐藏工作量或压力。每次共享集合改变都会重新计算所有同卡任务的速度。

三策略均使用 FCFS 可行回填、同一到达轨迹、固定资源申请、无抢占、同一执行种子及观察窗。每 Pod 本轮为一个 vGPU、落一张物理卡；不模拟跨卡通信或多 vGPU 联合放置。11 卡布局数量来自用户，ID 是离线标识。训练 Pod 是有限训练任务；推理 Pod 是有限串行请求批次。

指标口径：平均等待和训练 JCT 对全部可行任务排空后计算；完成数量、算力/显存预留率在共同 600s 窗内统计。预留率是资源账本积分，不是监测到的 GPU 利用率。推理 P95 是**批次 Pod 响应时间**；每请求平均服务时间、已完成整批的请求吞吐和合成批次 SLO 单独报告，不声称测得在线请求 P95、TTFT 或逐 token 时延。控制平面、镜像启动、通信及模型推理的调度耗时未计入仿真时间。

“模块独立”只排除直接读取分类标签评价收益；全部执行参数仍是合成假设。需要结合干扰敏感性、模型消融、失败场景看结论，不能外推真实平台，也不保证动态策略获胜。

## 第一轮：直接运行

在 Windows PowerShell 中，从本工作区进入项目：

```powershell
cd gpu-scheduling-lab
# 本次已创建独立环境并装好依赖，直接使用：
.\.venv\Scripts\python.exe -m gpu_lab.run experiment --output results/offline-v1
.\.venv\Scripts\python.exe -m pytest -q --basetemp .tmp/pytest
.\.venv\Scripts\python.exe -m gpu_lab.run demo --model results/offline-v1/random_forest.joblib
```

新机器安装（Python 3.12；只需要一次联网安装，此后训练/测试/演示完全离线）：

```powershell
New-Item -ItemType Directory -Force .tmp | Out-Null
$env:TEMP = (Join-Path (Get-Location) '.tmp')
$env:TMP = $env:TEMP
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
```

本机执行时用项目内 `.tmp`，避免系统临时目录权限影响 venv/pytest。Linux 对应命令为 `python3 -m venv .venv`、`.venv/bin/python -m pip install -r requirements.txt`、`.venv/bin/python -m gpu_lab.run experiment`、`.venv/bin/python -m pytest -q`。不需要 CUDA，不启动 GPU 任务。

固定种子 `20261016`；默认每种卡 300 条独立轨迹，每条尝试 16 次分配/释放。重跑同一命令得到相同样本划分和预测；运行耗时与 joblib 文件字节不作为随机复现的比较对象。改变种子或配置时请使用新的 `--output` 目录以保留已有实验。

## 数据、标签与模型

`data/public/gpus.json` 只放公开规格及 NVIDIA 原始资料 URL、页码、字段来源；使用同一精度口径的非 Tensor、稠密 FP32 理论峰值，不将 AI TOPS、稀疏 Tensor FLOPS 混合比较。`data/synthetic/cluster-layout.json` 的 4+4+3 数量来自用户，节点和 GPU ID 是离线虚构标识。

`data/reference_workloads.json` 固定两种抽象 FP32 参考负载：

| 参考负载 | 最少显存 | 最低算力配额 | 运算量 / 次 | 显存流量 / 次 | 延迟上限 | 最低吞吐 |
|---|---:|---:|---:|---:|---:|---:|
| 训练 step | 8192 MiB | 20/100 | 2 TFLOP | 8 GB | 200 ms | 5 step/s |
| batch=1 推理 request | 4096 MiB | 10/100 | 0.06 TFLOP | 2.5 GB | 10 ms | 100 request/s |

这是先固定的**合成参考负载合同**，不声称是 ResNet、Llama 等模型的实测需求。暂定 FP32 效率 0.35、带宽效率 0.55；数值是公开声明的实验假设，不是用真实任务拟合所得。吞吐是串行参考操作速率，不是批处理服务系统容量。

标签参考公式：`latency = max(work / (FP32_peak * 0.35 * q), traffic / (bandwidth * 0.55 * q))`，其中 `q=剩余算力配额/100`。显存、最低算力、实例空位都满足且达到延迟和吞吐门槛，才判定该参考负载可运行。假设剩余带宽按算力配额同比例变化；这不代表 vGPU 具备带宽隔离保证。

参考负载的算力是弹性申请：满足性能目标需要的整数配额由公式反解并向上取整，同时不能小于合同最低值。例如 L40 训练需至少 32/100、推理需至少 53/100，不能只给 20/100 或 10/100 就声称达标。标签计算当前剩余配额能否提供这个预算，实际所需配额也写入样本的 `reference_details`。

| 训练参考满足 | 推理参考满足 | 标签 |
|---|---|---|
| 是 | 否 | `training_preferred` 训练优先 |
| 否 | 是 | `inference_preferred` 推理优先 |
| 是 | 是 | `balanced` 训推一体 |
| 否 | 否 | `unavailable` 暂不可用 |

所以这里的“优先”有严格含义：只有该类参考负载满足合同；不是显卡厂商市场定位。“训推一体”表示两种参考负载分别可行，不保证它们同时放入后仍达标；应逐次分配并重新预测。“暂不可用”也不排除更小任务能运行。

动态输入保留原始容量、FP32、带宽、CUDA 核数、TDP，同时加入剩余显存、剩余算力、已分配实例数、训练/推理实例数和实例上限。不输入标签、解析延迟、参考可行性结果、GPU ID 或轨迹 ID。已有任务类型是输入特征，但本版不凭空添加训推干扰系数。

公开规格的 nominal GB 按十进制字节向下转换成 MiB，作为保守的**离线容量假设**；不能直接作为线上 `allocatable`。每卡 100 算力配额、最多 10 个实例是当前离线配置，算力配额不是监测到的 GPU 利用率，也不是物理 CUDA 核的独占划分。`ResourceRequest` 对单卡多个实例记录的是该卡上**合计资源**；跨卡的 Pod 放置层尚未实现。

缺失值：显存/FP32/带宽是生成参考标签必需的数据，缺失时拒绝该画像，不编造数值；可选核数/TDP 可为 `null`，由仅在训练集拟合的中位数填补器处理并加入缺失标记。当前三种卡的所用规格均齐全。对未纳入训练的芯片 ID 明确拒绝推断；本轮不声称跨厂商泛化。

训练使用固定超参数的 sklearn RandomForest，240 棵树，`max_features=0.8`，`min_samples_leaf=2`。只在训练集拟合，验证集报告指标，测试集做最终评估；不根据测试分数调参。按独立轨迹分组 60/20/20 划分（样本数因轨迹长度和去重而不等比例），划分前全局去掉完全重复的特征状态。指标含 Accuracy、Macro-F1、四类 Precision/Recall/F1/Support、混淆矩阵、每卡统计和训练集多数类基线。

输出在 `results/offline-v1/`：三个划分 JSONL、预测明细、数据审计、`metrics.json`、`random_forest.joblib`、`dynamic-demo.json` 和中文 `report.md`。硬件规格和负载合同在结果目录保存副本及 SHA256；所有合成状态和解析标签标明数据类型；真实 GPU 性能测试数为 0。

动态演示在一张离线 L40 上执行两次分配、两次释放，每次都重新调用 RF；同时输出完整 11 卡离线初始画像。预测概率只是森林投票比例，不是 SLO 置信保证。硬资源检查始终独立于模型，模型预测不会批准超量申请。Pod 类型只读 `metadata.labels["gpu-lab/workload-type"]`，允许 `training` / `inference`；缺失或错误就拒绝。`examples/` 仅用于本地标签解析，不是部署任务。

## 当前结果与后续范围

首轮种子下生成 9,123 个独特合成状态，训练/验证/测试为 5,399/1,799/1,925。测试 Accuracy 0.998961，Macro-F1 0.996510；详细证据见 `results/offline-v1/report.md`。这仅证明 RF 能近似解析参考规则，不能作为真实 GPU 适配性准确率或调度性能提升。类别分布和每卡缺失类别均保留，不为抬高分数伪造标签。

上述是第一轮结果的边界。第二轮已按上文实现三策略、独立执行时间模型及比较输出；第一轮的文件保持原样，不能把两轮实验指标混用。

14 天目标的简单安排：第 1–3 天完成当前画像/分类/账本；第 4–7 天完成三策略和仿真及约束测试；第 8–10 天完成多种负载、随机种子、执行假设变化的对比；第 11–14 天整理可复现结果和论文图表。该安排不要求实验室 GPU 空闲。

## 目录

```text
gpu_lab/                 Python 画像、参考标签、数据集、RF、账本、动态演示、Pod 标签解析
data/public/             公开规格及来源（不是性能实测）
data/reference_workloads.json  声明的合成参考负载及假设
data/synthetic/          用户数量组成的离线集群配置
tests/                   pytest 自动化测试
examples/                仅供本地解析的 Pod Label 示例
scripts/                 已有只读环境采集和报告脚本
benchmarks/              真实训练/推理基准预留（当前为空）
profiles/                真实 GPU 性能画像预留（当前为空）
k8s/                     真实集群测试 YAML 预留（当前为空）
results/                 历史环境证据与独立 offline-v1 实验目录
reports/                 历史环境检查报告
scheduler/               Volcano 源码及测试预留（当前为空）
```

## 历史第一阶段：只读检查脚本

脚本只执行查询，不运行 `apply`、`delete`、`patch`、`label`、`drain`、`rollout`、`helm install/upgrade`、驱动配置或 GPU 业务。不会创建 Pod、拉取镜像、重启服务。可能使用 `sudo -n` 读取运行时信息，权限不足会记录失败，不询问或保存 sudo 密码。

在管理节点的本项目目录运行（仅需 Python 3 标准库及已有 kubectl）：

```bash
python3 scripts/check_cluster.py --kubeconfig /etc/kubernetes/admin.conf --sudo-kubectl > results/cluster-check.jsonl
python3 scripts/check_node.py > results/management-node-check.jsonl
```

每个 GPU 节点分别执行 `check_node.py`。也可以不复制文件，通过 SSH 输入脚本：

```bash
ssh bszh@172.16.20.34 'python3 -' < scripts/check_node.py > results/node-172.16.20.34.jsonl
ssh bszh@172.16.10.34 'python3 -' < scripts/check_node.py > results/node-172.16.10.34.jsonl
```

本轮管理账号没有默认 kubeconfig，上面的命令只读使用已存在的 `/etc/kubernetes/admin.conf`，不复制凭据或修改配置。如果在另一套已配置 kubectl 的管理环境运行，直接运行 `python3 scripts/check_cluster.py` 即可。

`scripts/hosts.json` 的地址和账号来自用户此前提供的信息，仅作为连接入口；GPU 型号、节点角色及安装状态以本轮命令输出为准。A40 节点不在本次主动 SSH 检查范围；Kubernetes 若仍返回该节点，报告会保留其实际状态并标为不纳入实验。

本机批量采集入口为 `scripts/collect_remote.py`。使用 OpenSSH 和已有 `known_hosts`，严格检查主机密钥。密码从交互输入或进程环境读取，不写入项目。

```text
python scripts/collect_remote.py --known-hosts ../server_known_hosts --management-kubeconfig /etc/kubernetes/admin.conf --sudo-kubectl
```

`check_access.py` 和 `check_followup.py` 是本轮发现配置入口及核实运行时/挂载二进制用的只读补充脚本。常规复查先运行 cluster/node 两个主脚本。`generate_report.py` 从本轮核验的 JSONL 生成报告，不访问或修改集群；若后续输入缺失或环境关键版本改变，会停止套用当前结论，需要按新记录重新审阅。实际结果见 `reports/environment-report.md`。

## 判断边界

- `nvidia-smi` 中的 CUDA Version 是驱动支持的 CUDA 版本上限，不是宿主机已安装 Toolkit 版本。
- GPU 当前利用率/空闲显存不等于 Kubernetes 尚未分配的 GPU 配额。
- Volcano 已安装、vGPU 插件已安装、vGPU 资源已注册、调度器启用 vGPU，是不同检查项。
- `nvidia.com/gpu`、`volcano.sh/vgpu-*`、旧版 `volcano.sh/gpu-*` 不能混为同一种资源。
- 本阶段不创建测试 Pod，因此没有已有成功工作负载证据时，只能判断配置前提，不能宣布端到端验证成功。
- 5090 的镜像兼容性必须对应具体镜像、驱动、框架和 SM 架构；没有指定或运行镜像时保留未验证结论。

以上仅为已有环境采集脚本说明。本轮离线开发未执行这些远程命令，也不开始集群部署。
