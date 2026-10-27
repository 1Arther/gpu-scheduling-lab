# 第四轮：NVIDIA 跨型号 RandomForest 适配性分类

仅本机CPU训练和离散事件仿真，未连接Kubernetes、未修改Volcano、未运行GPU负载。所有准确率均以**合成理论标签**为真值，不是真实GPU性能分类准确率。

15款NVIDIA独立型号/配置；7710条合成资源状态。前三轮 553 个结果文件经SHA256核对内容未变。旧代码不修改，新增 gpu_lab/nvidia 包，复用第三轮账本、数据生成和标签合同。

## 固定测试与标签

在生成本轮标签和查看本轮预测前保存 data/round4-plan.json。新独立型号为RTX A6000、RTX6000 Ada、RTX5080；它们不进入任何协议的训练或验证，包括已知状态实验。A10是已看过的诊断案例；H200与RTX5090也属历史已观察型号，相关结果明确标为诊断。跨协议可能重复测试相同新型号，因此不是多个相互独立的重复验证；所有协议同时冻结后才一次性评估。

| 协议 | 意义 |
|---|---|
| known_states | 历史12款型号的新状态，按整条轨迹60/20/20划分 |
| A_ampere / A_ada | 同架构、按型号完全隔离，分别测试新RTX A6000/RTX6000 Ada |
| B_multi_architecture | 多架构训练，测试三个新型号及H200历史诊断；不是整个架构未见 |
| C_blackwell_holdout | 训练与验证完全无Blackwell，测试5080/5090；严格架构外推，5090为历史诊断 |
| D_a10_diagnostic | 完全不拟合A10的诊断回归，不能称为新盲测 |

同一协议中型号分组无泄漏（known_states按轨迹分组）；样本ID、轨迹ID和特征哈希均检查不交叉。Hopper和Blackwell各只有两个型号，不足以在单一架构内划分三个互斥型号集合，不硬凑同架构实验。

标签合同原样沿用第三轮：IEEE FP32非Tensor；training8192MiB、最低20配额、2TFLOP/8GB、0.2s与5次/s；inference4096MiB、最低10配额、0.06TFLOP/2.5GB、0.01s与100次/s。理论时间=max(work/(FP32*0.35*q),traffic/(BW*0.55*q))，每卡100个假想份额、10实例。系数与配额线性缩放是合成假设，不是实测规律。分类取两目标组合，训推一体表示分别满足而非同时可运行。

未改标签门槛，未增加厂商定位标签，未把RF预测回填为真值。缺失类按真实分布报告。Ampere部分设备FP32不足以满足训练合同，部分设备根本不会出现训练优先类。

## 特征与模型选择

基线为7静态+5动态+2比例的14列接口。3个低精度Tensor列因口径差异统一屏蔽，数据库保留原值。有效静态列为容量、带宽、非Tensor FP32和功耗；不将型号/厂商/架构字符串输入RF。

增加effective_fp32=FP32*q、effective_bandwidth=BW*q、compute_bandwidth=FP32*1000/BW（FLOP/byte）。只由输入计算，不读取标签、参考合同阈值或执行时间。它们与本轮理论标签假设一致，收益表示更容易拟合已知理论规律，不能证明实际性能线性缩放。

8组特征×2个叶节点大小{2,5}；120树、max_features0.85、seed20261017、CPU单线程。各特征组只在验证集选叶节点；再按验证集中实际出现类别的macro-F1、Accuracy、较少特征选主模型。始终同时报告固定四类Macro-F1，以免缺失类别改变解释。没有利用测试集选择特征或阈值。参数选择不使用任何测试标签。

## 模型结果（合成标签）

| 协议 | 选中特征 | 测试样本 | Accuracy | 四类Macro-F1 | 实际类别Macro-F1 |
|---|---|---|---|---|---|
| known_states | effective_compute | 1244 | 1.0000 | 1.0000 | 1.0000 |
| A_ampere | effective_compute | 522 | 0.9579 | 0.7058 | 0.9411 |
| A_ada | absolute_only | 515 | 0.9670 | 0.9547 | 0.9547 |
| B_multi_architecture | effective_both | 2067 | 0.9995 | 0.9992 | 0.9992 |
| C_blackwell_holdout | effective_both | 1044 | 1.0000 | 0.7500 | 1.0000 |
| D_a10_diagnostic | effective_both | 514 | 0.9591 | 0.7225 | 0.9633 |

known_states的训练/验证/测试指标、各协议四类Precision/Recall/F1/Support、混淆矩阵见各metrics.json和confusion.csv/png。所有原始预测都计入测试指标，包括最终因可信度不足而待验证的状态。

