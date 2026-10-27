# 第三轮：跨型号 RandomForest 适配性原型

本轮仅在本机 CPU 运行 Python、随机森林和离散事件仿真；未连接集群，未执行真实 GPU 训练或推理。

**结论边界：这是公开硬件参数 + 合成资源状态 + 合成理论标签的监督学习原型，尚不具备经真实性能验证的跨厂商预测能力。** 模型学习的是声明的参考合同。没有测量标签和可比公开基准标签，不报告真实跨厂商准确率。

硬件库 22 条、7 个厂商；16 条具有共同 FP32 原型所需参数，其余保留 null/待验证。合成状态 4867 条。历史结果文件 411 个已核对 SHA256，内容未变。

## 标签、资源和精度口径

训练/推理类型由 Pod 的 `gpu-lab/workload-type: training|inference` 指定。RF 不识别任务类型，也不使用型号、厂商、架构字符串作为输入。

两个布尔目标表示当前剩余资源是否分别满足以下假设负载。两者都满足为训推一体，只有训练满足为训练优先，只有推理满足为推理优先，都不满足为当前不满足参考负载要求。两者分别测试，**不代表可以同时容纳两个负载**。第五状态来自输入/适用范围检查，不伪造有监督真值。

| 参考合同 | 精度 | 显存下限 | 配额下限 | 单次工作量/访问量 | 延迟上限/吞吐下限 |
|---|---|---|---|---|---|
| training | dense IEEE FP32 vector | 8192 MiB | 20/100 | 2 TFLOP / 8 GB | 0.2s / 5次/s |
| inference | dense IEEE FP32 vector | 4096 MiB | 10/100 | 0.06 TFLOP / 2.5 GB | 0.01s / 100次/s |

理论时间 `max(work/(FP32*0.35*q), traffic/(bandwidth*0.55*q))`。0.35/0.55 和带宽随份额线性变化都是人为声明的假设。8192/4096MiB 是参考合同要求，绝非通用训练/推理显存门槛，也不是具体模型测试结果。

例如 L40 剩余约44GB、40%配额：训练理论时间约0.158s、推理约0.0132s，故按该合同为训练优先；剩余7GB、95%配额：训练未达8192MiB下限，推理约0.00554s，故为推理优先。这是资源与参考合同的关系，不是 L40 的硬件算力发生变化。

14 个数值输入=7个静态特征+5个动态特征+2个比例。低精度规格若累加精度/引擎口径不一致则保留原数并屏蔽给模型；FP32是本轮标签依据。可选缺失使用训练集拟合中位数+缺失指示；整列缺失使用0占位+指示，0并非真实算力。关键容量/带宽/FP32缺失直接待验证。

计算配额 namespace 为 `synthetic_device_share_100_v1`，只是假想的设备份额。不同厂商原生核数、百分比或后端资源名没有等价映射时拒绝统一预测。多芯粒 MI250X 的合成池不包含拓扑局部性。

## 训练和最终测试

使用 multi-output RandomForestClassifier 同时预测两目标；180棵树、max_features=0.85；min_samples_leaf∈{2,5}仅依据验证集选择，训练后不使用测试集调参。A按整条状态轨迹划分，B按型号组划分。样本ID、轨迹ID和特征哈希跨集合均无重叠。B额外保证型号组无重叠。前两轮模型和原拆分完整保留，本轮另建多型号拆分。

运行前曾以固定40棵树做一次实现冒烟检查并查看 A/B 汇总；正式180棵树与两个候选叶节点大小预先固定，未按该测试结果更改特征、分组或参数搜索空间。本轮仍属开发期评估；论文最终结论应再冻结新独立型号测试集。

| 协议 | 数据集 | 样本 | Accuracy | Macro-F1 | 误分类 |
|---|---|---|---|---|---|
| A-known-models | train | 2907 | 0.9997 | 0.9996 | 1 |
| A-known-models | validation | 988 | 0.9868 | 0.9686 | 13 |
| A-known-models | test | 972 | 0.9887 | 0.9769 | 11 |
| B-unseen-models | train | 3061 | 0.9990 | 0.9980 | 3 |
| B-unseen-models | validation | 885 | 0.9718 | 0.7309 | 25 |
| B-unseen-models | test | 921 | 0.7655 | 0.5778 | 216 |

上表全部以**合成理论规则**为真值，不能据此声称真实吞吐/延迟预测准确，也不能据此声称调度优于其他方法。完整 Precision/Recall、类别数量和混淆矩阵见各协议 metrics.json、confusion.csv/png。原始 RF 的所有测试预测都计入指标，待验证机制没有隐藏错误。

B 测试型号固定 A10、MI300X、MTT S4000；验证型号 A30、MI210、H100；其他10个型号组用于训练。仅16款静态设备，数千个合成状态不等于数千款硬件。

| B 未见型号 | 样本 | Accuracy | Macro-F1 |
|---|---|---|---|
| a10 | 295 | 0.5085 | 0.1843 |
| mi300x | 311 | 0.8424 | 0.4145 |
| mtt_s4000 | 315 | 0.9302 | 0.4651 |

Macro-F1 固定按四类平均；单型号如果某类无样本，对该类F1按0处理，需结合support理解。RF树不能可靠外推训练范围外的数值，硬件类型少、类别不平衡和跨型号性能交互均限制B表现。若B表现不佳，先保留保守回退和待验证；下一步可在冻结新测试集后，按验证集研究剩余有效算力/带宽交互特征，仍需独立基准校准。

