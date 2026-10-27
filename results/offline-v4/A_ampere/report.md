# A_ampere

协议：same_architecture_unseen_model。全部真值为synthetic理论标签，没有真实性能准确率。

型号/轨迹成员清单见 [split-manifests.json](../split-manifests.json)。新的3款测试型号不参与任何协议拟合/验证；标为诊断的型号已在历史中观察过。

验证集选中特征 `effective_compute`；选模冻结见 [选择清单](../selection-frozen-before-test.json)。

| 划分 | 样本 | Accuracy | 固定四类Macro-F1 | 实际类别Macro-F1 | 真值类别数 |
|---|---|---|---|---|---|
| train | 2067 | 0.997581 | 0.747812 | 0.997083 | 3 |
| validation | 512 | 0.792969 | 0.598596 | 0.798128 | 3 |
| test | 522 | 0.957854 | 0.705817 | 0.941090 | 3 |

| 型号 | Accuracy | 错误 | 接受状态/总数 | 接受后错误 | 状态 |
|---|---|---|---|---|---|
| rtx_a6000 | 0.957854 | 22 | 0/522 | 0 | fresh_model_held_out_this_round |

[完整PR/F1与类别数量](metrics.json) · [混淆矩阵](confusion.png) · [错误样本](test-errors.csv) · [逐状态预测及拒判原因](test-predictions.jsonl)

[特征重要性](feature-importance.csv)为树的杂质重要性，受相关特征与取值范围影响，只用于诊断，不能当作因果解释或泛化证明。

固定四类Macro-F1将没有真值且没有预测的类别记0；实际类别Macro-F1仅平均该划分存在的真值类。两者都给出，不对缺失类别伪造样本。模型拒判与原始RF误差分开记录。