| 协议/型号 | 架构 | 状态 | Accuracy | 四类F1 | 接受状态比例 | 接受后错误 |
|---|---|---|---|---|---|---|
| known_states / a10 | Ampere | previously_observed_or_known_diagnostic | 1.0000 | 0.7500 | 1.000 | 0 |
| known_states / a100_sxm80 | Ampere | previously_observed_or_known_diagnostic | 1.0000 | 0.5000 | 0.759 | 0 |
| known_states / a30 | Ampere | previously_observed_or_known_diagnostic | 1.0000 | 0.2500 | 1.000 | 0 |
| known_states / a40 | Ampere | previously_observed_or_known_diagnostic | 1.0000 | 0.7500 | 1.000 | 0 |
| known_states / h100_sxm | Hopper | previously_observed_or_known_diagnostic | 1.0000 | 0.7500 | 1.000 | 0 |
| known_states / h200_sxm | Hopper | previously_observed_or_known_diagnostic | 1.0000 | 0.7500 | 1.000 | 0 |
| known_states / l4 | Ada Lovelace | previously_observed_or_known_diagnostic | 1.0000 | 0.5000 | 0.960 | 0 |
| known_states / l40 | Ada Lovelace | previously_observed_or_known_diagnostic | 1.0000 | 1.0000 | 1.000 | 0 |
| known_states / l40s | Ada Lovelace | previously_observed_or_known_diagnostic | 1.0000 | 1.0000 | 1.000 | 0 |
| known_states / rtx3090 | Ampere | previously_observed_or_known_diagnostic | 1.0000 | 0.7500 | 1.000 | 0 |
| known_states / rtx4090 | Ada Lovelace | previously_observed_or_known_diagnostic | 1.0000 | 1.0000 | 1.000 | 0 |
| known_states / rtx5090 | Blackwell | previously_observed_or_known_diagnostic | 1.0000 | 0.7500 | 1.000 | 0 |
| A_ampere / rtx_a6000 | Ampere | fresh_model_held_out_this_round | 0.9579 | 0.7058 | 0.000 | 0 |
| A_ada / rtx6000_ada | Ada Lovelace | fresh_model_held_out_this_round | 0.9670 | 0.9547 | 1.000 | 17 |
| B_multi_architecture / h200_sxm | Hopper | previously_observed_or_known_diagnostic | 0.9980 | 0.7460 | 0.000 | 0 |
| B_multi_architecture / rtx5080 | Blackwell | fresh_model_held_out_this_round | 1.0000 | 0.7500 | 0.000 | 0 |
| B_multi_architecture / rtx6000_ada | Ada Lovelace | fresh_model_held_out_this_round | 1.0000 | 1.0000 | 1.000 | 0 |
| B_multi_architecture / rtx_a6000 | Ampere | fresh_model_held_out_this_round | 1.0000 | 0.7500 | 1.000 | 0 |
| C_blackwell_holdout / rtx5080 | Blackwell | fresh_model_held_out_this_round | 1.0000 | 0.7500 | 0.000 | 0 |
| C_blackwell_holdout / rtx5090 | Blackwell | previously_observed_or_known_diagnostic | 1.0000 | 0.7500 | 0.000 | 0 |
| D_a10_diagnostic / a10 | Ampere | previously_observed_or_known_diagnostic | 0.9591 | 0.7225 | 0.531 | 0 |

每款型号的类别数量在per-model.csv中；所有错误样本含硬件/资源特征保存在各协议test-errors.csv，全部预测与待验证原因保存test-predictions.jsonl。不把无训练优先真值的单型号F1=0误读为该类被证实无效。

## 特征消融

各行参数均独立在同一验证集选取。下表为B协议，主模型在测试前选定，不能按下表测试分数改选。

| 组合 | 验证Acc | 验证实际类F1 | 测试Acc | 测试四类F1 | 验证选中 |
|---|---|---|---|---|---|
| baseline14 | 0.7864 | 0.6952 | 0.9444 | 0.8568 | False |
| absolute_only | 0.7854 | 0.6947 | 0.9439 | 0.8524 | False |
| relative_memory | 0.7518 | 0.6368 | 0.8916 | 0.7534 | False |
| no_static_capacity | 0.7792 | 0.6834 | 0.9434 | 0.8514 | False |
| effective_compute | 0.8952 | 0.8849 | 0.9753 | 0.9338 | False |
| effective_bandwidth | 0.7782 | 0.6936 | 0.9642 | 0.8911 | False |
| effective_both | 1.0000 | 1.0000 | 0.9995 | 0.9992 | True |
| effective_both_balance | 1.0000 | 1.0000 | 0.9995 | 0.9992 | False |

absolute_only移除两个剩余比例；relative_memory只保留显存比例而去掉总/剩余显存绝对值；no_static_capacity仅移除物理总容量。参考负载本身有绝对MiB下限，因此比例不能等价代替绝对显存。有效算力/带宽代理把乘法交互显式提供给轴对齐RF；比例项和算力/带宽比是否有帮助要结合逐协议消融结果，不能假设必然提升。

## 相关验证证据与未知处理

取消第三轮全局缺类开关。先校验输入/配额/软件后端及资源约束，再检查三项关键静态能力是否超出训练范围。相关验证群体优先使用同架构且三个关键能力均在2倍距离内的验证设备；无此群体时要求至少两款数值邻近的验证设备。架构只用于可选验证证据路由，不进入RF。未知架构仍可走数值邻域。