## 未知型号和可信度

检查关键参数、动态状态合法性、配额语义、静态数值是否超出训练范围、软件后端是否在合成训练域，以及验证Macro-F1是否达到预先设定0.75。任一范围/后端/质量问题返回待验证；保留 raw_rf 供诊断。即使通过，也只标注 provisional_synthetic_model_only。架构名称未知本身不触发拒绝。

RF 投票比例只是树的输出，不是可靠性。待验证时返回硬约束结果和0.8倍有效能力的保守理论回退；该回退也不保证真实性能。未获得独立性能标签的未知设备仅展示预测，不进入真实性能准确率。NPU、未知后端和缺关键参数设备不接入 NVIDIA 执行模型。

| 例子 | 最终结果 | 原因 |
|---|---|---|
| a10 | 信息不足、待验证 | validation_quality_gate_failed |
| mi300x | 信息不足、待验证 | outside_training_numeric_range; validation_quality_gate_failed |
| mtt_s4000 | 信息不足、待验证 | unvalidated_software_backend; validation_quality_gate_failed |
| intel_max1550 | 信息不足、待验证 | missing_required:fp32_tflops |
| mlu370_x8 | 信息不足、待验证 | missing_required:fp32_tflops; device_type_not_in_unified_GPU_model |
| ascend910 | 信息不足、待验证 | missing_required:memory_capacity_mib; missing_required:memory_bandwidth_gbps; missing_required:fp32_tflops; device_type_not_in_unified_GPU_model; missing_or_invalid_dynamic:free_memory_mib |
| biren166c | 信息不足、待验证 | missing_required:memory_capacity_mib; missing_required:memory_bandwidth_gbps; missing_required:fp32_tflops; device_type_not_in_unified_GPU_model; missing_or_invalid_dynamic:free_memory_mib |
| identity_and_architecture_rename | 信息不足、待验证 | validation_quality_gate_failed |
| missing_required_FP32 | 信息不足、待验证 | missing_required:fp32_tflops |
| unmapped_native_quota | 信息不足、待验证 | unmapped_backend_quota_semantics |

## 分配与释放演示

构造 L40 的五步资源轨迹，每次修改后重新预测，静态硬件哈希保持一致。下表如实呈现 RF 输出；理论参考独立显示，不用其覆盖模型结果。

| 操作 | 剩余MiB | 剩余配额 | 实例 | 训练预测 | 推理预测 | 最终分类 | 理论合同 |
|---|---|---|---|---|---|---|---|
| initial | 45776 | 100 | 0 | True | True | 训推一体型 | balanced |
| allocate_compute_heavy_1000MiB_60quota | 44776 | 40 | 1 | True | False | 训练优先型 | training_preferred |
| allocate_memory_heavy_5quota | 6000 | 35 | 2 | False | False | 当前不满足参考负载要求 | unavailable |
| release_compute_heavy | 7000 | 95 | 1 | False | True | 推理优先型 | inference_preferred |
| release_all | 45776 | 100 | 0 | True | True | 训推一体型 | balanced |

`static-demo.json` 包含22款完整物理资源初始分类及硬件画像。所有动态事件详见 `dynamic-demo.json`，每步包含硬件、资源、两个预测、原因、理论参考和数据类型。具体 Pod 的显存/算力/实例与软件兼容性硬约束仍优先；此轮旧执行器仅运行已支持的三款 NVIDIA 卡。

## 原仿真兼容回归

保持 Binpack、静态分类、动态分类三个策略。静态/动态分别通过适配器接入同一新A模型，静态冻结初始状态，动态逐次预测；Binpack不依赖RF。11张NVIDIA(4090×4、5090×4、L40×3)，相同60个Pod、随机种子101、执行种子707。执行时间仍由第二轮独立的合成性能模型计算，不使用第三轮标签规则。

| 策略 | 完成数量 | 平均等待s | 训练JCTs | 推理Pod P95s |
|---|---|---|---|---|
| binpack | 60 | 2.444 | 200.847 | 6.412 |
| static_rf | 60 | 2.444 | 200.847 | 6.412 |
| dynamic_rf | 60 | 2.201 | 195.064 | 6.137 |

所有策略无资源超分配、结束后显存/配额恢复。这里只验证兼容性，60个Pod的单次回归不支持调度收益结论，资源利用率是预留配额/显存占比，不是实测GPU忙碌率。

## 可复现命令

在 gpu-scheduling-lab 目录 PowerShell：

```powershell
$env:TEMP=(Join-Path (Get-Location) '.tmp'); $env:TMP=$env:TEMP
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
.\.venv\Scripts\python.exe -m gpu_lab.crossgpu.run --output results/offline-v3-replay
.\.venv\Scripts\python.exe -m pytest -q --basetemp .tmp/pytest-round3-replay
```

依赖沿用 requirements-offline.txt 和 requirements-simulation.txt；不需要CUDA。运行清单记录种子、Python/sklearn版本、代码哈希。pytest真实执行结果另存 pytest-results.txt/json。来源审计见 source-audit.md；全部分类监督标签来源为synthetic，规格数据为public_spec，真实性能标签为unknown。
