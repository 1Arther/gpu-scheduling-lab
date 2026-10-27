# 第七轮最后复核与最小待批准操作

> 此文为审批前记录。后续已按用户明确授权完成一次真实窗口并恢复原scheduler，见[真实实验报告](round7-real-kubernetes-report.md)。原草案和快照保留。

**当前：未授权、未部署、真实实验 Pod 为 0。可以不清空显存、保留原 DQN 部署进行本实验；前提是建立可核实的提交窗口并完成镜像准备。集群级准入策略不再是默认操作。** 本文替代旧手册中“必须先安装准入窗口”的步骤；旧证据、源码、模型和结果保留。

## 本次只读结果

2026-10-19 北京时间 **18:43–18:49**，重新通过4090管理节点查询真实API、日志、CRI、检查点及现有文件。原始命令、退出码、时间和SHA保存在 `results/round7-final-review-20261019/raw/`，汇总为同目录 `review.json`。

| 项目 | 本次观测 |
|---|---|
| 集群 | Kubernetes v1.35.0，4注册节点、3 Ready；A40不参与 |
| 活跃/Pending/Terminating GPU/vGPU/DRA Pod | 0；同时检查requests、limits、initContainers、DRA和分配注解 |
| 潜在自动提交来源 | 当前Deployment/StatefulSet/DaemonSet/ReplicaSet/RC/Job/CronJob中，GPU/DRA请求模板0；不能由此证明外部脚本、用户和未来提交已受控 |
| DRA | ResourceClaim 0，ResourceSlice 0 |
| 插件在途注解/节点锁 | 4090有限时间戳、未消费分配0；`hamivgpu`不存在；8个旧终态Pod的临时注解为空、时间戳为uint64最大值 |
| 原DQN | scheduler和DQN服务各1 Available，GPUSelectPolicy仍为dqn-job，配置RV 21788675；运行中二进制SHA仍为`9f2e1ae3359ce234fd048bc48aab082add99a686631808908b5ae518bdebf4f4` |
| DQN在途证据 | 观察到60次调度周期结束，无DQN预测、设备Add/Sub或分配/绑定匹配日志；两个旧空PodGroup每周期被跳过。没有现存cache dump，不能声称直接观测私有账本为空 |
| Device Plugin | v1.11.0三目标节点Ready；4090同时注册普通GPU和vGPU，两入口容量重叠 |
| 4090物理状态 | 每卡6695MiB已用、17516MiB空闲、利用率100%；观察到的进程属于宿主机会话cgroup，不是该Pod逻辑账本的申请量，不作为本轮否决项 |
| sleep基础条件 | RuntimeClass nvidia存在；固定镜像已缓存，`/bin/sleep` ELF和glibc校验一致；尚未实测新Pod的Runtime/Allocate/preload启动 |
| kubelet检查点 | 12条历史资源记录、4个UID，均未匹配当前API Pod。保留原记录，不清理；不能解释为当前12个Pod或调度器12次预留 |
| 实验控制面 | RF Deployment、窗口Policy/Binding不存在。RF/scheduler正式镜像尚未构建并固定digest |

目标四卡：

| GPU UUID | 注册逻辑显存MiB | 注解重建剩余MiB / cores / 实例 |
|---|---:|---|
| GPU-7b94f673-ce00-719b-2c94-706baa032dae |24564|24564 / 100 / 10|
| GPU-fb6aaeaf-9ca4-3c9a-7b3b-f56714c1f7cd |24564|24564 / 100 / 10|
| GPU-28cf7186-0bdd-be9f-92e5-3c59b4e92c1e |24564|24564 / 100 / 10|
| GPU-27be0024-0498-4114-e6b9-b2d0db148309 |24564|24564 / 100 / 10|

这里的剩余量来自**活跃Pod注解重建**，不是直接读取DQN内存，也不是Node.allocatable或物理空闲显存。完整清单在 `4090-inventory.json`。基线重建预留率0、参考4096/20/1碎片率0，不是新真实实验结果。

## 最小风险选择

优先独立 `volcano-rf-test`，仅t3dgq，单副本、独立Lease、Python loopback sidecar，CPU计算只加载冻结模型一次。先一个Pod消费完成，再提交第二个；不并行启动多个测试分配。

**协调窗口可以替代强制准入，但不是技术互斥。** 管理员需要确认所有可能到达4090的普通GPU、vGPU、DRA提交者和自动化已协调暂停新请求，禁止未协调的重试、直接绑定及分配注解修改。无需暂停CPU Pod、宿主机进程或其他节点已确认不可能使用4090的任务。本次没有取得这项确认。只发现并发请求后再停止提交不能撤回已发生的重复预留。

