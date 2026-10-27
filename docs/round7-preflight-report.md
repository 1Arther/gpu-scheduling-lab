# 第七轮阶段一：真实集群只读检查与部署准备报告

**结论：NO-GO。当前没有已证明可安全独占的测试 GPU；不得进入真实部署或提交 Pod。阶段一没有创建、修改或删除集群资源。**

集群快照开始时间（北京时间）：2026-10-19T15:42:53.840864+08:00。每条物理采样另有 UTC 时间，阶段二前必须重新采样。
Kubernetes：v1.35.0；4 个注册节点，3 个 Ready。r5300/A40 仍不可用，排除；研究范围为 4090×4、5090×4、L40×3。

## 当前 GPU 清单

逻辑“已分配/剩余”由 API 活跃 Pod 的永久 UUID 注解重建，不是直接读在线 DQN scheduler 内存。终态 Pod 排除；删除中的非终态 Pod继续计入。nvidia-smi 的物理数值单列，未输入 RF。

| 节点/索引 | GPU UUID | 注册 MiB | 重建已分配 MiB/算力/实例 | 物理已用/空闲 MiB | 实际利用率 | GPU 进程数 |
|---|---|---:|---|---|---:|---:|
| t3dgq/0 | GPU-7b94f673-ce00-719b-2c94-706baa032dae | 24564 | 0/0/0 | 6695/17516 | 100% | 1 |
| t3dgq/1 | GPU-fb6aaeaf-9ca4-3c9a-7b3b-f56714c1f7cd | 24564 | 0/0/0 | 6695/17516 | 100% | 1 |
| t3dgq/2 | GPU-28cf7186-0bdd-be9f-92e5-3c59b4e92c1e | 24564 | 0/0/0 | 6695/17516 | 100% | 1 |
| t3dgq/3 | GPU-27be0024-0498-4114-e6b9-b2d0db148309 | 24564 | 0/0/0 | 6695/17516 | 100% | 1 |
| t3dgq-2/0 | GPU-d8482de5-c31d-9b24-f28b-217dc071a5d9 | 46068 | 0/0/0 | 44049/1325 | 0% | 3 |
| t3dgq-2/1 | GPU-69fd2a04-950c-e3fd-3c78-2c2525113383 | 46068 | 0/0/0 | 42369/3004 | 0% | 6 |
| t3dgq-2/2 | GPU-ea84e352-a3f2-bdfe-03a9-f33051f2609e | 46068 | 0/0/0 | 36601/8773 | 0% | 1 |
| t3dmg/0 | GPU-fffbc9bd-6d21-4287-ff97-7a02b17e9ad6 | 32607 | 0/0/0 | 31044/1107 | 0% | 1 |
| t3dmg/1 | GPU-412f170c-a414-091e-1203-28751d20a1f3 | 32607 | 0/0/0 | 31044/1107 | 0% | 1 |
| t3dmg/2 | GPU-d078c4f4-f0ac-6469-7960-0beee158ca7a | 32607 | 0/0/0 | 31182/969 | 0% | 1 |
| t3dmg/3 | GPU-9ba09ba0-9b12-8014-4c2e-fd48889ccc9f | 32607 | 0/0/0 | 31182/969 | 100% | 1 |

NVML 还有驱动保留容量，total 不必等于 used+free；表中 free 是直接查询值。逻辑资源看似空闲，并不能证明测试设备无人使用。

4090 四卡有 root 的 hashcat.bin 用户会话进程；5090 四卡有 sxzhou 的 VLLM worker 用户会话进程；L40 有 infonet/rlj 的推理进程、MPS 及 root Docker 进程。这里只读记录 PID/用户名/cgroup，未停止或调整任何进程。
按当前 API 注解重建，目标设备活跃 vGPU 分配为 0；重建警告 0 条。这不能排除调度器的未绑定预留或非 Kubernetes GPU 访问。

## 调度器、插件与独立构建

