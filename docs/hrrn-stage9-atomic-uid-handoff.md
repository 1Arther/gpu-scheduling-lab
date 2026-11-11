# 持久 nonce 与 kubelet 协议：本地验证

## 当前结论

本阶段把 UID 交接 nonce 与完整作业资源预约写入同一个 ConfigMap 事务，并接通官方 Kubernetes 设备插件消息类型与本地 gRPC 测试。**真实多 Pod vGPU 启动仍未实现：真实调度路径继续保持阻断，实际 HAMi 插件、kubelet 和持久交接路径尚未联合运行。**

## 同账本 nonce

`coordination/durable_handoff.go` 扩展阶段七的版本 1 账本：可选 handoff 记录与预约保存在同一个 `ledger.json`。写入时在同一 UID/resourceVersion 更新中核对非隔离状态、Bound Pod、当前节点槽位、epoch/序号以及容器完整设备/配额；不同容器和未释放 Pod 的设备 ID 不能冲突。读出、确认和消费阶段均重新读取并校验账本；一旦某 Pod 已出现交接记录，旧消费入口不能绕过未确认容器。提供 `RecordConsumedWithHandoffs` 对全部容器记录进行检查。

接管后的旧记录保留但不可作为新尝试确认；结果未知、版本冲突、取消和隔离时均不自动释放容量。测试覆盖并发写入、响应丢失、旧票据、多容器、错误配额、设备 ID 复用和最终模拟释放。确认操作目前仍接受调用者的断言，不会自动认证节点或 fencing；没有交接记录的旧版本调用仍有旧行为。ConfigMap 的单对象 CAS 只能约束参与协议的写入者，不能阻止旧调度器、kubelet 或插件继续产生外部副作用。

实际 Kubernetes Binding 是异步的，kubelet 可能在账本的 Bound 观察完成之前发出 Allocate；当前模型会拒绝该早到请求，尚未实现受限等待和可靠重试，因此也没有证明真实系统的启动活性。

向后兼容仅指新代码可以读取没有 handoff 字段的旧账本。旧二进制会因新增字段拒绝读取新账本，部署或回滚前需要版本迁移方案。账本有 768 KiB 上限，历史交接记录目前不做清理，长期运行会因空间上限而停止接受新操作；不能用于生产。

`uidgate/LedgerJournal` 适配 Gate 与同账本记录；不能因为存在它就放开生产调度，也不能用自己调用 `ConfirmHandoff` 代替可信节点观察。

## RPC 与节点读取

`uidgate/RPCAdapter` 使用 `k8s.io/kubelet v0.33.2` 的真实 `DevicePlugin` 消息与 gRPC 接口，在内存传输上测试实际 protobuf 解码、编码、nonce、checkpoint 匹配及同账本写入。它没有实现 ListAndWatch，不接入 HAMi，ClaimSource、Pod 绑定、checkpoint 和节点观察均由测试替身提供，不能注册为实际可用设备插件。

适配器使用现有 `<MiB>m` 形式核对显存，测试条件相当于 `GPUMemoryFactor=1`；HAMi 非 1 factor 或其他配额单位未经适配，必须在正式接入时明确配置并验证，不能据当前测试宣称兼容所有部署。

`LocalCheckpointObserver` 以 Kubernetes 客户端在读取前后核对 Pod/Node UID、resourceVersion、owner、状态及容器资源；Linux 文件读取器采用 no-follow 和受限文件读取，检查类型、权限、owner 与读期间文件变化。观察器刻意不实现 Gate 的 `NodeObserver` 接口：它无法证明 checkpoint 条目新鲜、节点路径是实际 kubelet 挂载、资源独占或旧 RPC 已 fenced，因此不可直接用来放行。

已将官方 kubelet v0.33.2 源码归档与项目已有的 go.sum 哈希核对；离线缓存记录位于 `results/hrrn-k8s-stage9-20261027/pluginapi-source/`。运行测试需要该缓存或可用 Go proxy。实验未访问真实集群或运行 GPU 计算，RF 模型及相关冻结文件未更改。

2026-10-27 的八个相关 Go 包 Linux 回归结果位于 `results/hrrn-k8s-stage9-20261027/full-linux-regression-r1/`：174 个顶层测试、692 个叶子场景全部通过，无跳过。阶段四保存的 83 个冻结文件均与基线一致，RandomForest joblib 的 SHA256 为 `ee35cfabe30fffc28ff0be09d3738587d4f9bffbe26386e2a9fae5c5b9e80748`；本轮本地 RF 测试服务已退出。

## 复现

先按 [阶段七说明](hrrn-stage7-durable-coordination.md) 在一个窗口启动冻结 RF 本地测试服务，再在项目根目录的另一个 PowerShell 窗口执行：

```powershell
$taskGateway = ((& wsl.exe -d Codex-RF-Verify-20261019 -- ip -4 route show default) -split '\s+')[2]
$taskAttempt = 'replay-' + (Get-Date -Format 'yyyyMMddHHmmss')
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test_hrrn_stage9.ps1 -Attempt $taskAttempt -RFEndpoint "http://${taskGateway}:18083/v1/rank"
```

测试结束后关闭 RF 服务。脚本只运行相关包，不代表整个 Volcano 项目通过；阶段四 PVC 不稳定问题仍须单独报告。本机未运行 Go race 检测。

## 仍需完成

1. 实现可信的节点观察者、实际插件 Allocate/PreStart 接入、持久 journal 的授权和可靠的旧 RPC/设备 ID 隔离。当前观察器和 RPC adapter 刻意不能直接提供上述生产保证。
2. 对所有竞争者实施同一预约和节点 fencing；将计划放到真实 Volcano action 时保留全部原生 CPU、内存、队列、Gang、拓扑和 predicates。只读 barrier 目前拒绝所有 vGPU 小作业。
3. 对部分 Kubernetes Binding、插件确认失败、取消、进程或节点重启建立真实补偿和释放证据；不得声称跨 Pod 原子绑定。
4. 使用真实 Volcano action 与实际设备插件代码完成局部端到端测试，验证小作业旁路、双 Pod 启动、失败隔离和释放后，才重新评估集群实验。
