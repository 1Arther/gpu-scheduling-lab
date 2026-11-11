# C 组多 Pod vGPU 协调：本地实现与边界

## 当前状态

这一阶段提供一个可选的真实调度路径安全闸门、一个独立 vGPU 规划器和一个独立的进程内交接模型。**它们尚未组成可在 Kubernetes 中启动多 Pod 作业的协调器。** 在 `coordinated-complete-job: true` 时，多 Pod vGPU 作业会保持等待，即使显存、算力和实例槽位已经足够；其他合法作业可以继续调度。不能把这一状态说成已经实现 C 组完整启动。

新增开关默认关闭；阶段 4 的既有 C 配置没有自动变安全。仅本地测试用的配置为 `k8s/hrrn-stage6/C-hrrn-coordinated-fail-closed.yaml`。它保留原有 `complete-job: true`，另设 `coordinated-complete-job: true`。若该开关的值无效，或者该开关启用但 HRRN/complete-job 配置不匹配，安全闸门仍会阻止不安全路径，避免静默退回旧的部分规划。多个同名 HRRN 配置取安全开关并集，不能由后一个实例覆盖保护。安全模式中，任何拟放行的作业均须将其已观察成员和声明数量与可核验的 VolcanoJob owner、UID 和实际 spec 副本数核对一致；独立 PodGroup、权限不足或身份不匹配时同样等待。原生 Job 排序和其他资源约束仍由 Volcano 原插件执行。

安全闸门目前只为已审计的 `enqueue, allocate, backfill` 组合编写集成验证。虽然源码中的 `preempt` 和 `reclaim` 也调用 JobValid，尚未完成这两种动作的端到端安全测试；第三方 action 或直接调用 Statement/Session Allocate 的路径不自动受保护。不得据此声称任意插件配置都安全。

## 为什么不能简单重入或清除 NodeLock

现有 `Statement.Allocate` 在整个 Job 确认可提交之前调用 vGPU Allocate；首个成员获取节点锁并写设备注解后，第二个同节点成员被锁阻塞。原始 `Discard` 能回滚内存账本和部分 Pod 元数据，却没有可靠证据证明可以释放该锁。当前源码与已有测试保留旧失败作为诊断，并没有修改旧测试来制造通过。

设备插件的 Allocate 请求本身不包含 Pod UID；本地留存的上游插件源码通过节点与时间注解选择待分配 Pod，锁没有可证明的独占所有者。仅让同一作业重入锁、或者在失败时直接删除锁，都不能证明设备消费对应预期 Pod，也无法处理其他并发请求。上游源码与当前运行镜像并未证明逐字节一致。详见 `results/hrrn-k8s-stage5-20261027/local/nodelock-v2/complete-c-source-review.md`。

## 本轮组件

- `pkg/scheduler/plugins/hrrn/coordination_guard.go` 在 JobValid 阶段拦截声明了 vGPU 的多成员作业，以及成员数未知或未完全观察到的作业，避免进入会写锁和注解的原有分配路径。即使目前看到的成员全是 CPU，真实 Job 的未观察成员也可能请求 vGPU；所以拟放行的作业都需要核对 VolcanoJob spec、副本总数和所有成员的 controller 身份。多成员 Job 的真实任务模板只要包含 vGPU 资源就会被阻断，即使观察到的 Pod 未显示该资源。它同时识别配置的 vGPU 资源键、默认资源键、requests、limits 和 init containers；通过权威成员校验的完整纯 CPU 作业不因 vGPU 交接而被拦截。此安全拦截不是资源可行性证明，也不提供跨对象原子快照；必须通过 admission/RBAC 限制作业 spec 和成员在调度时并发变化。
- `pkg/scheduler/api/devices/nvidia/vgpu/job_planner.go` 对 vGPU 账本深拷贝，在副本上复用冻结的 `rfPlanFor`、校验和 HAMi 资源计数，给完整 Pod 集合提出候选计划。它不锁节点、不发布分配注解、不绑定，也不写原始账本；会调用现有 RF 服务并使用其现有缓存和 fallback。计划前后会检查源账本和配置指纹，但调用方仍须防范并发与 ABA，且快照不能替代持久预约。
- `pkg/scheduler/plugins/hrrn/coordination/` 是隔离、进程内的协调模型：完整计划的三维资源先整体预留，同节点一次至多一个待交接 Pod；身份、设备、配额和消费证据不完整时拒绝继续。发布后结果不确定则保留预约并阻断该节点，等待可信外部证据。模型不操作真实 NodeLock，也不具备持久化、跨进程 fencing 或设备插件回执适配器。

规划器在每个节点只尝试冻结 RF 返回的候选，跨节点做有界回溯；候选耗尽和搜索预算用尽不能证明数学上的全局不可行。它只检查 vGPU，并不替代 CPU/内存、队列配额、Gang、亲和、卷、拓扑及其他 Volcano predicates。返回的计划不授权提交，也不是持久资源预约。

## 本地验证的含义