- Deployment `volcano-system/dqn-scheduler-grpc`：镜像 `pytorch/pytorch:2.2.2-cuda12.1-cudnn8-runtime`；状态 `{"availableReplicas": 1, "conditions": [{"lastTransitionTime": "2026-06-04T11:38:30Z", "lastUpdateTime": "2026-06-04T11:38:52Z", "message": "ReplicaSet \"dqn-scheduler-grpc-768b998b6c\" has successfully progressed.", "reason": "NewReplicaSetAvailable", "status": "True", "type": "Progressing"}, {"lastTransitionTime": "2026-08-04T12:05:50Z", "lastUpdateTime": "2026-08-04T12:05:50Z", "message": "Deployment has minimum availability.", "reason": "MinimumReplicasAvailable", "status": "True", "type": "Available"}], "observedGeneration": 1, "readyReplicas": 1, "replicas": 1, "terminatingReplicas": 0, "updatedReplicas": 1}`。
- Deployment `volcano-system/volcano-scheduler`：镜像 `volcanosh/vc-scheduler:v1.13.1`；状态 `{"availableReplicas": 1, "conditions": [{"lastTransitionTime": "2025-12-24T07:39:46Z", "lastUpdateTime": "2026-06-05T07:20:39Z", "message": "ReplicaSet \"volcano-scheduler-668889fb4f\" has successfully progressed.", "reason": "NewReplicaSetAvailable", "status": "True", "type": "Progressing"}, {"lastTransitionTime": "2026-08-04T09:35:38Z", "lastUpdateTime": "2026-08-04T09:35:38Z", "message": "Deployment has minimum availability.", "reason": "MinimumReplicasAvailable", "status": "True", "type": "Available"}], "observedGeneration": 147, "readyReplicas": 1, "replicas": 1, "terminatingReplicas": 0, "updatedReplicas": 1}`。
- DaemonSet `kube-system/volcano-device-plugin`：镜像 `docker.io/projecthami/volcano-vgpu-device-plugin:v1.11.0, docker.io/projecthami/volcano-vgpu-device-plugin:v1.11.0`；状态 `{"currentNumberScheduled": 3, "desiredNumberScheduled": 3, "numberAvailable": 3, "numberMisscheduled": 1, "numberReady": 3, "observedGeneration": 8, "updatedNumberScheduled": 3}`。
- DaemonSet `nvidia-device-plugin/nvidia-device-plugin-daemonset`：镜像 `nvcr.io/nvidia/k8s-device-plugin:v0.17.0`；状态 `{"currentNumberScheduled": 2, "desiredNumberScheduled": 2, "numberAvailable": 1, "numberMisscheduled": 0, "numberReady": 1, "numberUnavailable": 1, "observedGeneration": 4, "updatedNumberScheduled": 2}`。

线上 scheduler Pod 虽标记镜像 v1.13.1，实际挂载 `/home/zbs/volcano-v1.13.1-dqn/_output/bin/vc-scheduler`。配置仍为 `deviceshare.GPUSelectPolicy: dqn-job`；部署参数未设置 `--node-selector`，不能把 Deployment 的宿主机 nodeSelector=t3dgq 误解成它只调度 t3dgq。
4090 同时注册普通 nvidia.com/gpu 与 volcano.sh/vgpu-*，kubelet 检查点也列出了同一批 GPU UUID。两套资源名代表重叠设备，不能加总容量或分别预留。
补充只读探测：在线scheduler的8080/metrics连接被拒绝，没有获取到其内部逻辑账本直接指标；因此本报告不把API注解重建值冒充直接观测。原DQN ConfigMap resourceVersion前后仍为21788675；两实验Namespace查询均为NotFound。原二进制只读SHA256为9f2e1ae3359ce234fd048bc48aab082add99a686631808908b5ae518bdebf4f4，未覆盖或执行它。
检查点共有 15 条资源记录，其中 15 条无法对应本次 API Pod 列表。它们可能是历史/待清理记录，不能当成当前 RF 实验，也不手工修改 checkpoint。L40 检查点当前无 PodDeviceEntries。详见 kubelet-checkpoint-correlation.json。
已有独立第六轮模型/二进制与 Linux 成功记录已只读核验。上轮测试 HTTP 服务已关闭；本轮只在独立 CPU 单元测试期间启动 loopback 模型服务，无在线 RF Deployment。新 Go 参数 `deviceshare.RFPredictionEnabled` 默认 true，便于只切换学习项，模型和评分权重未变。
RFTimeoutMS=500；服务异常/版本不符/非法 UUID 由 Go 确定性选择回退并重新校验资源；模型 pending 时由 Python 保守评分回退。这两种回退必须分开统计。
冻结模型 `rf-v4-known-states-ee35cfabe30f` 对当前逻辑快照的离线预测已存 read-only-snapshot-classification.json（不是线上 RF 调度）。初始标签分布 `{'balanced': 11}`，不代表这些繁忙设备可供实验。

## 隔离判断

