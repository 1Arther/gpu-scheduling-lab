# HRRN 多 Pod 协调：持久预留与设备回执本地验证

## 范围与状态

本阶段新增一个独立的 ConfigMap 持久预留原型和逐 Pod 回执校验原型，并用 fake Kubernetes、模拟插件及冻结 RF 服务验证。**真实 Volcano 多 Pod vGPU 作业仍被安全闸门阻断，尚未证明在真实集群可以完整启动。** 本阶段没有访问 Kubernetes 集群或执行 GPU 计算，也没有修改 RF 模型、特征、标签、评分和资源账本。

## 实现

`scheduler/volcano-rf/pkg/scheduler/plugins/hrrn/coordination/durable_store.go` 使用同一 ConfigMap 保存 GPU 容量和外部基线、完整 Job 和 Pod 的逐设备需求、交接状态、epoch、序号及节点待交接槽位。预留先检查所有 Pod 的显存、算力、实例容量，失败无部分变更；成功后通过 UID 与 resourceVersion 进行乐观并发控制。测试使用显式 CAS reactor，因为 client-go 默认 fake 不提供 API server 的版本冲突语义。

接管时变更 epoch，将可能已发生外部效果的 Pod 转为不确定状态，继续计费并隔离；不确定写入结果也不能假定失败。`OpenDurable` 使用原 epoch 重新打开账本不意味着旧进程已经退出，不能作为故障恢复。接管和 CAS 不会阻止旧进程、其他调度器、kubelet 或设备插件执行操作：必须另行完成真实外部 fencing、全参与者接入和资源清点。`RecoveryEvidence`、`BaselineEvidence` 和释放观察目前由调用者提供，布尔字段不会自动证明任何事实。

`coordination/ack.go` 校验绑定、插件响应、checkpoint 和节点静默四类模拟来源，核对 Job/Pod UID、节点、交接序号、容器、GPU UUID、显存、算力、实例数、来源和有效期；缺证、错配、在途分配或节点锁未释放时拒绝确认。签名仅为本地模拟信任模型，来源标签和密钥不同不证明现实主体独立，也没有持久化反重放证据。`RecordConsumed` 接收的是外部验证后的断言，不能绕过真实可信验证直接用于生产。

`coordination/checkpoint_receipt.go` 解码 kubelet checkpoint 并验证格式、校验和、Pod/容器/资源、已注册虚拟设备、GPU UUID 和配额；结果不读取集群，也不改变协调状态。历史 checkpoint 可在 Pod 结束后继续留在磁盘上，校验和不是认证签名，不能单独证明设备仍被该 Pod 使用。回执字节哈希匹配目前为保守规则；若插件响应在 checkpoint 写入时重新编码，语义相同而字节不同也会被拒绝。

## 测试场景与证据边界

单 GPU 100 cores，背景占用 30，大作业含两个 40-core Pod，小作业 60：真实冻结 RF 规划器在背景占用下无法给大作业完整规划，小作业可在本地协调模型中预约；释放背景和小作业后，大作业获得完整 80-core 预约，并以模拟绑定、模拟设备插件和模拟确认逐 Pod 交接，最后释放账本。测试检查实际 RF 请求成功、模型版本及无 fallback。`job_durable_rf_integration_test.go` 的 Pod 更新不是 Kubernetes Binding API，模拟确认也不是实际设备插件回执；结果不能证明真实大作业启动。

另有 fake 客户端测试并发预留和接管冲突、旧票据、丢失响应、重启隔离、未确认时保留资源、资源过量和身份不符等。阶段 6 的真实 Go 调度路径测试证明：启用安全闸门后，小作业可旁路，未经证实的多 Pod vGPU 不会进入危险分配路径；它不证明真实多 Pod 完整启动。

冻结文件与阶段 4 基线比较见 `results/hrrn-k8s-stage7-20261027/frozen-rf-comparison.json`。原阶段 4 全量 Go 测试的 PVC 个例不稳定问题不因本阶段相关包回归而消失。本机没有 C 编译器，未运行 Go race 检测。

2026-10-27 的 Linux 回归在 `results/hrrn-k8s-stage7-20261027/full-linux-regression-r1/`：HRRN 34 个、vGPU 38 个、协调模块 33 个顶层测试，共 105 个顶层测试、494 个叶子场景，全部通过且没有跳过。结果中的真实 RF 是本地 CPU 推理；Kubernetes、设备交接和所需来源均为测试替身。检查的 83 个冻结文件无变化，模型 SHA256 为 `ee35cfabe30fffc28ff0be09d3738587d4f9bffbe26386e2a9fae5c5b9e80748`。本轮专用 RF 服务在验证完成后已退出。

## 复现

依赖项目内 Go、Python、缓存和 `Codex-RF-Verify-20261019` WSL。以下在项目根目录启动本地 RF 服务；服务仅允许指定 WSL 客户端，端口 18083/18084 应空闲：

```powershell
$taskGateway = ((& wsl.exe -d Codex-RF-Verify-20261019 -- ip -4 route show default) -split '\s+')[2]
$taskAddress = (& wsl.exe -d Codex-RF-Verify-20261019 -- ip -4 -o addr show eth0) -join ' '
if ($taskAddress -notmatch '\binet\s+(\d+\.\d+\.\d+\.\d+)/') { throw '无法读取 WSL 地址' }
$taskGuest = $Matches[1]
& .venv/Scripts/python.exe scripts/hrrn_rf_server.py --bridge-host $taskGateway --allowed-client $taskGuest
```

保持服务窗口运行，在另一个项目根目录 PowerShell 中执行（每次指定新的 `Attempt`）：

```powershell
$taskGateway = ((& wsl.exe -d Codex-RF-Verify-20261019 -- ip -4 route show default) -split '\s+')[2]
$taskAttempt = 'replay-' + (Get-Date -Format 'yyyyMMddHHmmss')
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test_hrrn_stage7.ps1 -Attempt $taskAttempt -RFEndpoint "http://${taskGateway}:18083/v1/rank"
```

测试完成后在服务窗口按 Ctrl+C。脚本在独立目录保存三个相关包的 Linux 构建和测试日志，并指定阶段 5 保存的历史 kubelet checkpoint 样本。测试通过并不代表 Volcano 仓库全套测试通过。

## 真实集成前仍需完成

1. 核实运行镜像和设备插件源码对应关系，实现可认证、可追踪 Pod UID/请求/UUID/配额的实际确认；禁止用 `bind-phase=success` 或旧 checkpoint 充当消费确认。
2. 让所有竞争资源的原生分配路径读写同一预留账本，并证明与节点锁、CPU/内存、Gang、队列约束及全部 predicates 一致；有界 RF 规划不是这些约束的完整证明。
3. 实现真实的旧调度器 fencing、重启清点、设备释放确认、Pod 部分绑定补偿和重复事件处理；单个 ConfigMap 更新不提供跨 Pod 原子绑定。
4. 在可信假设备插件和真实 Go 调度动作上完成端到端验证，随后才重新评估受控的 sleep 集群实验。现阶段不得解除多 Pod 安全阻断或宣称 C 组已成功。
