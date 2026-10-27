# 第七轮调整：RF-vGPU 逻辑分配的集群可行性复核

> 最后复核已更新：[最小待批准操作](round7-final-approval.md)。优先评估管理员协调提交窗口；本文原有“先安装集群级准入”的命令现为另行审批的备选，不得默认执行。旧采样和验证记录保留。

## 结论

**只运行 sleep 时，物理显存被其他应用占用不再单独构成 NO-GO。当前不能直接开始真实测试，原因是尚未获写操作授权，且防止双 scheduler 重复预留的准入窗口尚未建立、传播验证尚未执行，实验镜像也未完成构建与固定 digest。**

短期建议采用 **B：独立 RF scheduler + 经批准的短时 GPU 准入窗口**。它不升级或停用 DQN scheduler，不改 DQN 配置，也不清空显存。窗口内暂停其他用户新建 GPU/DRA Pod，确认先前 GPU 请求与待消费分配均为空后，只放行本轮规定的 RF sleep Pod。这是**协调新增逻辑预留的时间边界**，不是两个 scheduler 获得了共享内存账本。

如果不能批准暂停新 GPU/DRA 请求，或准入生效后仍无法排除先前未发布／在途预留，就不执行 B。长期采用 A 的统一调度权威更合理，但需要升级原 scheduler 的二进制并重建其 Pod；本次未获此授权。两方案不能同时启用来绕过边界检查。

旧 `round7-preflight-report.md`、旧 YAML 和结果作为历史快照保留。本报告修正其将物理繁忙／独占整卡作为本轮逻辑实验前提的判断，不改写旧证据。

## A/B/C/D 重新区分

| 问题 | 本轮含义与处理 |
|---|---|
| A：宿主机物理显存／利用率 | 与逻辑 quota 分开记录，不输入模型剩余配额、不用作 sleep 测试的单独否决条件。sleep 不等于零启动成本，仍须取得测试启动与窗口授权。 |
| B：已记录的 Pod 逻辑请求 | 必须进入原生账本。不能把 Node.status.allocatable 当剩余值；也不能把历史终态 Pod 当活跃预留。 |
| C：多个 scheduler 的未发布预留 | 当前核心问题。不同 Namespace、schedulerName、Lease、NodeLock 或 RF 白名单不能解决；采用单权威灰度，或阻断竞争请求并确认无遗留请求的短时窗口。 |
| D：Device Plugin／runtime 启动 | 需要当前注解协议、健康插件、RuntimeClass、镜像与 preload 依赖、可用虚拟设备槽。最终只能通过另获授权后的首个 sleep Pod 核验，不以旧日志替代。 |

逻辑 Allocate 成功不表示立即物理预留 4096MiB，也不保证未来 CUDA 程序成功。本轮不运行 CUDA、不测实际性能、不做 OOM 实验。

## 本轮真实只读复查

证据：`results/round7-reassessment-20261019/raw/cluster-final.jsonl`、`raw/4090-runtime.jsonl`、`live-window-preflight.json`。每条命令有时间、退出码和输出校验值。北京时间约 2026-10-19 16:56 的 API 快照：

- Kubernetes v1.35.0；4 个注册节点，3 个 Ready。实验范围为 t3dgq/4090×4、t3dmg/5090×4、t3dgq-2/L40×3；r5300/A40排除。
- 活跃普通 GPU/vGPU/DRA Pod 请求为 0；目标节点没有有限时间戳、未消费设备注解。此结论来自 API Pod 请求／注解检查，不是直接读取 DQN 的全部私有预留。
- 原 Deployment `volcano-system/volcano-scheduler` 仍为1副本，仍挂载原定制二进制；ConfigMap resourceVersion仍为21788675，策略为dqn-job，未改动。
- vGPU Device Plugin三目标节点Ready；4090仍同时注册普通 `nvidia.com/gpu` 与vGPU接口。二者指向重叠的设备，准入窗口必须同时覆盖，不能把容量加总。
- `rf-v7-gpu-window` Policy/Binding及`volcano-rf-test` Deployment均不存在。本轮没有创建它们。
- 8个旧终态vgpu-test Pod的临时注解为空，`vgpu-time=18446744073709551615`。插件成功／失败路径会将时间戳改为该最大值；它们不等于当前Pending分配。本次不删这些Pod，不改历史checkpoint。
- 新采样4090的缓存镜像、sleep ELF及glibc文件只读检查通过。5090/L40的同一镜像缓存仍无确认；不因物理显存繁忙直接排除这两类设备。

