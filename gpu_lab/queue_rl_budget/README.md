# HRRN-PPO 诊断与预算实验

独立增量模块；所有GPU/Job放置、HRRN、EASY、RF、奖励、动作及观察均复用已冻结的 `gpu_lab/queue_rl` 等模块。只在CPU上运行合成轨迹。

## 已完成结果

`results/hrrn-ppo-budget-20261026/README.md` 是本轮主报告，`diagnosis/README.md` 是原模型独立诊断。目录内保存完整模型、原始逐步CSV、更新日志、公开观察、开发指标、配对区间、图和SHA。不要把开发集称作新最终测试。

## 全量复现（必须指定新的项目内目录）

在项目根目录 PowerShell 运行，沿用现有独立 `.venv-queue-rl` 环境和原第三阶段冻结模型：

```powershell
$env:OMP_NUM_THREADS='1'
$env:OPENBLAS_NUM_THREADS='1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
$env:GPU_QUEUE_BUDGET_OUT="$PWD/results/hrrn-ppo-budget-reproduction"
.\.venv-queue-rl\Scripts\python.exe -m gpu_lab.queue_rl_budget.run
.\.venv-queue-rl\Scripts\python.exe -m pytest -q tests/test_queue_rl_budget.py tests/test_queue_rl.py tests/test_queue_sim.py tests/test_queue_study.py tests/test_resources.py
```

环境变量仅改变新结果目录，不改变旧冻结目录。prepare和已完成训练拒绝覆盖；重算报告不重新训练或修改旧结果。完整复现可能需要十几分钟到更长，是否追加32768由冻结规则决定，不伪造未执行预算。

## 分步运行

```powershell
.\.venv-queue-rl\Scripts\python.exe -m gpu_lab.queue_rl_budget.common
.\.venv-queue-rl\Scripts\python.exe -m gpu_lab.queue_rl_budget.diagnose
.\.venv-queue-rl\Scripts\python.exe -m gpu_lab.queue_rl_budget.accumulation
.\.venv-queue-rl\Scripts\python.exe -m gpu_lab.queue_rl_budget.enrich
.\.venv-queue-rl\Scripts\python.exe -m gpu_lab.queue_rl_budget.counterfactual
.\.venv-queue-rl\Scripts\python.exe -m gpu_lab.queue_rl_budget.train --seed 17 --maximum 8192
.\.venv-queue-rl\Scripts\python.exe -m gpu_lab.queue_rl_budget.train --seed 43 --maximum 8192
.\.venv-queue-rl\Scripts\python.exe -m gpu_lab.queue_rl_budget.train --seed 71 --maximum 8192
.\.venv-queue-rl\Scripts\python.exe -m gpu_lab.queue_rl_budget.evaluate --workers 2
.\.venv-queue-rl\Scripts\python.exe -m gpu_lab.queue_rl_budget.analyze
.\.venv-queue-rl\Scripts\python.exe -m gpu_lab.queue_rl_budget.checkpoints
.\.venv-queue-rl\Scripts\python.exe -m gpu_lab.queue_rl_budget.report
```

若 `staircase-decision.json` 的 `stop_before32768` 为false，需先对三个种子执行 `train --maximum 32768`，再 `evaluate --extended` 和 analyze/report。32768使用新目录下完整重训以保留同一随机轨迹前缀，重复前缀成本计入总CPU成本，不伪称无成本续训。

主要优化指标、损失边界、停止条件、输入SHA在训练前写入 protocol/input-freeze。报告生成器在晋级条件通过时拒绝输出负结论，要求先制定冻结的新测试协议、生成从未使用的测试轨迹并实际执行；本轮是否执行以主报告和状态文件为准。

## 方法限制

- 原模型的逐动作概率由最终权重和原公开状态重算；原始优势未保存，不能编造恢复。
- 资源积累是原策略的完整合成轨迹离线复放；不是用隐藏实际时长挑动作。
- 新训练的 `steps.csv` 记录采样时的概率/优势；开发评估保持原确定性argmax。
- 有效决策=至少一个保护动作合法，并不意味着动作之间存在可学习的巨大收益差。
- 置信区间对相同工作负载种子块配对；三种训练种子单独报告波动，避免伪增样本量。
