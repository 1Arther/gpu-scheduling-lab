# 第八轮材料入口

本轮仅离线；无SSH、集群写操作、模型训练、规则或评分权重调整。

- [完整论文实验报告与局限](../../docs/round8-thesis-experiments.md)
- [五步动态预测CSV](dynamic-path.csv) / [完整原始响应与账本JSON](dynamic-path.json)
- [冻结文件前后哈希](frozen-artifact-audit.json)
- [各轮统一汇总](unified-experiments.csv)
- [分类全部划分](classification-all-splits.csv) / [逐型号](classification-per-model.csv) / [逐类别](classification-per-class.csv)
- [划分审计](split-audit.csv) / [特征消融](feature-ablations.csv) / [冻结特征顺序](frozen-feature-order.csv)
- [三策略仿真均值与标准差](simulation-main.csv) / [相对变化，负值为变差](simulation-relative-changes.csv)
- [180组原生消融汇总](native-rf-ablation-families.csv)
- [真实六Pod](real-six-pods.csv) / [真实候选评分](real-candidate-scores.csv) / [真实资源快照](real-native-resource-transitions.csv)
- [真实释放](real-release-checks.csv) / [真实on-off](real-on-off.json)
- [源文件与SHA审计](evidence-source-index.csv) / [版本](versions.json) / [Go修改清单](volcano-modified-files.csv)
- [源码函数位置](source-call-chain.csv) / [UUID输入机制核查](uuid-selection-source-audit.json)
- [四卡占位可行性离线回放](same-gpu-control-fixture.json)
- [待批准sleep Pod、精确步骤和回滚](../../k8s/round8-pending-approval/README.md)
- [本轮测试及环境处理记录](test-results.json)

五步预测：24564/100/0 →23564/40/1 →6000/35/2 →7000/95/1 →24564/100/0，
对应训推一体→训练优先→参考不足→推理优先→训推一体。
`dynamic-path.csv` 的两类评分使用相同64MiB/1core诊断请求，便于跨状态比较，不是参考负载性能。
主Pod真实离线评分在JSON的events中（1000/60和17564/5）。全部UUID/UID为SIM，原生与插件UUID列留空。

## 图表与建议图注

每组均有同名PNG和SVG，SVG字体为路径。不要去掉图题中的证据范围。

|文件前缀|论文图注及解释|
|---|---|
|01-dynamic-transition|冻结RF在同一4090合成逻辑状态下的四类切换与释放恢复；尚未执行本轮真实Pod。|
|02-resource-phase-map|固定一个训练实例的540状态扫描；剩余100cores意味着已有0配额不限流实例，按现有机制进入待验证。不是独立测试集。|
|03-classification-protocols|不同分组协议的合成标签Accuracy、固定四类F1与安全门覆盖；原始概率与可靠性分开。|
|04-confusion-matrices|已知状态、Ada留型号、多架构留型号的理论标签混淆矩阵；行真值、列预测。|
|05-synthetic-scheduling|相同轨迹三种策略的六指标均值与样本标准差，3种子；时间均为合成执行模型输出。|
|06-native-rf-ablation|180组真实Go设备分配fake-client配对消融；37组学习项独立UUID变化，不能解释为性能收益。|
|07-real-allocation-evidence|真实六个sleep Pod的UUID证据与相同4090状态的on/off等值学习项；真实on/off UUID未变。|
|08-feature-ablation|B协议8组合的验证/测试F1，模型选择只依赖验证；有效代理帮助拟合理论标签。|
|09-core-algorithm-flow|Pod显式任务类型、原生硬过滤、冻结RF建议、Go重新校验、原生Allocate、插件消费与释放闭环。另附Mermaid源。|

## 复现

项目根目录运行，使用已有venv，不访问集群：

```powershell
.venv/Scripts/python.exe -m gpu_lab.round8
.venv/Scripts/python.exe -m gpu_lab.thesis
.venv/Scripts/python.exe -m pytest tests/test_round8.py -q
```

所有新增实验代码都只加载冻结joblib。历史Linux Go和真实Kubernetes结果来自保存证据，不冒充本轮重跑。
`test-results.json` 记录更完整的运行时/解析器回归命令，包含本机HTTP测试，需要允许127.0.0.1连接。
首次受限运行的失败XML保留为 `pytest.xml`，最终实际通过结果单独保留，不覆盖原始失败记录。
