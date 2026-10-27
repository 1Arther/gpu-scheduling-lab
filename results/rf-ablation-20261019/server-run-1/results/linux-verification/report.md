# RandomForest 预测项独立贡献：原生 Volcano 离线消融验证

执行位置：`T3DGQ`，`/home/zbs/rf-ablation-20261019-1425/lab`；go version go1.27.1 linux/amd64。
真实 Linux 进程执行 Go 测试，真实常驻 Python/joblib 推理，真实 Volcano FilterNode / ScoreNode / Allocate / SubResource；Kubernetes 使用 client-go fake，未部署线上调度器。

冻结计划 SHA256：`b0f1c6a4fbbb4a182b4918655ca17efc062170cc4717cb05c25fd131da758628`；模型：`rf-v4-known-states-ee35cfabe30f`；模型与评分策略文件校验前后不变。

## 消融合同

`S_on = H + R`；`S_off = H`。仅去掉暂定可用设备的学习项 R；不改权重、不重新归一化、不改标签和模型。
H 包括原有硬件/共享、装箱、资源平衡和待验证设备的确定性保守代理项。RF off 仍执行模型以记录完全相同的诊断结果，但 RF 贡献为零。
所有主实验候选均通过具体 Pod 的原生逻辑显存、算力、实例及共享约束；参考负载适配性约束与具体 Pod 准入分开记录。
节点按 deviceshare 的四舍五入分数选择，并用固定节点名破平；节点内 UUID 按 RF 设备排序。此测试调用真实 GPUDevices 方法，未运行完整集群调度循环。

## 全量结果

| 场景 | 组数 | 新策略相对 Binpack 改 UUID | 仅移除 RF 后改 UUID | 同节点物理 UUID 改变 | 两候选参考硬条件均满足的 RF 证据 |
|---|---:|---:|---:|---:|---:|
| mixed_pending | 6 | 6 | 0 | 0 | 0 |
| pending_only | 6 | 6 | 0 | 0 | 0 |
| verified_cross_node | 144 | 144 | 32 | 0 | 28 |
| verified_within_node | 24 | 20 | 5 | 5 | 5 |

总计 180 组配对输入，540 次原生分配/释放，3546 条逐候选记录。RF 独立改变 UUID 的组数：37。
相对 Binpack 的变化同时含启发式、排序和 RF，不能全部归功于 RF；只有同输入 RF on/off 的变化用于归因。

## 直接证据

固定用例 `verified-0-1-training-1024`，任务 `training`。两种策略选中的 GPU 都有相关验证覆盖，且目标参考负载硬条件均满足。

| GPU UUID | 模式 | 原始 RF 类别 | 训练/推理判断 | 状态 | RF 贡献 | 启发式贡献 | 总分 | Allocate UUID |
|---|---|---|---|---|---:|---:|---:|---|
| SIM-sim-4090-GPU-00 | rf_on | balanced | True/True | provisional | 30.000000 | 28.869623 | 58.869623 | SIM-sim-4090-GPU-00 |
| SIM-sim-l40-GPU-00 | rf_on | unavailable | False/False | provisional | 0.000000 | 36.278683 | 36.278683 | SIM-sim-4090-GPU-00 |
| SIM-sim-l40-GPU-00 | rf_off | unavailable | False/False | provisional | 0.000000 | 36.278683 | 36.278683 | SIM-sim-l40-GPU-00 |
| SIM-sim-4090-GPU-00 | rf_off | balanced | True/True | provisional | 0.000000 | 28.869623 | 28.869623 | SIM-sim-l40-GPU-00 |

该用例由全量结果中首个满足上述条件的用例自动展示，不修改输入、模型或评分权重。所有其他用例在 CSV 中保留。

## 回退、过滤与动态账本

待验证候选共 0 对；每对 RF 贡献为零，保守代理及最终分数保持相同。混合场景仍可能因其他已验证设备的 RF 项变化而改变最终设备。
独立 filter-control.json 记录不满足具体 Pod 准入条件的设备，RF 开关均不能使其被选中；该过滤对照不计入主消融收益。
sequential-ledger.json 记录 A 分配、B 基于新账本分配、释放 A、C 重新预测、全部释放。每次比较从相同当前账本分叉，避免前一个策略的分配轨迹差异混入当前消融。
所有配对分配均从 Allocate 最终写入的 devices-to-allocate 注解读取 UUID，并检查对应设备增量、其他设备不变以及 SubResource 后快照哈希恢复。

## 验证边界

Go 包结果：`{"volcano.sh/volcano/pkg/scheduler/api/devices/nvidia/vgpu": "pass", "volcano.sh/volcano/pkg/scheduler/plugins/deviceshare": "pass", "volcano.sh/volcano/pkg/scheduler/plugins/predicates": "pass"}`；测试（含子测试）：`{'pass': 254}`。Python 结果见 pytest.log / pytest.xml。
只有逻辑资源快照与合成理论标签；未验证真实训练吞吐、推理延迟、CUDA 成功率或物理显存。RF 投票比例不是预测可靠性。
模型被调用不等于模型改变了决策；贡献相同、分差不足、节点评分取整后相同、所有候选待验证、或硬约束只剩唯一候选时，UUID 可以不变。
真实 Kubernetes 集群调度成功：本轮未执行，不能由离线原生分配推导。

复现：在独立 Linux 目录安装 requirements.txt 的固定 CPU 依赖，执行 `python scripts/run_rf_ablation_linux.py --go /usr/bin/go --output results/linux-verification-new`，然后 `python scripts/report_rf_ablation.py results/linux-verification-new`。
