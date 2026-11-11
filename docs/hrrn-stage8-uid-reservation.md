# 多 Pod UID 关联与持久预约：本地实现边界

## 当前结论

本轮在真实 Volcano Go 分配入口接入了一个默认关闭的持久账本读取及硬阻断实验开关，并实现独立的 UID 关联原型。**尚未完成真实设备插件接入，也没有让真实多 Pod vGPU 作业启动。** 启用实验开关时，所有 vGPU 分配都会被阻断，包括本应旁路的小作业；preempt 和 reclaim 也会停用。不要部署该开关或将本地测试描述为 C 组成功。

## UID 两阶段关联原型

标准设备插件 `Allocate` 和 `PreStartContainer` 请求均不带 Pod UID。原型 `uidgate/` 从已确认的预约获取目标 Job/Pod/容器、设备和 quota，为 Allocate 响应加入 256 位随机 nonce，并要求在返回前将唯一设备 ID、计划和响应摘要记录到 Journal。PreStart 阶段从节点观察者读取 kubelet checkpoint，以设备 ID、nonce、实际 Pod UID、容器、GPU UUID 与配额共同验证，任何重叠、旧 nonce、错 UID、未知响应字段或不确定状态均拒绝。

这一方案依赖真实节点上的 kubelet 按预期先写 checkpoint 再调用 PreStart、受保护的 checkpoint 读取、身份可信的 Pod 绑定观察、可恢复的持久 Journal、设备 ID 和旧 RPC 的可靠隔离。当前这些生产组件均未实现：Journal 和观察者都是接口，测试使用替身；尚未修改或接入已部署的设备插件。旧 NodeLock 无可靠所有者令牌，不能承担本协议的 fencing。设备 ID 的 init-container 复用、重启时 kubelet 在重新 Allocate 前调用 PreStart 等情况当前保守拒绝，可能使容器无法启动。

响应摘要遵循 [Kubernetes v0.33.2 设备插件 protobuf 定义](https://github.com/kubernetes/kubelet/blob/v0.33.2/pkg/apis/deviceplugin/v1beta1/api.proto)，覆盖 env、mount、device、annotation 和 CDI 内容，规范化 map 顺序并保留 repeated 字段顺序；未知字段和有歧义的重复字段被拒绝。保存的历史 checkpoint 的内容及手工重排测试已通过；由于本地缺少对应的生成类型且下载超时，尚未完成真实 pluginapi/kubelet 解码再编码的互通测试。

## 调度路径与 Pod 更新

`coordination.ReservationView` 从同一个 ConfigMap 版本读取未释放预约、UID、epoch、资源版本和隔离状态，排除外部 baseline；它不锁定版本、证明 baseline 或提供跨调度器 fencing。

`reservation-barrier` 是默认关闭的实验选项，注册在真实 Session 的硬 veto 路径，覆盖 Predicate、Allocate 和 Pipeline。由于读取与后续副作用之间存在竞态，它不因空账本或当前无冲突就放行 vGPU；不可读取和隔离状态也拒绝。启用任何硬 veto 的 Session 禁用 preempt 与 reclaim，避免先驱逐其他 Pod 再拒绝目标；这会影响 CPU 作业。该实现不能作为持续运行的生产调度器。

`vgpu/PatchPodAnnotationsIfUnchanged` 是新协调路径可使用的独立助手：同一次 JSON Patch 检查 Pod UID 和 resourceVersion，再更新注解。旧设备分配和 RF 路径未接入这一助手，仍存在同名替换 ABA 与 patch 错误时补偿不足的已知风险。网络写入结果不确定时不能直接重试或释放资源。

## 验证范围

本地测试包括真实 Go 调度入口和 fake Kubernetes、RFC6902 HTTP 测试服务、冻结 RF 的 CPU 推理，以及模拟 kubelet checkpoint/设备插件。针对 UID 交换、旧 nonce、设备 ID 复用、并发更新、隔离接管和保留资源均有负例。持久账本与 UID gate 的联合测试仍使用内存 Journal、模拟绑定和模拟节点观察者，不是 kubelet 或设备插件的真实端到端测试。

2026-10-27 八个相关 Go 包的 Linux 回归在 `results/hrrn-k8s-stage8-20261027/full-linux-regression-r1/`：151 个顶层测试和 625 个叶子场景通过，无跳过；回归后新增的 `OnSessionOpen` 配置接线测试在 `results/hrrn-k8s-stage8-20261027/veto/hrrn-on-session.log` 单独运行通过，含六个场景。比较阶段四记录的 83 个冻结文件无变化，模型 SHA256 为 `ee35cfabe30fffc28ff0be09d3738587d4f9bffbe26386e2a9fae5c5b9e80748`。本轮测试 RF 服务已退出。

本阶段未访问集群、运行 GPU 任务或修改 RF 模型。与历史全量测试相比，原 PVC 不稳定案例没有因本轮测试自动解决；没有 C 编译器，因此未运行 race 检测。

## 复现

先按 [阶段七说明](hrrn-stage7-durable-coordination.md) 的第一段命令在独立窗口启动仅允许本地 WSL 客户端的冻结 RF 服务。另一个项目根目录 PowerShell 中运行：

```powershell
$taskGateway = ((& wsl.exe -d Codex-RF-Verify-20261019 -- ip -4 route show default) -split '\s+')[2]
$taskAttempt = 'replay-' + (Get-Date -Format 'yyyyMMddHHmmss')
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test_hrrn_stage8.ps1 -Attempt $taskAttempt -RFEndpoint "http://${taskGateway}:18083/v1/rank"
```

执行结束后关闭测试 RF 服务。脚本会在新的独立结果目录中运行 Linux Go 测试并保存日志；所需 WSL、项目内 Go、Python、缓存及历史 checkpoint 夹具须已存在。

## 真实端到端前的必要工作

1. 在设备插件实现并实际接入 nonce 与 PreStart 校验，提供受保护的节点观察者和持久、可恢复的 Journal，并测试真实生成类型的 protobuf 编解码。
2. 建立全竞争者参与的原子资源预约与有所有者、可安全恢复的节点隔离；单次 ConfigMap 读取、旧 NodeLock 或人工布尔断言不能满足这一要求。
3. 让完整计划通过全部原生 Gang、CPU/内存、队列、拓扑和 predicate，并安全地逐 Pod 提交；对部分成功的绑定和插件失败实现可验证的补偿。不能声称跨 Pod 原子绑定。
4. 用实际 Volcano action 与实际设备插件代码开展本地端到端测试，覆盖小作业旁路、两个成员顺序启动、错误 Pod、重启与释放，之后再评估受控集群实验。
