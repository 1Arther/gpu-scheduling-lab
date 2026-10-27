# D_a10_diagnostic

协议：previously_observed_model_diagnostic。全部真值为synthetic理论标签，没有真实性能准确率。

型号/轨迹成员清单见 [split-manifests.json](../split-manifests.json)。新的3款测试型号不参与任何协议拟合/验证；标为诊断的型号已在历史中观察过。

验证集选中特征 `effective_both`；选模冻结见 [选择清单](../selection-frozen-before-test.json)。

| 划分 | 样本 | Accuracy | 固定四类Macro-F1 | 实际类别Macro-F1 | 真值类别数 |
|---|---|---|---|---|---|
| train | 4165 | 1.000000 | 1.000000 | 1.000000 | 4 |
| validation | 1472 | 0.931386 | 0.914934 | 0.914934 | 4 |
| test | 514 | 0.959144 | 0.722454 | 0.963273 | 3 |

| 型号 | Accuracy | 错误 | 接受状态/总数 | 接受后错误 | 状态 |
|---|---|---|---|---|---|
| a10 | 0.959144 | 21 | 273/514 | 0 | previously_observed_or_known_diagnostic |

[完整PR/F1与类别数量](metrics.json) · [混淆矩阵](confusion.png) · [错误样本](test-errors.csv) · [逐状态预测及拒判原因](test-predictions.jsonl)

[特征重要性](feature-importance.csv)为树的杂质重要性，受相关特征与取值范围影响，只用于诊断，不能当作因果解释或泛化证明。

固定四类Macro-F1将没有真值且没有预测的类别记0；实际类别Macro-F1仅平均该划分存在的真值类。两者都给出，不对缺失类别伪造样本。模型拒判与原始RF误差分开记录。