不同 Namespace、schedulerName、leader-election Lease 只隔离对象职责，不能隔离物理 GPU 或跨调度器未绑定预留。Volcano 会观察已绑定的其他 scheduler Pod，但看不到对方尚未发布/绑定的本地预留。NodeLock 是节点注解和短时分配协调，不能作为持久设备独占租约。
本地源码当前仅有 GPU 型号 include/exclude 注解，未发现可直接用于双方统一 UUID 分区的规则。不能伪造 nvidia.com/use-gpuuuid 为有效隔离；只在 RF 侧加 UUID 白名单也不会阻止原生 GPU Plugin、DQN 或宿主机进程访问它。
可证明的首选方案：独占整台 GPU 测试宿主机，且不再受原 DQN/default scheduler、普通 GPU 接口或其他宿主机用户管理；使用独立测试集群或由管理员提供已审计的排他分区。当前没有这样的设备，本轮不把任何现有节点切出生产、不改 DQN 选节点规则，也不自行加 taint/cordon。
同集群采用专用节点标签/taint时，它们只是额外约束。还必须有可验证的“其他入口不能预留这些设备”的保护及宿主机使用授权；单独 NoSchedule taint 也不能阻止带通配 toleration、nodeName 直绑或宿主机 GPU 进程。
至少需要多张独占 GPU 才能验证设备选择差异；单卡可检验绑定/释放链路，但无法证明 RF 改选设备。单一同型号的空闲节点中分类相同，UUID 不变也应如实记录。

## 文件、执行与回滚

`k8s/round7/` 包含 Namespace、最小 RBAC、独立 Queue/Quota、四套 ConfigMap、scheduler+RF sidecar Deployment 草案、9 个单 Pod/PodGroup sleep 用例和两份 Dockerfile。Deployment 默认 replicas=0；镜像含 build-required 占位，测试节点为 rf-exclusive-node-required；不得直接 apply。
独立 schedulerName=volcano-rf-test，独立 Lease gpu-rf-system/volcano-rf-test；RF sidecar只监听127.0.0.1:18081，scheduler健康/指标分别18091/18090。RF sidecar没有挂载SA token；PodGroup明确创建，避免修改现有控制器的schedulerName列表。
RBAC仅允许在两实验 Namespace 修改 Pod/PodGroup/事件/绑定；节点写权限限未来指定节点；不允许删除外部 Pod，不启用 preempt/reclaim。cache 启动仍会尝试 Create root 与自定义默认 Queue，因此需要 queues.create 权限；现存 root 返回 AlreadyExists，不会覆盖 root。必须提前核查 root 存在，不能宣称该启动过程完全不触及共享 API 对象。
sleep 镜像固定为 `docker.io/pytorch/pytorch@sha256:923f687790bec78081c357e71dcd5dcef80b0cc00f6c34484902a5e83362c854`，4090缓存及 `/bin/sleep`/glibc ELF 文件已读验证=True；5090/L40未确认有该镜像缓存。只执行/bin/sleep，不导入PyTorch、不运行CUDA；仍不能以Running证明CUDA兼容或成功。
详细顺序、潜在写命令及其目标/回滚见 docs/round7-execution-runbook.md。采集：round7_collect.py、round7_capture.py；UUID一致性：gpu_lab/staging.py；回滚：round7_rollback.py 默认只打印，强制核对真实创建UID与所有权，永不删除Node/Namespace/CRD/root队列，也不清理hamivgpu锁。

## 证据与验收状态

