# 第七轮执行手册：当前仅完成阶段一，禁止部署

当前状态为 **NO-GO**。11张卡都有使用者，原DQN scheduler没有独占节点边界，4090的普通GPU与vGPU接口重叠。以下写命令是供后续审查的操作清单，本轮未执行。不能仅因用户批准部署就跳过设备隔离条件。

## 1. 隔离方案与进入条件

首选：独占整台GPU宿主机，并使它不再被原集群的DQN/default scheduler、普通GPU分配入口及其他宿主机用户使用，放入独立测试集群或已有可靠排他分区。当前没有这样的设备。不得把正在运行的节点移出生产，不得结束其他人的任务。

若将来仍在同一集群使用专用节点，管理员必须提供可审核证据证明旧调度器不会对这些UUID做预留，普通GPU入口也不能分配它们，宿主机使用者已释放并同意独占。标签 `gpu-lab/partition=rf-exclusive-v7` 和专用NoSchedule taint只能作为额外限制，不能单独证明隔离。不能只在RF客户端屏蔽UUID，却让DQN继续看到整卡。

阶段二前应再次检查：

- 目标节点Ready，注册UUID、逻辑总量、健康及HAMI模式一致；普通GPU/vGPU别名已被统一保护。
- 没有外部GPU进程、普通GPU Pod或未决/终止中的vGPU分配；检查点和API中无法对应的设备记录已解释，不能手工编辑checkpoint。
- 全部目标UUID已形成书面独占清单。多卡选择实验至少需要两张可比较的独占卡；单卡只验证设备绑定、逻辑分配和释放。
- RF源码、二进制、模型、特征顺序、评分策略SHA一致；两份实验镜像已在独立构建机生成并固定digest。
- 目标节点有已验证的sleep镜像；所有前置检查重新采样，不能沿用本报告快照证明将来仍空闲。
- root Queue存在，实验名称未被其他人使用，Namespace/RBAC归属可核实，镜像与API权限预检通过。
- 用户另外明确批准阶段二的具体对象、镜像、目标节点/UUID、变更与回滚。

`k8s/round7/isolation-gate.json` 当前为false；不得自动改成true来绕过检查。原始YAML是审查草案：replicas=0、镜像包含build-required、目标节点为rf-exclusive-node-required，**不可直接部署**。

## 2. 已实现的配置与源码

- 独立schedulerName：`volcano-rf-test`。
- 系统/测试Namespace：`gpu-rf-system` / `gpu-rf-tests`；独立Queue `rf-v7-queue`。
- Lease：`gpu-rf-system/volcano-rf-test`，显式指定leader election名称、命名空间和leases类型，不复用volcano-system/volcano。
- scheduler健康端口18091、指标18090；Python服务18081仅监听同Pod的127.0.0.1。采用常驻sidecar，不创建第二套资源账本。
- Python加载冻结模型一次，失败时在listen前退出；readiness exec检查model_ready和精确模型版本。RF容器不挂载API token。
- HTTP超时500ms；Go检查schema、request ID、快照token、模型版本、开关回显、UUID、资源和分值；异常走Go确定性回退。锁内不等待Python。
- `deviceshare.RFPredictionEnabled` 新增配置入口，默认true，仅控制已有学习项是否加入总分，权重不变。RFEnable仍默认关闭。
- `audit_server.py` 输出rf_advice JSON：Pod UID、逻辑快照、原始RF/适配判断、pending原因、R/H/总分、推荐UUID和request ID。
- 最终仍由原生Allocate更新账本、写`vgpu-ids-new`/`devices-to-allocate`/`vgpu-node`，由Device Plugin消费。
- 不启用preempt/reclaim、proportion/overcommit，不授予删除外部Pod的权限。节点写权限只能填入被隔离节点名。

注意：stock cache启动会尝试Create root和默认Queue。root已存在时应返回AlreadyExists；不是“全程只操作新Namespace”。RBAC的queues.create不能用resourceNames限定，必须审查这一权限，且阶段二前确认root已存在。不要在生产集群直接运行vc-scheduler进行所谓健康检查：即使没有实验Pod，也会初始化缓存并尝试API写入。本轮仅运行--help、读取build info、fake-client单元测试和编译。

