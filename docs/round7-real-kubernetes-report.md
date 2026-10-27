# 第七轮：真实 Kubernetes RF-vGPU sleep 分配验证

**已在真实4090节点完成6个sleep Pod的设备分配，全部启动，全部清理。原`volcano-scheduler`已恢复1副本并可用；RF为0副本。原配置、Deployment spec及运行二进制核对一致。**

本次不是纯Python仿真或Go fake client：使用真实API Server、Volcano Go Filter/Score/Allocate及Device Plugin Allocate响应。只运行`/bin/sleep 600`，不运行CUDA、训练或推理。RF输出仍来自合成理论标签训练的冻结模型，不代表真实性能保证。

## 执行窗口与恢复

- 管理员通过本次对话确认没有其他用户或自动化在窗口内提交可能使用4090的GPU任务。API同时核对无活跃/Pending/Terminating GPU/vGPU/DRA请求。
- 先备份原Deployment和ConfigMap，准备两个独立镜像，再停止原scheduler。**不是两个GPU调度器同时运行**；原Pod和CRI容器退出后才启动RF。普通GPU入口仍依靠已确认的协调窗口，没有安装集群级准入。
- 第一次尝试因采集函数未处理普通Pod缺少annotations而中止，约4.6秒后自动恢复；没有创建RF或测试Pod。修复后在新目录重试，保留旧记录。
- 第二次窗口：北京时间`2026-10-19 19:39:12–19:42:39`，约208秒。原DQN服务始终1副本，未改配置/二进制；原scheduler恢复后产生新Pod，这是临时缩容/恢复的正常结果。
- 正常和异常路径都有finally恢复及独立超时保护。末尾回退校验把原因字符串误当bool，原始脚本因此记录failure；清理和原scheduler恢复仍成功。后续按真实日志合同修复解析并完成离线核验，**没有改写原始failure记录，也没有为此再切换集群**。

备份与完整原始日志在4090：`/home/zbs/rf-round7-live-window-20261019/`及`/home/zbs/rf-round7-live-window-20261019-attempt2/`。本地精简证据含所有原始文件SHA；完整116MiB原始记录仍在服务器。大包下载超时留下的`.partial`不作为有效证据。

## 真实分配结果

所有Pod请求相同：1个vGPU、4096MiB逻辑显存、20算力配额。目标为t3dgq四张4090；下表简写UUID，完整值在CSV。

| 用例 | 类型 | 原生Allocate UUID | 插件响应UUID | 结果 |
|---|---|---|---|---|
| A | training | GPU-27be0024… | 相同 | Running，RF三方一致 |
| B（A仍运行） | inference | GPU-28cf7186… | 相同 | Running，输入已包含A的配额 |
| C（删除A、保留B） | training | GPU-27be0024… | 相同 | Running，输入已反映A释放 |
| control RF on | inference | GPU-27be0024… | 相同 | Running，学习项启用 |
| control RF off | inference | GPU-27be0024… | 相同 | Running，仅关闭学习评分项 |
| 服务不可达 | inference | GPU-27be0024… | 相同 | Running，原生确定性回退 |

完整UUID：`GPU-27be0024-0498-4114-e6b9-b2d0db148309`、`GPU-28cf7186-0bdd-be9f-92e5-3c59b4e92c1e`。另外两卡也参与候选评分，均满足硬约束，没有被本轮请求过滤掉。

核对链：RF响应的request_id/Pod UID → Go成功提交日志的requestID/UUID → 永久`volcano.sh/vgpu-ids-new` → 同一UID、sleep容器、vgpu-number资源的kubelet AllocateResponse.envs.NVIDIA_VISIBLE_DEVICES。不使用虚拟DeviceID推断UUID；临时devices-to-allocate被消费后为空是正常现象。

结果：

- 合法分配并启动：**6/6**。
- RF建议→原生→插件三方一致：**5/5**，其中4次启用学习项、1次关闭学习项；故障用例没有RF建议，不放进这个分母。
- 原生UUID→插件响应UUID一致：**6/6**。
- 每次原生分配对象的扣减：选中卡**4096MiB/20/1**，其他卡不变，**6/6正确**。
- 记录的原生快照与API重建中，显存/算力/实例超分配：**0次**。
- 6个Pod UID均已删除并存在相符SubResource减量；最终4卡的12项逻辑分配指标为0，API无活跃GPU/DRA Pod、无NodeLock。
- 故障用例：RF endpoint指向loopback:1，真实记录`connection refused`，原生回退成功1/1。没有停止DQN服务或杀死Python进程。

这些都是本次小样本控制面结果，不能外推为所有负载100%可靠。没有验证CUDA可用性或实际GPU执行成功。

## 动态状态是否真的更新

A前：四卡各剩余24564MiB、100配额、0实例。

B的真实RF输入：27be卡剩余**20468MiB/80**，已分配**1实例、1训练Pod**；B选择28cf卡。

删除A后，C的真实RF输入：27be恢复**24564MiB/100/0实例**；28cf仍为**20468MiB/80/1推理Pod**；C选择27be。A、B、C在同一个RF scheduler Pod中完成，未通过重启调度器冒充释放。