真实 Go action、原生 NodeLock、fake Kubernetes API 与冻结 RF 服务联合测试中：背景占 30 cores、大作业需 2×40 时，受保护模式在任何大作业设备注解或锁写入之前将其旁路；已核验 VolcanoJob owner 和单副本 spec 的 60 cores 小作业可以获得 GPU。模拟任务结束后，使用原生 `NodeInfo.RemoveTask`/`SubResource` 检查显存、cores 和实例账本归零；没有真实插件 ACK，因此测试不伪造 NodeLock 释放。

单独的 RF 规划器和内存协调器联测中，资源不足时没有部分计划；资源充足后可以整体预留 2×40，并在模拟插件提供明确身份及消费证据时逐 Pod 交接与释放。这一联测的 Binder、设备消费、回执和锁均为模拟，不证明真实跨 Pod 原子绑定或设备插件行为。真实调度的安全闸门不会放行该多 Pod 计划。

## 复现本地 Go 回归

在项目目录的 Windows PowerShell 中执行。下面启动的 RF 桥只用于本机 WSL 测试，复用冻结模型；每次使用独立输出目录，最后仅停止本次启动的进程。需要已配置的 `Codex-RF-Verify-20261019` WSL、项目内 Go 1.26.3、Python 环境和依赖缓存：

```powershell
$taskRoot = (Get-Location).Path
$taskAttempt = 'replay-' + (Get-Date -Format 'yyyyMMddHHmmss')
$taskOutput = Join-Path $taskRoot ("results/hrrn-k8s-stage6-20261027/bridge-" + $taskAttempt)
New-Item -ItemType Directory -Path $taskOutput -ErrorAction Stop | Out-Null
$taskGateway = ((& wsl.exe -d Codex-RF-Verify-20261019 -- ip -4 route show default) -split '\s+')[2]
$taskAddress = (& wsl.exe -d Codex-RF-Verify-20261019 -- ip -4 -o addr show eth0) -join ' '
if (-not $taskGateway -or $taskAddress -notmatch '\binet\s+(\d+\.\d+\.\d+\.\d+)/') { throw '无法读取 WSL 地址' }
$taskGuest = $Matches[1]
$taskBridge = Start-Process -FilePath (Join-Path $taskRoot '.venv/Scripts/python.exe') -ArgumentList @('scripts/hrrn_rf_server.py','--bridge-host',$taskGateway,'--allowed-client',$taskGuest) -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskOutput 'rf-service.jsonl') -RedirectStandardError (Join-Path $taskOutput 'rf-service-error.log') -PassThru
try {
  $taskReady = $false
  for ($i=0; $i -lt 30; $i++) {
    if ($taskBridge.HasExited) { throw 'RF 测试桥已退出，查看日志' }
    try { if ((Invoke-RestMethod 'http://127.0.0.1:18084/health').model_ready) { $taskReady=$true; break } } catch {}
    Start-Sleep -Seconds 1
  }
  if (-not $taskReady) { throw 'RF 测试桥未就绪' }
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test_hrrn_stage6.ps1 -Attempt $taskAttempt -RFEndpoint "http://${taskGateway}:18083/v1/rank"
  if ($LASTEXITCODE -ne 0) { throw 'Go 回归失败，请查看该次独立日志' }
} finally {
  if (-not $taskBridge.HasExited) { Stop-Process -Id $taskBridge.Id }
}
```

`test_hrrn_stage6.ps1` 为每次运行创建新的结果目录，并在 WSL 中实际执行三个 Linux 测试二进制。历史的 Stage4 全量 Go 测试仍有 PVC 个例失败，本轮相关包通过不能替代“全量测试全绿”。本机未提供 C 编译器，Go `-race` 未运行。

2026-10-27 的最终本地回归记录在 `results/hrrn-k8s-stage6-20261027/full-linux-regression-r4-20261027/`：HRRN 34 个、vGPU 37 个、协调模型 10 个顶层测试通过，共 423 个叶场景，零失败、零跳过。另有真实冻结 RF + 模拟设备消费者的联测结果在 `results/hrrn-k8s-stage6-20261027/coordination/`。与阶段 4 保存的 83 个冻结文件逐一比较均相同，RandomForest joblib SHA256 仍为 `ee35cfabe30fffc28ff0be09d3738587d4f9bffbe26386e2a9fae5c5b9e80748`。本轮仅访问本机 WSL 和本地 RF 测试桥，未访问集群或运行 GPU 计算。测试桥已在验证后停止。

## 后续真实 C 组的必要工作

需要先确定运行中的设备插件源码与镜像对应关系，并实现可持久恢复的逐节点交接：对外最多一个可消费候选、确保 UID 与实际设备回执对应、持久预留其余资源、处理崩溃/重启与已有绑定部分失败。必须证明跨 Session 与其他调度器竞争时的锁所有权和恢复语义，并对真实 kubelet/设备插件错绑、超时、重复响应、节点锁与资源释放建立测试。若无法可靠获得消费证据，应继续阻断，不能用注解 `bind-phase=success` 冒充设备消费 ACK。

只有上述 Go 本地、fake-client 和设备插件联动测试通过后，才可重新评估独占时段的有限 sleep 实验。任何步骤都不能将多个 Pod 的 Kubernetes Binding 称为原子操作，除非另行实现并验证该保证。