窗口开始后须复查全体Pod、待消费注解、NodeLock及旧调度循环，核对任何既有分配UID/失败回滚。当前连续零请求和安静日志是支持证据，不是私有预留的直接证明。若管理员无法接受/核实排空依据，或者不能控制所有竞争入口，**不启动RF**；再单独审批强制准入窗口，或采用统一scheduler灰度。增加准入本身也不能自动消除旧的私有预留。

因此：**不要求默认安装集群级VAP；也不批准当前无协调、无保护地并行调度。** 当前未发现确定的未完成GPU预留冲突，仍缺协调与排空确认。统一scheduler灰度会升级/重启原DQN scheduler，本轮不采用。Namespace、不同schedulerName、Lease、白名单、taint均不作为共享账本证明。

实际源码依据（均相对于独立 `scheduler/volcano-rf`）：

- `pkg/scheduler/cache/cache.go:addEventHandler`：接收外部scheduler已绑定Pod，不接收其未绑定私有计划；`AddBindTask`有进程内绑定队列。
- `.../vgpu/dqn_client.go:queryDQNJobOrderedGPUIndexes`：10秒缓存是GPU排序建议，不是跨scheduler资源锁；RPC有3秒超时，不能用等待10秒推断所有预留已释放。
- `.../vgpu/device_info.go:Allocate`：原生先记本地资源，再Patch注解；`hamicore.go:AddPod/SubPod`更新配额。旧配置没有NodeLockEnable，新RF启用锁也不能强制旧DQN与普通GPU接口参与互斥。
- `.../vgpu/rf_job.go:allocateRF/commitNativePlan`：冻结模型建议影响原生UUID，提交前重新检查；HTTP不占账本写锁。锁只保护同进程。
- `pkg/scheduler/cache/dumper.go:ListenForSignal`：现有dump由信号触发；本次只查已有文件，未发信号。
- 保存的官方插件v1.11.0 `pkg/plugin/vgpu/util/util.go:GetPendingPod/getOldestPod`：按节点/时间取待消费Pod，不按schedulerName隔离；故须等待前一Pod消费完成。

## 文件与待执行命令

复用 `k8s/round7-logical-window/00–04`、`test-a-training.yaml`、`test-b-inference.yaml`、`test-d-after-release.yaml`、`test-e-control.yaml`、`test-f-fallback.yaml`。均单容器、`/bin/sleep 600`、整数1/4096MiB/20，指定t3dgq及nvidia RuntimeClass。初始不扩展5090/L40。

**现有Deployment仍是replicas=0和占位镜像，不能直接apply整目录。** 新增本地渲染器拒绝tag/占位镜像，仅接受实际`repository@sha256:...`；默认包不含VAP/Binding或DQN补丁。它不会调用kubectl或授权部署。构建文件复用 `k8s/round7/Dockerfile.scheduler` 和 `Dockerfile.rf`；前者必须复制已验证的独立Linux二进制，不能使用原在线文件。构建/传输镜像后需确认t3dgq缓存精确digest，不能用测试fixture哈希。

以下变量必须来自实际镜像构建/导入记录；当前未赋虚假值：

```bash
# 本地生成文件，不写集群。两镜像尚待构建和缓存检查。
python3 scripts/round7_render_minimal.py --scheduler-image "$SCHEDULER_IMAGE" --rf-image "$RF_IMAGE" --output-dir build/round7-approved
# 只读复查；结果仍不等于自动批准。
python3 scripts/round7_window_preflight.py --sudo-kubectl --kubeconfig /etc/kubernetes/admin.conf --node t3dgq
```

**以下是待批准写操作，当前一条也未执行。** 仅在窗口、排空、节点启动授权、镜像与明确写授权齐全后，管理员在项目根目录执行：

```bash
k() { sudo -n kubectl --kubeconfig=/etc/kubernetes/admin.conf "$@"; }
k apply -f build/round7-approved/control-plane.yaml  # 创建独立控制面，副本仍为0
k -n gpu-rf-system scale deployment volcano-rf-test --replicas=1
k -n gpu-rf-system rollout status deployment/volcano-rf-test --timeout=120s
# 先核对leader、缓存同步、目标4UUID、model_loaded版本和500ms超时，再开始：
k apply -f build/round7-approved/test-a-training.yaml
# 收齐A的原生Allocate、永久注解、插件消费证据后才提交B：
k apply -f build/round7-approved/test-b-inference.yaml
```

