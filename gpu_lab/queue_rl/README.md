# HRRN 引导的自适应 vGPU 保护：第三阶段

仅使用本地 CPU、合成执行时长和冻结RF推理。使用 Gymnasium + Stable-Baselines3/sb3-contrib 的 MaskablePPO，不含自写PPO、GNN和在线集成。前两阶段源码与正式结果不变。

环境代码 `env.py`；公开观察 `observations.py`；动作与完整候选 `actions.py`；事件挂起/继续 `simulator.py`；积分奖励 `rewards.py`；启发式与安全推理 `policies.py`。`audit.py` 复用独立整数账本和公开预测审计。训练与评价仅通过原 `Cluster.commit` 分配完整Job。

## 安装

本次独立环境 `.venv-queue-rl` 使用Python3.12；精确依赖在结果目录的 `requirements-lock.txt`。它是从原环境复制依赖后补装RL组件，不修改原 `.venv`。干净环境可执行：

```powershell
py -3.12 -m venv .venv-queue-rl
.\.venv-queue-rl\Scripts\python.exe -m pip install -r requirements.txt gymnasium==1.2.3 stable-baselines3==2.7.1 sb3-contrib==2.7.1 torch==2.9.1
```

所有模型显式指定 `device='cpu'`，并固定Torch线程数1。训练已安装的torch实际版本、环境清单和完整SHA另存于结果目录。

## 测试和完整复现

```powershell
Set-Location 'C:\Users\EDY\Documents\ChatGPT\vGPU调度\gpu-scheduling-lab'
$env:OMP_NUM_THREADS='1'
$env:OPENBLAS_NUM_THREADS='1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
.\.venv-queue-rl\Scripts\python.exe -m pytest -q tests/test_queue_rl.py tests/test_queue_sim.py tests/test_queue_study.py tests/test_resources.py

# --out 必须指向一个新目录，下面全部命令保持相同路径。
.\.venv-queue-rl\Scripts\python.exe -m gpu_lab.queue_rl.experiment prepare --out results/my-stage3
.\.venv-queue-rl\Scripts\python.exe -m gpu_lab.queue_rl.batch_train --out results/my-stage3 --workers 3
.\.venv-queue-rl\Scripts\python.exe -m gpu_lab.queue_rl.evaluate development --out results/my-stage3 --workers 3
.\.venv-queue-rl\Scripts\python.exe -m gpu_lab.queue_rl.experiment freeze --out results/my-stage3
.\.venv-queue-rl\Scripts\python.exe -m gpu_lab.queue_rl.evaluate test --out results/my-stage3 --workers 3
.\.venv-queue-rl\Scripts\python.exe -m gpu_lab.queue_rl.report --out results/my-stage3
.\.venv-queue-rl\Scripts\python.exe -m gpu_lab.queue_rl.experiment verify --out results/my-stage3
.\.venv-queue-rl\Scripts\python.exe -m gpu_lab.queue_rl.package --out results/my-stage3

# 一条完整正式轨迹：验证确定性指标、动作以及独立账本，保存所有快照。
.\.venv-queue-rl\Scripts\python.exe -m gpu_lab.queue_rl.replay --out results/my-stage3 --seed 7100009 --condition high_load__independent__under --policy hrrn_ppo --save results/my-stage3/replayed.json
```

单模型训练：`python -m gpu_lab.queue_rl.train --variant joint --idle 0.25 --seed 17 --steps 512 --out results/my-stage3`。相同512步、2种子、2权重用于joint、switch_only、no_hrrn，不能把多个预算混成一次充分收敛训练。

Gym API：`env.reset(seed=...)` 返回观察和info；`env.action_masks()` 返回4个bool；`env.step(action)` 返回5元组。动作0始终有效；无保护决策的episode在reset后已排空，唯一终止dummy transition仅将整条轨迹的积分成本结算，不计入真实保护决策频率。非法动作安全回退0；建议在正式推理中使用 `safe_predict`，不能调用未传mask的普通预测或普通EvalCallback。

有限episode全部排空，gamma=1、GAE lambda=1，采用不折扣总时间成本；首次决策之前的常数成本进入第一奖励。没有通过改变事件数量优化奖励的收益。RL策略不读取真实时长或隐藏事件堆；`DecisionSimulator`作为环境可继承原引擎推进真实合成完成事件。状态构造函数只接收公开快照。

保护的目标固定且完整，多卡没有部分提交。取消和设备不再可用时复用原清理规则；推理不允许直接操作账本。100ms超时为返回后丢弃迟到建议，不能中断卡死的第三方模型调用；生产硬实时需要单独进程隔离，本轮不实现。

## 评价限制

20个配对种子块×14条件，新命名空间；9主策略+2消融。P95/P99为逐轨迹分位数宏平均；高负载有额外三种估计误差条件，因而条件等权会赋予高负载较高权重，不能直接与第二阶段场景等权汇总横比。规模分组和逻辑资源定义复用第二阶段。

95%区间以种子块配对bootstrap；PPO对HRRN、固定vGPU、启发式、EASY的P95/长等待比例/完成工作量12个主要比较另给Bonferroni控制区间。其它分层/条件排序为次要描述。

PPO仅选前三个RF完整组合，8GPU定长输入，候选+3条队列记录，其余作业聚合。开发尺度压力测试4/8卡×24/40作业不用于重新选型。当前训练预算只证明运行流程，不证明收敛；须保留始终不保护、退化和所有失败结果。并行墙钟测量受CPU竞争和共享缓存热度影响，不是严格实时上界。资源分配率是逻辑配额，虚拟闲置保护单独计量，不是真实GPU利用率。

成熟库文档：[MaskablePPO](https://sb3-contrib.readthedocs.io/en/master/modules/ppo_mask.html)、[Gymnasium环境接口](https://gymnasium.farama.org/api/env/)。