## 3. 只读检查与文件验证命令（可在管理节点执行）

```bash
cd /home/zbs/rf-round7-preflight-20261019
python3 scripts/round7_collect.py --cluster --node --sudo-kubectl --kubeconfig /etc/kubernetes/admin.conf > cluster-readonly.jsonl
python3 scripts/round7_collect.py --supplement --management-runtime --sudo-kubectl --kubeconfig /etc/kubernetes/admin.conf > runtime-readonly.jsonl
```

各GPU宿主机执行 `python3 scripts/round7_collect.py --node` 和 `--supplement`。没有crictl时仅尝试已有ctr镜像列表，不安装工具。

`round7_report.py` 在项目本地读取收集文件并生成JSON/CSV；不会连接Kubernetes。模型对只读逻辑快照的分类也属于离线推断，不能冒充线上scheduler调用。

## 4. 构建准备（未构建镜像、未推送、未部署）

在独立CPU构建环境中，使用本轮已经测试的源码构建：

```bash
mkdir -p build/round7
cd scheduler/volcano-rf
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o ../../build/round7/vc-scheduler ./cmd/scheduler
cd ../..
docker build -f k8s/round7/Dockerfile.scheduler -t gpu-lab/volcano-rf:round7 .
docker build -f k8s/round7/Dockerfile.rf -t gpu-lab/rf-model:round7 .
```

构建/推送的registry、digest和目标节点缓存验证应另行记录；本轮没有执行这些Docker命令。之后将已审查的digest填入Deployment；不要使用stock v1.13.1镜像冒充含rf-job的新二进制。

更新草案时只填目标节点、镜像digest、RBAC的节点resourceNames，确认节点标签/隔离已由管理员落实；保留其他策略、模型和评分权重。先展示最终YAML diff给用户审查，再获得阶段二授权。不要静默修改原DQN Deployment/ConfigMap/二进制。

## 5. 潜在写操作清单：当前均禁止执行

以下命令只有在隔离完成、占位值已替换、最终YAML获批之后才有效；文件名保留DRAFT不代表已获授权。

| 命令 | 目标/影响 | 回滚范围 |
|---|---|---|
| `kubectl apply -f k8s/round7/00-namespaces.yaml` | 新建两实验Namespace；预先拒绝接管已有外部Namespace | 默认不自动删Namespace |
| `kubectl apply -f k8s/round7/01-rbac.yaml` | 新SA、ClusterRole/Binding、两实验Namespace Role/Binding | 仅记录UID对应的新对象 |
| `kubectl apply -f k8s/round7/02-queue-quota.yaml` | 新rf-v7-queue与测试ResourceQuota | 先清理自己的Pod/PodGroup，再删新Queue/Quota |
| `kubectl apply -f k8s/round7/03-configmaps.yaml` | 新实验配置和device-config；不改volcano-system配置 | 删除本轮ConfigMap |
| `kubectl apply -f k8s/round7/04-scheduler-DRAFT.yaml` | 审查版独立scheduler/RF sidecar Deployment；启动后会写自身Lease和相关实验对象 | 清理测试Pod并确认释放后停止/删除本Deployment |
| `kubectl apply -f k8s/round7/test-a-training.yaml`（其他用例同理） | 仅新实验PodGroup/Pod；4096MiB、20配额、1实例 | 精确删除该Pod，验证释放后再删PodGroup |
| `kubectl -n gpu-rf-system patch deployment volcano-rf-test --type=strategic -p '{"spec":{"template":{"spec":{"volumes":[{"name":"config","configMap":{"name":"rf-v7-config-no-prediction"}}]}}}}'` | 仅本轮Deployment按卷名切换学习项消融配置，引起其重建 | 同命令将configMap.name改为rf-v7-config-on；自己Pod均释放后再切换 |
| `kubectl -n gpu-rf-system patch deployment volcano-rf-test --type=strategic -p '{"spec":{"template":{"spec":{"volumes":[{"name":"config","configMap":{"name":"rf-v7-config-native-binpack"}}]}}}}'` | 原生Binpack对照；不是仅关闭RF预测项 | 切回rf-v7-config-on |
| `kubectl -n gpu-rf-system patch deployment volcano-rf-test --type=strategic -p '{"spec":{"template":{"spec":{"volumes":[{"name":"config","configMap":{"name":"rf-v7-config-service-unavailable"}}]}}}}'` | 将本轮RF endpoint指向127.0.0.1:1，验证确定性回退；不停止DQN或其他服务 | 切回rf-v7-config-on |
| `kubectl -n gpu-rf-tests delete pod rf-v7-c-first --wait=true --timeout=60s` | 仅结束自己的A，触发实际设备与账本释放；不要force删除 | 不需要重新创建A；记录释放快照 |