**证据限制：** scheduler日志中 `NewGPUDevices` 的 `UsedMem/UsedCore/UsedNum=0` 是构造注册对象时的数值，之后还会重放Pod；不能把这条日志冒充“最终账本直接观测”。API注解重建也不包含尚未发布的scheduler私有预留。准入生效后必须再次检查所有已有请求并等待旧调度循环／在途调用结束；不能仅凭这次零请求快照立即启动RF。

## 两个方案比较

| 项目 | A：统一 scheduler 灰度 | B：独立 scheduler +短时准入窗口 |
|---|---|---|
| 原 DQN scheduler | 要升级二进制、重建原Pod；保留dqn-job作为业务策略 | 不改其二进制、配置或服务，不停止它 |
| 新逻辑预留 | RF实验Pod与DQN业务共用同一原生分配权威 | 窗口期间只有RF实验请求能成为新的GPU请求；旧请求必须先核实为空 |
| 普通GPU接口 | 仍需统一管理／限制重叠接口；单进程vGPU账本不会自动管理普通GPU插件 | 临时阻止新的普通GPU请求，避免别名入口重复预留 |
| 改动范围 | Go灰度分流、共享提交同步、旧Deployment新镜像/sidecar/配置 | 原rf-job无需灰度升级；新增准入Policy/Binding、独立RF控制面和测试Pod |
| 业务影响 | 原scheduler重启；混合提交同步、错误传播影响所有vGPU业务路径，不能称零影响 | 短时拒绝新增GPU/DRA Pod，控制器可能重试；已有任务不删除，CPU普通Pod不受此规则限制 |
| 回滚 | 先清理实验分配，再关闭灰度／恢复旧Deployment和配置；保留原文件 | 先释放自己的Pod、停止独立RF，再移除窗口；原DQN不需回滚 |
| 当前建议 | 长期方向；已做独立源码原型与离线回归，尚未上线 | 当前零活跃GPU请求条件下，作为一次性逻辑验证的最小改动候选；必须另外批准窗口影响 |

准入窗口是集群范围的：只限制实验Namespace会漏掉DQN/default scheduler的新GPU请求。DRA目前无活跃请求，为避免另一设备分配通道，草案也暂停新建DRA Pod，包括可能的非GPU DRA用途；这项范围必须向管理员说明。规则不阻止宿主机程序继续用卡，不防范管理员绕过API或移除策略；窗口期间禁止未协调的设备／分配注解变更。