对训练、推理两个预测符号分别检查：相关验证群体中该预测符号至少20个样本、3条轨迹、正确比例至少0.90。硬约束已否定的目标直接为False，不要求无关类别存在。所有阈值在计划中冻结，不随测试效果调整。RF概率未用于质量开关，更不等于真实可信度。相关合成状态有相关性，因此支持量与正确率也不是独立性能置信区间。

数值超出训练范围仍待验证；例如16GB的5080小于本次其余训练卡的最小容量，不能因为原始RF答对就宣布已验证。缺失关键字段、未知配额/后端同样保守拒绝。待验证保留raw_rf和0.8有效能力因子的确定性理论回退。调度适配器先执行显存/算力/实例/后端过滤，随后按保守理论分数与packing排序；不会用分类结果批准超额Pod。

| 协议 | 可暂定型号数/总数 | 接受状态数/总数 | 接受后错误 |
|---|---|---|---|
| known_states | 12/12 | 1214/1244 | 0 |
| A_ampere | 0/1 | 0/522 | 0 |
| A_ada | 1/1 | 515/515 | 17 |
| B_multi_architecture | 2/4 | 1037/2067 | 0 |
| C_blackwell_holdout | 0/2 | 0/1044 | 0 |
| D_a10_diagnostic | 1/1 | 273/514 | 0 |

该统计中接受表示最终返回四类之一。原始RF错误数与硬约束修正后的错误数分别保留，防止用过滤隐藏分类器错误。B汇总同时含H200历史诊断，独立新型号请看逐型号表三条fresh行。未知演示没有真实性能标签，不统计真实性能准确率。

## 动态演示与兼容回归

使用known_states模型演示4090、5090、L40，各5步：初始、占用1000MiB/60配额、再占用大部分显存/5配额、释放计算份额、完全释放。每步包含静态硬件、动态状态、训练/推理预测、四类概率近似、证据和待验证原因。静态硬件哈希不变。四类概率是两个RF边际估计的乘积近似，非校准联合概率。

11卡三策略相同60Pod/seed101，旧独立执行模型seed707；三种策略不改资源和释放语义。待验证时的兼容概率字段承载确定性排序分数，明确标为非概率。下面只证明兼容，不能作为调度收益结论。

| 策略 | 完成 | 平均等待s | 训练JCTs | 推理PodP95s |
|---|---|---|---|---|
| binpack | 60 | 2.444 | 200.847 | 6.412 |
| static_rf | 60 | 2.444 | 200.847 | 6.412 |
| dynamic_rf | 60 | 2.201 | 195.064 | 6.137 |

所有策略无超分配，结束后显存和配额全部恢复；Pod Label解析、动态变更后重新预测均已检查。未使用该回归结果反向调模型。

## 结论的使用范围

本轮可以检验数值硬件特征对合成理论标签的跨型号近似；新增型号数量只有3，存在单一厂商、固定合同、少数验证型号及合成轨迹相关性限制。即使测试高分也不能声称真实训练/推理任务、不同精度或实际vGPU干扰已验证。A10诊断与第三轮50.85%使用不同训练覆盖/拆分状态，只能诊断改进，不能作为受控独立盲测提升率。

## 复现

在gpu-scheduling-lab目录：

```powershell
$env:TEMP=(Join-Path (Get-Location) '.tmp'); $env:TMP=$env:TEMP
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
.\.venv\Scripts\python.exe -m gpu_lab.nvidia.run --output results/offline-v4-replay
.\.venv\Scripts\python.exe -m pytest -q --basetemp .tmp/pytest-round4-replay
```

依赖沿用requirements.txt，不需要CUDA。计划、来源、代码/历史结果哈希和模型选择均保存。fresh测试型号始终不参与拟合/验证。

## 文件索引

- [硬件CSV](nvidia-hardware.csv) / [硬件JSON](nvidia-hardware.json) / [逐参数来源](parameter-sources.csv) / [审计说明](source-audit.md)
- [冻结实验计划](frozen-plan.json) / [全部划分](split-manifests.json) / [测试前选择冻结](selection-frozen-before-test.json)
- [逐型号结果](per-model.csv) / [完整特征消融](ablations.csv) / [未知型号示例](unknown-demo.json)
- [动态分类示例](dynamic-demo.json) / [仿真回归](simulation-regression.json)
- 各协议目录内selected-model.joblib、model-metadata.json、metrics.json、confusion.png/csv、test-errors.csv和test-predictions.jsonl。

## 补充实验与验证文件

- [同架构Ampere报告](A_ampere/report.md) / [同架构Ada报告](A_ada/report.md)
- [跨架构混合训练报告](B_multi_architecture/report.md) / [严格Blackwell架构外推](C_blackwell_holdout/report.md)
- [A10历史诊断](D_a10_diagnostic/report.md) / [特征消融分析](ablation-report.md)
- [新型号有效暂定预测覆盖](fresh-unknown-coverage.json) / [未知GPU保守排序](unknown-ranking-demo.json)

实际pytest：110项，errors=0，failures=0，skipped=0。原文见[pytest结果](pytest-results.txt)。