本轮真实 Pod 实验尚未执行。分配成功率、UUID一致率、超分配次数、释放正确率、RF on/off真实UUID变化等均为 null/未测，见 phase2-metrics-NOT-EXECUTED.json。已有180组消融和当前CPU回归均属于离线fake-client证据。
模型监督仍为合成理论标签。资源预留率为逻辑已分配/逻辑注册总量；碎片率定义为1−逐卡可容纳参考请求数/聚合资源可容纳请求的上界（分母0为null）；另记录不能放下4096MiB/20配额/1实例请求的卡上残余显存和算力。它们不表示实际GPU利用率或真实性能匹配率。
当前阻碍：11张GPU均有使用者、DQN没有排他节点边界、4090双接口重叠、检查点历史记录尚未解释完毕、实验镜像尚未构建和固定digest。只有管理员完成安全隔离及镜像/权限预检，并且用户另行明确批准阶段二后，才可以开始真实验证。
源码依据：本地 device_info.go 的FilterNode/ScoreNode/Allocate；rf_job.go 的rfPlanFor/rfValidate/allocateRF；hamicore.go账本；cache.go的addEventHandler/newDefaultAndRootQueue；event_handlers.go的nodeCanAddCache/addTask；nodelock.go的LockNode/ReleaseNodeLock；Device Plugin v1.11.0 的Allocate/GetNextDeviceRequest/EraseNextDeviceTypeFromAnnotation。
kubelet检查点解码以[官方Kubernetes v1.35.0 Device Plugin API](https://raw.githubusercontent.com/kubernetes/kubernetes/v1.35.0/staging/src/k8s.io/kubelet/pkg/apis/deviceplugin/v1beta1/api.proto)的ContainerAllocateResponse.envs字段为依据；必须用Pod UID关联，不能把虚拟DeviceID直接当成物理UUID。

## 最终验证与交付索引

本轮 Linux CPU 工作目录为 `/home/zbs/rf-round7-preflight-20261019`，与线上 `/home/zbs/volcano-v1.13.1-dqn` 分开。原有第六轮源码、模型、结果均保留。Go 工作分支为 `codex/rf-round7-preflight`，提交 `b289ad75aca8240dc2a3bdfbb27feadb3e647860`，父提交为第六轮消融版本 `9dcbee7d51747aa6b0cab113f177dd52d938e3f1`。本轮 Go 变更仅是将已有 RF 预测项开关接入配置，以及对应配置测试，没有改模型或评分权重。

| 实际验证项 | 结果 | 证据（项目内路径） |
|---|---|---|
| 4090 上真实 Linux 执行 Python 回归 | 71 passed，0 failed/errors/skipped | `results/round7-preflight-20261019/linux-final-safety/pytest-safety-final.xml` |
| Linux Go 三个原生包测试 | 三包通过，256 个测试通过事件（含父测试及子测试），0 失败 | `results/round7-preflight-20261019/linux-cpu/go-tests.log` |
| 独立 scheduler 编译 | go build 退出码 0，未启动该 scheduler 连接集群 | `results/round7-preflight-20261019/linux-cpu/execution.json` |
| Go–Python 与原生 Allocate 回归 | loopback RF 服务、180 组消融、连续账本与原生 fake-client 用例通过；不是实际 API Server 分配 | 同目录 `native-ablation.json`、`sequential-ledger.json`、`native-round6-regression/` |
| 冻结模型及评分合同 | 构建前后所有模型文件 SHA256 一致，未训练 | 同目录 `execution.json` |
| 回滚保护 | UID 条件、目标路径、实验归属等离线测试通过；未执行 DELETE | `linux-final-safety/final-safety-verification.json` 与 `tests/test_round7.py` |
| YAML 与镜像 | YAML 已由本地解析/约束测试验证；未进行 API Server 校验，镜像未构建、推送或部署 | `k8s/round7/` |
| 真实 Pod 调度与三方 UUID 一致性 | 未执行，指标为 null | `results/round7-preflight-20261019/phase2-metrics-NOT-EXECUTED.json` |

独立编译产物 SHA256：`3dc62e766a3b2bb6ac258f332afd8c6d7a3d21a00b3fb504630008e2f0bf3dd1`，仅保存在独立目录 `results/phase1-cpu/vc-scheduler`；没有替换线上二进制。

文件入口：`k8s/round7/README.md` 为部署草案说明；`docs/round7-execution-runbook.md` 包含 A～G 用例顺序、所有拟议写操作的目标与影响、停止条件、回滚步骤；`gpu-inventory.csv` 与 `logical-physical-inventory.json` 为当前资源证据；`raw/` 与 `raw-manifest.json` 保留命令输出、时间、失败/不完整记录及校验值。`verification-summary.json` 汇总最终验证与源码校验。API 注解重建、物理采样、离线 RF 推断各自标明来源。

可在当前独立目录重复执行 Python 测试（不连接集群）：

```bash
cd /home/zbs/rf-round7-preflight-20261019
KUBECONFIG=/home/zbs/rf-round7-preflight-20261019/nonexistent-offline-kubeconfig /home/zbs/rf-ablation-20261019-1425/lab/.venv/bin/python -m pytest tests/test_round7.py tests/test_round5.py tests/test_round6.py tests/test_rf_ablation.py -q
```

完整 Linux Go–Python/编译流程为 `scripts/round7_linux_check.py`；它只启动 loopback 模型服务、运行 fake-client 测试和编译，不启动真实 scheduler。该脚本拒绝覆盖已有 `results/phase1-cpu`，若要重跑应准备新的独立源码副本且该输出目录不存在，保留本次证据。构建命令另见执行手册；不应把运行 scheduler 当作编译验证。

**当前阶段二仍为 NO-GO：先协调独占设备并证明所有 GPU 分配入口排他、解释检查点遗留记录、完成镜像与最终配置预检，然后再取得用户明确授权。当前没有实验集群对象需要回滚。**