镜像、节点标签/taint/cordon、准入策略的具体变更**目前没有可批准的目标**，本轮不生成自动执行命令。不得擅自通过改原DQN配置来解决隔离。不同ConfigMap切换时Deployment使用Recreate，禁止同时运行RF on/off两个scheduler抢同一资源。Lease只解决本实验进程的leader，不解决与DQN的资源冲突。

## 6. A～G执行顺序（阶段二授权后）

1. 再做只读预检，保存注册容量、目标UUID、资源账本来源、普通GPU别名、进程/用户及检查点；任一隔离条件失败则不部署。
2. 创建实验控制面对象，等待独立scheduler和RF sidecar均Ready、模型版本准确，另核对新Lease holder和scheduler缓存同步/目标节点日志，才创建任何测试Pod。仅HTTP Ready不能证明已获得leader或完成缓存同步。为每个创建对象记录kind/name/namespace/UID/实验标签。
3. 启动采集：`python scripts/round7_capture.py --output results/round7-real-run-01 --seconds 120`。它只GET与读取日志；Node检查点另采。
4. A单训练：提交test-a-training，保存RF请求ID与推荐UUID、原生分配日志、API绑定及插件消费证据；删除自己的Pod、确认释放并删PodGroup。
5. B单推理：同样4096/20/1，不能用不同请求制造差异；同样清理恢复。
6. C连续：提交test-c-first（A），记录Allocate后每卡配额；保持A存活，再提交test-c-second（B），检查其RF请求快照已经包含A的分配。
7. D释放：删除rf-v7-c-first，等待原生账本和kubelet状态一致后提交test-d-after-release，观察分类/排名是否恢复。最后删除自己的B/C并确认回到基线。
8. E完整策略与纯学习项分开比较：依次on / no-prediction / native-binpack，用同一请求、相同独占设备初始配额、相同启发式参数。每次先清理并确认基线恢复，再切换自己的配置并等待Ready。不能把重启后重建账本当成释放成功的证据。
9. F只切换本轮endpoint为不可达端口，再提交test-f-fallback；要求Go日志标明回退、UUID仍合法、账本无超分配。恢复on，完全清理。
10. G用test-g-score-training/inference记录逐卡原始分类、R/H/总分及最终UUID；只有所有参与比较的候选都满足硬约束时，才讨论学习项贡献。输入差异、资源过滤、回退、节点总分和破平规则分别记录。若类别/UUID不变，如实报告，不调权重或模型去制造变化。

不要一次apply全部test-*.yaml。测试ResourceQuota限制最多4Pod、4实例、16384MiB/80配额；每个用例默认600秒sleep、900秒总deadline。多设备/多容器不是本轮首要验收，9个用例均单容器单vGPU。

## 7. 三阶段UUID与逻辑配额证据

1. Python sidecar的rf_advice：Pod UID、request_id、snapshot_token、model_version、RF开关、当前候选逻辑快照、两类适配性/原始输出、pending、各分项、推荐UUID。日志不是第二套资源账本。
2. 原生Go日志 `rf-job UUID allocation`：requestID与最终devices字段；API中永久注解 `volcano.sh/vgpu-ids-new` 和 `volcano.sh/vgpu-node`。**Device Plugin会擦除已消费的devices-to-allocate片段；消费后空串正常，不能拿这个空串判断UUID不匹配。** 因此尽量保存前后两阶段注解；轮询未捕获消费前瞬间时记录缺失，不能补造该字段，可用永久注解与成功写入后的Go日志交叉核对。
3. 读取分配节点kubelet checkpoint中匹配PodUID、容器名、vgpu-number的AllocateResponse.envs.NVIDIA_VISIBLE_DEVICES，或实际CRI runtime env的同名字段。虚拟vgpu-memory/cores DeviceID可能分散到其他卡，不代表实际可见UUID，不能用fake ID推断最终设备。