Kubernetes v1.35支持通过CEL验证Policy加Deny Binding拒绝匹配请求；`failurePolicy: Fail` 使求值错误也不能静默放行。规则安装、类型检查及所有API Server传播仍须在获准后验证，不能用本地CEL测试代替。[官方v1.35文档](https://v1-35.docs.kubernetes.io/docs/reference/access-authn-authz/validating-admission-policy/)

## 实际 Go 源码准备（只在独立目录）

独立分支 `codex/rf-round7-canary` 基于前轮 `b289ad75aca8240dc2a3bdfbb27feadb3e647860`；线上目录 `/home/zbs/volcano-v1.13.1-dqn` 未修改。开发测试目录为 `/home/zbs/rf-round7-reassess-20261019`。精确补丁、提交与校验清单见本轮结果目录。

| 文件／函数 | 实现或复用的内容 |
|---|---|
| `.../vgpu/rf_job.go:RFActive, RFForPod` | 分开“统一账本保护启用”与“当前Pod是否走RF”。灰度默认关闭；实验Namespace+experiment标记+workload label存在共同匹配；空值／非法类型由现有解析拒绝，缺失Label走原策略。没有逐请求修改全局GPUSelectPolicy。 |
| `.../vgpu/device_info.go:FilterNode, ScoreNode, Allocate` | RF实验Pod走原rfPlan/allocateRF；非实验Pod保留配置的DQN设备排序与原节点评分。 |
| `.../vgpu/utils.go:checkNodeGPUSharingPredicateAndScore, checkNodeGPUSharingNative` | 拆出原生DQN/确定性路径；混合模式读取独立快照，RPC不占用账本写锁。 |
| `.../vgpu/rf_canary.go:allocateLegacyShared` | 非RF分配也在同一提交锁下检查快照token和重复预留，再调用原生AddPod；状态改变返回重试，不能把旧DQN计划直接提交。 |
| `.../vgpu/rf_job.go:commitNativePlan` | 两条策略共用原生注解写入、账本扣减、patch失败回滚；日志包含route、Pod UID、UUID、直接账本前后状态。 |
| `.../vgpu/device_info.go:SubResource` | 复用原生SubPod，增加实验Pod释放前后账本日志。 |
| `.../deviceshare/deviceshare.go` | 新增默认关闭RFCanaryEnable、Namespace、实验节点评分权重配置；原业务权重不变。RF内部评分权重未改。 |
| `.../predicates/predicates.go:AllocateFunc` | 复用第六轮错误传播和Task.Pod注解同步；混合模式中对非RF分配也生效，是影响范围之一。 |

灰度MVP只支持当前HAMI-core，不应对MIG节点启用；不增加虚拟化资源语义猜测。A不是简单切一个配置：旧二进制没有这些代码。`A-canary-config-DRAFT.yaml`保留旧actions、插件和DQN地址，`A-scheduler-patch-DRAFT.yaml`移除Pod内覆盖可执行文件的旧hostPath挂载，改用新镜像，并给Python sidecar隔离API token；原host文件本身不删除、不覆盖。A的完整Deployment回滚应使用变更前备份，不能假设rollout undo会回滚ConfigMap内容。

## Sleep 启动条件与设备消费

初始建议只在t3dgq四卡进行首个完整链路检查；UUID清单见本轮`logical-inventory.json`。每卡逻辑总显存24564MiB、100算力配额、10实例，当前注解重建已分配为0，因此4096/20/1请求在四卡均满足逻辑硬约束。开始前必须重查；不能把本表当将来可用性的保证。

固定sleep镜像：`docker.io/pytorch/pytorch@sha256:923f687790bec78081c357e71dcd5dcef80b0cc00f6c34484902a5e83362c854`。只执行 `/bin/sleep 600`，不导入PyTorch；使用整数MiB `"4096"`，不用`4Gi`冒充这个插件的整数协议。`runtimeClassName: nvidia`，requests与limits都写1/4096/20；imagePullPolicy=Never避免临时拉取。插件注入库／挂载／缓存文件以及runtime设备配置仍可能产生开销或启动错误，文件兼容证据不等于成功启动。

已复核保存的官方v1.11.0插件源码：`pkg/plugin/vgpu/util/util.go:GetPendingPod/getOldestPod`通过节点注解和时间戳选择待处理Pod，不按schedulerName筛选；`PodAllocationSuccess/Failed`把时间戳置为最大值。`GetNextDeviceRequest`读取临时分配注解；`plugin.go:NvidiaDevicePlugin.Allocate`设置实际`NVIDIA_VISIBLE_DEVICES`、限额及preload挂载。该协议允许另一个schedulerName，但其Pending选择和全节点短时锁也要求避免同时存在未协调分配。本结论来自对应版本官方源码及现有注解，不是新RF Pod的实测结果。

5090、L40不因nvidia-smi物理占用被排除。`expand-three-nodes/`准备了11卡范围的scheduler、RBAC、准入和评分Pod草案；须补齐两节点镜像缓存、用户批准及启动检查后才能用。原生`cache.go:updateNodeSelectors`以完整key:value保存、`event_handlers.go:nodeCanAddCache`按OR判断；三个重复`--node-selector`只包含这三台，不包含A40。

## 证据采集与验收

每个测试都要保留：Pod UID/Label/整数请求 → RF请求中的每卡逻辑快照与模型版本 → 原始分类、训练/推理判断、pending、RF/H/总分 → 原生`rf-job UUID allocation`日志与永久UUID注解 → 对应UID/容器的插件日志／kubelet AllocateResponse或CRI环境。

- `round7_capture.py`保存Pod与RF/Go日志；`round7_collect.py`读节点checkpoint，解码`vgpu-number`响应里的`NVIDIA_VISIBLE_DEVICES`。不能用memory/cores虚拟DeviceID推断实际物理UUID。
- 临时`devices-to-allocate`可能被插件清空；永久`volcano.sh/vgpu-ids-new`、`volcano.sh/vgpu-node`和成功提交日志分别保留。缺失消费证据就记未确认，不能由Running补造。
- 直接RF请求、提交／释放日志和原生metrics属于scheduler账本观测；API活跃Pod注解重建单列来源。构造日志、Node.allocatable和Python推荐不能替代最终分配。
- 分配增量应为选中UUID的4096MiB/20/1，其余设备不变；释放后回到可核对基线。第二个Pod快照必须包含第一个Pod的已分配量。类别／UUID未改变时如实记录。
- 统计合法分配率、三方UUID一致率（仅证据完整样本）、配额正确率、显存/算力/实例超分配次数、释放正确率、动态分类触发、RF on/off差异和两种回退（服务失败、pending保守排序）。未执行项为null。
- 逻辑预留率分别为已分配MiB/注册MiB、已分配cores/(100×卡数)。参考请求碎片率沿用`gpu_lab/staging.py:fragmentation`，不能解释为物理利用率或性能收益。

冻结模型只拟合合成理论标签；本轮不训练、不修改数据或评分权重。离线测试与版本证据见`verification-summary.json`，真实Pod实验仍未执行。准确命令、顺序、影响与回滚见`round7-logical-runbook.md`。

## 本轮实际执行的离线验证

2026-10-19在4090宿主机的独立Linux进程实际执行，并非Windows交叉编译：

| 项目 | 结果 | 证据 |
|---|---|---|
| Python回归（第五/六轮、消融、第七轮及本轮准备） | 80 passed；0失败/错误/跳过 | `linux-final/pytest-preparation-final.xml` |
| Go原生三个包：vgpu、deviceshare、predicates | 全通过；282个通过事件（含父/子测试），0失败/跳过 | `linux-final/go-tests.log`、`test-summary.json` |
| 初始四卡准入CEL规则 | 20个正反例通过，已包含于上述Go测试 | 同一Go日志 |
| 三节点扩展CEL规则 | 22个正反例通过，单独运行 | `policy-expanded-cel-tests.jsonl` |
| scheduler编译 | go build退出0；只写独立目录，不运行scheduler | `linux-final/execution.json` |
| 模型/评分合同 | 测试前后SHA256完全一致，未训练 | 同一execution.json |

上述路径均相对`results/round7-reassessment-20261019/`。最终Go提交为`1a5caec7cb44b7a939f6413057596af3cd1ec19a`，独立编译二进制SHA256为`501e9ece8f838fc2abcc82f5ea731e399178705f7a0bda0d6c02c8eac6b0d7e9`。线上二进制不变。CEL测试基于实际Go CEL求值器和JSON夹具，不是已安装Policy的API Server验证；YAML还没有server dry-run。

真实Python/joblib服务在灰度用例中收到3次请求，原生Go fake-client注解与推荐一致：

| 离线步骤 | SIM-...-00已用MiB/cores | SIM-...-01已用MiB/cores | 推荐及原生Allocate UUID |
|---|---:|---:|---|
| live-a（training）前 | 0/0 | 0/0 | SIM-live-canary-GPU-00 |
| live-b（inference）前 | 4096/20 | 0/0 | SIM-live-canary-GPU-01 |
| 两Pod释放后重新预测 | 0/0 | 0/0 | SIM-live-canary-GPU-00（此步只预测；前两步实际fake-client Allocate） |

原始数据见`linux-final/canary-live-rf-advice.json`与`canary-native-trace.json`。这些UUID是测试夹具，不能当真实GPU实验。此序列中类别仍为balanced；排名与UUID变化同时涉及当前资源/启发式，不能由此单独归因于RF学习项。RF独立贡献仍引用已保留的180组配对消融结果；本轮真实on/off实验尚未执行。

另外用本地gRPC/HTTP桩确认非实验Pod继续调用dqn-job排序、实验Pod分流至RF路径；真实RF服务则用于上表的独立用例。混合策略竞争单卡时不超分配，重复事件不重复记账、释放恢复、非法/空类型标签不能绕过解析。线上DQN服务没有被调用、训练、重启或修改。