最小新增对象：两个实验Namespace、SA/RBAC（集群读、t3dgq节点注解写、两实验Namespace的Pod/PG写）、独立Queue、测试Quota、配置CM、一个双容器Deployment及独立Lease、自己的PG/Pod。scheduler还会写自己的事件/状态、t3dgq的原生握手/短时锁/Pod设备注解。**不改DQN Deployment/ConfigMap/二进制、不改节点标签/taint、驱动或插件，不默认创建集群准入对象。** 共享节点注解仍是影响面，不宣称完全零影响。

采集沿用 `round7_capture.py`、`round7_collect.py --supplement`、原生commit/SubResource日志。按Pod UID关联RF建议UUID、成功Allocate永久`vgpu-ids-new`、vgpu-number检查点AllocateResponse中的`NVIDIA_VISIBLE_DEVICES`。临时注解可能清空；Running和虚拟DeviceID均不能代替三方证据。

只删除自己真实UID的A（下节），保留scheduler运行，验证SubResource和配额恢复后，提交 `test-d-after-release.yaml`。随后清理所有测试分配、恢复相同基线，以 `rf-v7-config-no-prediction` 切换**独立Deployment**配置后重复同一 `test-e-control.yaml`；切回on重复。服务故障对照改用`rf-v7-config-service-unavailable`，只将RF endpoint置为loopback:1，不动DQN服务。

```bash
# 每次切换前，必须先释放自己的Pod并验证基线恢复；只改本轮Deployment。
k -n gpu-rf-system patch deployment volcano-rf-test --type=strategic -p '{"spec":{"template":{"spec":{"volumes":[{"name":"config","configMap":{"name":"rf-v7-config-no-prediction"}}]}}}}'
# 开启学习项：name改rf-v7-config-on；异常对照：name改rf-v7-config-service-unavailable。
```

## 停止与回滚

出现未协调GPU/DRA请求或分配、锁不释放、UUID任一不一致、超分配、账本不恢复、镜像/runtime/插件异常、影响已有业务时，停止自己的后续提交并保留证据。**不结束其他进程，不手动清NodeLock/checkpoint，不重启组件。** 不能仅停止RF来假装已释放测试资源。

删除自己的A的准确流程（UID预条件由现有脚本在API Server强制检查）：

```bash
k -n gpu-rf-tests get pod rf-v7-a-training -o json > own-a.json
python3 scripts/round7_record_ledger.py --input own-a.json --output a-ledger.json
python3 scripts/round7_rollback.py --ledger a-ledger.json  # 先展示计划
# 仅获准清理后，以同一管理员身份执行；不复制admin.conf：
sudo -n env KUBECONFIG=/etc/kubernetes/admin.conf GPU_LAB_ROUND7_ROLLBACK_APPROVED=yes python3 scripts/round7_rollback.py --ledger a-ledger.json --execute
```

全量收尾：导出自己Label的测试Pod真实UID→同脚本删除→原生释放/插件/账本确认→独立Deployment缩到0并确认Pod退出→按真实UID导出/清理本轮Deployment、CM、Quota、SA、Roles/Bindings、独立Queue。具体导出命令沿用 `round7-logical-runbook.md` 第7节；**没有安装准入的本方案跳过该节Policy/Binding操作**。保留空Namespace/Lease作审计；不删除root/default Queue及任何DQN对象，不清旧检查点/共享握手。若另选强制窗口，先停止RF并释放其Pod，最后才撤除批准的新Binding/Policy。

## 验证与剩余条件

本次在4090独立目录 `/home/zbs/rf-round7-final-review-20261019` 实际运行准备测试，**39 passed，Linux，2.52s**；本机沙箱因回环/临时目录权限失败，解除该限制并指定项目临时目录后也39 passed。均CPU、无真实Kubernetes客户端。Go源码本次未修改，沿用上次Linux编译/282个Go通过事件（含父测试/子测试）证据，不冒充本轮新执行。在线二进制、独立二进制、模型SHA本次重新读取一致。

本次修正预检：缺少VAP不自动否决，有VAP也不自动批准；新渲染器不含生产变更/准入，未填真实digest即拒绝生成可部署包。`execution-gate.json`仍为false。

当前尚缺：**管理员确认的节点使用/可控制提交源的短时窗口、旧私有/在途预留的排空核对、两个新镜像及digest缓存、明确写操作批准。** 若协调/排空无法得到足够依据，必须改选获准的强制边界或单scheduler方案；当前不能安全直接启动。新增Pod逻辑分配率、三方UUID一致率、释放率、RF on/off差异仍未测，保持未执行，不能填100%或0超分配。