调用 `uuid_consistency()` 对有直接消费证据的样本比较三个列表；缺少证据返回insufficient_evidence，不能记成功。Pod Running、bind-phase=success、历史检查点或Python推荐本身均不足以证明三方一致。检查点解码器按官方Kubernetes v1.35 Device Plugin protobuf结构解析；缺失/不支持格式明确返回未知。

直接原生账本观测：RF请求的rfGPUs快照、独立scheduler `/metrics` 中vgpu_device_allocated_memory/cores/shared_number。API注解重建值单列来源；Node.status.allocatable是总容量，不能当成当前空闲。两个scheduler的指标可能同时统计同一已绑定Pod，禁止直接相加。

## 8. 指标与停止条件

真实用例未执行时成功率/错误数为null，不是0。每项保留有效样本数和缺失证据数：合法分配数/尝试数、三方UUID一致数/证据完整数、账本正确增量/可核对分配数、释放恢复数/可核对释放数；显存/算力/实例超分配分别计数。服务故障回退与模型pending回退分开。

逻辑显存预留率=已分配MiB/注册MiB；算力预留率=已分配配额/(100×卡数)。参考请求为4096MiB/20/1，每卡可放数量取显存/算力/实例槽的最小整数值；碎片率=1−逐卡数量之和/聚合资源上界（上界0时null），另报无法再容纳参考Pod的卡上残余显存和算力。它们不代表真实GPU利用率、实际吞吐或适配准确率。

立即停止后续提交的条件：

- 外部GPU进程/业务进入实验分区，或普通GPU和vGPU出现重叠预留；隔离证据过期或有其他scheduler参与。
- 任一层UUID不一致、未知UUID、来源缺失、NodeLock长期未释放、插件Allocate失败。
- 账本负值/超量/重复记账、Pod删除后账本或kubelet设备记录不能恢复、存在无法解释的并发预留。
- 模型版本/特征/评分策略校验不符；预期模型验证场景连续发生fallback，导致无法评价RF机制。
- 实验影响现有业务，出现未预期的外部Pod/Queue变更，或出现CUDA初始化/显存错误（不进行OOM复现）。

只停止提交自己的新Pod并收集证据；不杀他人进程，不改驱动、Device Plugin或containerd，不force删除，不盲目释放NodeLock。

## 9. 回滚

`scripts/round7_rollback.py` 默认只打印，要求真实UID ledger：

```bash
python scripts/round7_rollback.py --ledger results/round7-real-run-01/created-test-pods.json
# 只有明确获准回滚后：
GPU_LAB_ROUND7_ROLLBACK_APPROVED=yes python scripts/round7_rollback.py --ledger results/round7-real-run-01/created-test-pods.json --execute
```

先仅删除自己的GPU测试Pod，保持RF scheduler运行以观察原生SubResource和释放；采集资源恢复证据后再使用单独control-plane ledger删除自己的Deployment、PodGroup、ConfigMap、Role/Binding、SA、Queue/Quota/ClusterRole。脚本拒绝把Pod与Deployment放在同一回滚批次，也拒绝Node、Namespace、CRD、root/default队列和外部资源。UID与实验标签先检查，删除请求再由API Server原子校验UID；发现同名新对象则停止，不操作替代对象。

回滚删除通过`kubectl delete --raw=精确API路径 -f -`发送DeleteOptions，包含服务器强制检查的UID precondition，防止检查后同名对象被替换而误删；随后GET等待原UID消失，若同名新UID出现则停止。该语义已对照[官方kubectl v0.35.0 RunDelete](https://raw.githubusercontent.com/kubernetes/kubectl/v0.35.0/pkg/cmd/delete/delete.go)，仅离线测试了命令/条件构造，本轮没有执行DELETE。

默认保留两个空Namespace以供审计。节点隔离保护最后由管理员在确认全部实验资源释放后恢复；本脚本不删标签/taint，不清理hamivgpu锁或checkpoint，不触及任何原DQN对象。当前没有创建集群对象，因此本阶段无需集群回滚。