随后B、C删除后的同进程control-on请求再次看到四卡零分配。其余对照之间为避免新旧RF实例并行，清理Pod后才Recreate切换配置。

本次被观测状态的分类均为**暂定训推一体（balanced/provisional）**，没有观察到类别变化；资源特征、已有任务组成和分数确实随分配/释放更新。

## RF独立贡献结论

严格配对的control-on/off使用相同Pod YAML、四卡逻辑快照、硬约束、硬件/共享/装箱/平衡权重及模型：

| 每张空闲4090 | RF学习项 | 启发式项 | 总分 |
|---|---:|---:|---:|
| on |0.300000000|0.286741984|0.586741984|
| off |0|0.286741984|0.286741984|

四张卡得到相同的balanced预测和相同的RF加分，因此排序与最终UUID均未改变。**证明RF预测项进入了实际评分，但这次同型号小请求实验没有证明该学习项独立改善或改变设备选择。** A/B的UUID不同也不能独立归因于RF分类：其资源状态和启发式评分已经不同。

RF off指关闭学习评分项，保留服务原始预测用于诊断；不是停止RF服务。故障回退使用原生评分，数值尺度与RF总分不同，不能当成同一种概率比较。本轮未单独提交原生产DQN或原生Binpack对照Pod，旧180组离线消融的37次UUID变化仍仅是离线证据。

## 配额指标与观测边界

- 峰值逻辑显存预留：8192/98256 = **8.3374%**。
- 峰值逻辑算力预留：40/400 = **10%**；峰值2实例。
- 参考请求4096MiB/20/1的碎片率：本次观测最大为**0**。定义沿用可放置实例数与聚合上界差额，非物理利用率。
- Go提交日志中的logicalBefore/After是直接的原生分配对象快照；RF请求是调度器提供的当前视图；API活跃Pod注解是独立重建，分别保留。
- SubResource还会在克隆调度视图中执行，因此重复释放日志**不是重复释放次数，也不是唯一的全局cache提交日志**。使用UID、实际删除、后续活跃RF输入和最终指标交叉核对；没有声称取得全局内存dump。
- 没有测训练时间、推理延迟、CUDA吞吐、物理显存性能或OOM；没有实测5090/L40异构优选。

## 模型、镜像、测试与复现

冻结模型：`rf-v4-known-states-ee35cfabe30f`；joblib SHA256仍为`ee35cfabe30fffc28ff0be09d3738587d4f9bffbe26386e2a9fae5c5b9e80748`。没有训练或修改权重/数据。Go源码本轮未改，运行独立二进制SHA为`501e9ece8f838fc2abcc82f5ea731e399178705f7a0bda0d6c02c8eac6b0d7e9`。

实验镜像：

- Scheduler：`docker.io/gpu-lab/volcano-rf@sha256:43c62614e45fb18197af5c3b19081bb814c47de2f573f6b382c5639ee0bdcdbc`
- RF：`docker.io/gpu-lab/rf-model@sha256:7ef2fd82031578388f75e101f9a6ab572faacf7c96fa27e39bfcfe8b213c4259`

修复的是采集/核验代码：允许Pod缺少可选metadata；按Go实际string合同解析fallback；读取原生JSON快照。没有调整模型、Go评分算法或插件。

Linux独立CPU回归：**97 passed**，冻结模型前后SHA相同。本机43项第七轮测试亦通过。Go编译/单测复用前轮Linux记录，不宣称本轮重新运行；本轮另有上述真实Go调度证据。

```bash
# 只读取已经保存的真实证据，重新生成CSV和校验摘要，不会连接集群：
python3 scripts/round7_analyze_live.py --results results/round7-live-window-20261019
# 修复后的第五至七轮CPU测试；不重新训练：
python3 -m pytest tests/test_round5.py tests/test_round6.py tests/test_rf_ablation.py tests/test_round7.py tests/test_round7_reassessment.py tests/test_round7_final_review.py -q
```

主要交付：`verified/summary.json`、`allocations.csv`、`candidate-scores.csv`、`native-resource-transitions.csv`、`release-checks.csv`、`rf-on-off-comparison.json`、`service-fallback-verified.json`。原始harness failure与独立证据核验结果分开保存。

## 最终集群状态

最终只读复核见 `final-readonly-verification.json`：原scheduler **1/1 Available**，Deployment spec及ConfigMap数据一致，配置RV仍21788675，实际运行二进制SHA仍`9f2e1ae3359ce234fd048bc48aab082add99a686631808908b5ae518bdebf4f4`；DQN服务1/1。RF **0副本/0 Pod**，测试Pod及PodGroup均为0，4090无NodeLock，无活跃GPU/DRA请求。

保留停止的RF Deployment、两个Namespace、RBAC、独立Queue/Quota、配置及独立Lease供审计/复现；RF默认配置已恢复到on，但不会自动启动。新镜像缓存保留。没有删除旧业务Pod、清理历史checkpoint/共享锁、改变驱动/plugin/containerd或操作其他用户进程。再次启动真实实验仍需新的协调窗口，不能直接重用本次已结束的授权窗口。
