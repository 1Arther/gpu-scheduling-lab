# known_states

协议：known_model_unseen_episode。全部真值为synthetic理论标签，没有真实性能准确率。

型号/轨迹成员清单见 [split-manifests.json](../split-manifests.json)。新的3款测试型号不参与任何协议拟合/验证；标为诊断的型号已在历史中观察过。

验证集选中特征 `effective_compute`；选模冻结见 [选择清单](../selection-frozen-before-test.json)。

| 划分 | 样本 | Accuracy | 固定四类Macro-F1 | 实际类别Macro-F1 | 真值类别数 |
|---|---|---|---|---|---|
| train | 3667 | 1.000000 | 1.000000 | 1.000000 | 4 |
| validation | 1240 | 0.999194 | 0.998986 | 0.998986 | 4 |
| test | 1244 | 1.000000 | 1.000000 | 1.000000 | 4 |

| 型号 | Accuracy | 错误 | 接受状态/总数 | 接受后错误 | 状态 |
|---|---|---|---|---|---|
| a10 | 1.000000 | 0 | 103/103 | 0 | previously_observed_or_known_diagnostic |
| a100_sxm80 | 1.000000 | 0 | 82/108 | 0 | previously_observed_or_known_diagnostic |
| a30 | 1.000000 | 0 | 125/125 | 0 | previously_observed_or_known_diagnostic |
| a40 | 1.000000 | 0 | 97/97 | 0 | previously_observed_or_known_diagnostic |
| h100_sxm | 1.000000 | 0 | 99/99 | 0 | previously_observed_or_known_diagnostic |
| h200_sxm | 1.000000 | 0 | 112/112 | 0 | previously_observed_or_known_diagnostic |
| l4 | 1.000000 | 0 | 97/101 | 0 | previously_observed_or_known_diagnostic |
| l40 | 1.000000 | 0 | 95/95 | 0 | previously_observed_or_known_diagnostic |
| l40s | 1.000000 | 0 | 111/111 | 0 | previously_observed_or_known_diagnostic |
| rtx3090 | 1.000000 | 0 | 79/79 | 0 | previously_observed_or_known_diagnostic |
| rtx4090 | 1.000000 | 0 | 99/99 | 0 | previously_observed_or_known_diagnostic |
| rtx5090 | 1.000000 | 0 | 115/115 | 0 | previously_observed_or_known_diagnostic |

[完整PR/F1与类别数量](metrics.json) · [混淆矩阵](confusion.png) · [错误样本](test-errors.csv) · [逐状态预测及拒判原因](test-predictions.jsonl)

[特征重要性](feature-importance.csv)为树的杂质重要性，受相关特征与取值范围影响，只用于诊断，不能当作因果解释或泛化证明。

固定四类Macro-F1将没有真值且没有预测的类别记0；实际类别Macro-F1仅平均该划分存在的真值类。两者都给出，不对缺失类别伪造样本。模型拒判与原始RF误差分开记录。
