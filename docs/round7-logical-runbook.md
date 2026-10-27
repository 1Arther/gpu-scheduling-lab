# 第七轮逻辑分配：执行命令与回滚（尚未获准执行写操作）

> 最后复核已更新：[最小待批准操作](round7-final-approval.md)。优先评估管理员协调提交窗口；本文原有“先安装集群级准入”的命令现为另行审批的备选，不得默认执行。旧采样和验证记录保留。

本轮只执行了只读检查、独立目录文件生成和Linux CPU测试。以下apply/scale/patch/create/delete命令全部是**待批准的操作清单**，未执行。禁止整目录apply，也禁止把A和B两套方案一起运行。当前镜像含占位值，文件还不能直接部署。

## 1. 当前可以重复执行的只读与离线命令

管理节点独立目录：

```bash
cd /home/zbs/rf-round7-reassess-20261019
python3 scripts/round7_window_preflight.py --sudo-kubectl --kubeconfig /etc/kubernetes/admin.conf --node t3dgq
python3 scripts/round7_collect.py --cluster --sudo-kubectl --kubeconfig /etc/kubernetes/admin.conf > results/read-only-new.jsonl
```

Linux完整回归使用已有CPU虚拟环境，输出目录必须是新的，防止覆盖历史：

```bash
/home/zbs/rf-ablation-20261019-1425/lab/.venv/bin/python scripts/round7_reassessment_check.py --output-dir results/reassess-repro-01
```

该脚本设置不存在的KUBECONFIG，只启动loopback模型服务，用fake-client执行Go原生分配、Python测试和编译；不启动真正连接集群的scheduler。两进程RPC调用是真实的，Kubernetes API操作是fake。

## 2. 阶段二必须重新确认的条件

1. 用户和集群管理员明确批准窗口时段、新GPU/DRA Pod暂停范围、sleep容器启动开销、实验对象和回滚。无需停止现有GPU程序；不得将“只sleep”解释成已经获业务授权。
2. 选定A或B。B初始只使用t3dgq四个UUID，清单见logical-inventory.json。A40排除。5090/L40扩大范围前需镜像缓存、runtime/preload及启动授权。
3. 新镜像从冻结源构建并写成真实digest；RF模型版本、特征与评分SHA不变；目标节点能获得镜像。当前没有构建/推送镜像，也没有可编造的digest。
4. B的准入规则须经API Server类型检查和正反例server dry-run验证，所有API Server都生效。当前仅完成离线CEL验证。
5. 准入窗口建立后，再次确认集群没有原有活跃／Pending GPU/DRA请求、有限时间戳未消费分配及在途原scheduler预留。等到旧调度循环/在途DQN调用结束并核实缓存已处理相关事件；固定等待若干秒不能单独作为证明。当前API零请求不是完整的私有账本dump；如果无法排除先前未发布预留，B仍禁止部署，应采用另获批准的统一权威方案A或继续离线。
6. 历史MAX时间戳、已清空临时注解的终态Pod不自动构成活跃预留。checkpoint遗留记录不自动等于物理占用或新分配成功；不能清理它们来“修复测试”。如果首个Pod在kubelet/plugin分配失败，停止后续提交并保留记录。
7. 原DQN ConfigMap/二进制/Deployment备份和校验值已保存，新实验名称无外部对象冲突。root Queue已存在，RBAC通过审查。scheduler启动会尝试Create root和实验默认Queue，root AlreadyExists不表示覆盖root。

## 3. B方案的准确命令、目标与影响

仅在另获阶段二批准且镜像占位已替换后，在管理节点使用以下函数。此定义在文档内，不代表本轮执行过：

```bash
k() { sudo -n kubectl --kubeconfig=/etc/kubernetes/admin.conf "$@"; }
cd /home/zbs/rf-round7-reassess-20261019
mkdir -p results/round7-real-run-01
k -n volcano-system get deployment volcano-scheduler -o json > results/round7-real-run-01/old-scheduler.json
k -n volcano-system get configmap volcano-scheduler-configmap -o json > results/round7-real-run-01/old-configmap.json
sha256sum /home/zbs/volcano-v1.13.1-dqn/_output/bin/vc-scheduler > results/round7-real-run-01/old-binary.sha256
```

先GET确认实验Namespace及所有同名对象不存在。发现外部同名资源即停止，不用apply接管。以下创建独立控制面，Deployment仍为0副本：

```bash
k apply -f k8s/round7-logical-window/00-namespaces.yaml
k apply -f k8s/round7-logical-window/01-rbac.yaml
k apply -f k8s/round7-logical-window/02-queue-quota.yaml
k apply -f k8s/round7-logical-window/03-configmaps.yaml
k apply -f k8s/round7-logical-window/04-scheduler-DRAFT.yaml
```

影响：只增加实验Namespace、SA/RBAC、Queue/Quota、ConfigMap和0副本Deployment。Queue为集群对象，RBAC的queues.create是宽于具体名称的权限，不能声称全部只影响Namespace。RF侧car不挂载API token，节点写权限仅t3dgq。部署镜像必须先在最终YAML diff中固定digest，不能带build-required执行。

构建文件复用`k8s/round7/Dockerfile.scheduler`、`Dockerfile.rf`；Go产物复制到**独立项目**的build/round7，不能复制到线上原二进制路径。registry/镜像digest尚未产生，实际build/push及目标镜像审查单必须在执行前补齐。

建立准入窗口（这是集群范围新增GPU/DRA Pod拒绝规则，必须单独批准其影响）：

```bash
k apply -f k8s/round7-logical-window/05-window-policy-DRAFT.yaml
k get validatingadmissionpolicy rf-v7-gpu-window -o yaml
k apply -f k8s/round7-logical-window/06-window-binding-DRAFT.yaml
# 只发server dry-run，不保存对象。必须确认错误来自本窗口Policy，而非RBAC/格式错误。
k create --dry-run=server -f k8s/round7-logical-window/probe-denied-DRAFT.yaml -o yaml
# 预期上一条Forbidden；若被允许，立即停止，不能启动RF。
k create --dry-run=server -f k8s/round7-logical-window/test-a-training.yaml -o yaml
# 预期该规定sleep模板被准入；此dry-run本身不进行RF调度。
python3 scripts/round7_window_preflight.py --sudo-kubectl --kubeconfig /etc/kubernetes/admin.conf --node t3dgq > results/round7-real-run-01/window-preflight.json
k -n volcano-system logs deployment/volcano-scheduler --since=60s --timestamps > results/round7-real-run-01/old-scheduler-window.log
```

还需针对普通nvidia.com/gpu、requests-only、initContainer及DRA请求做server dry-run反例，模板来自policy-cases.json；只有规则来源明确的拒绝才能记窗口验证成功。多API Server必须逐一核对传播。新建GPU请求被拒绝时控制器可能重试，现有任务不删除。Binding只在窗口结束、RF完全停止后移除。

检查第2节全部条件后，才能启动新scheduler：

```bash
k -n gpu-rf-system scale deployment volcano-rf-test --replicas=1
k -n gpu-rf-system rollout status deployment/volcano-rf-test --timeout=120s
k -n gpu-rf-system get lease volcano-rf-test -o yaml
k -n gpu-rf-system logs deployment/volcano-rf-test -c scheduler --timestamps
k -n gpu-rf-system logs deployment/volcano-rf-test -c rf-model --timestamps
```

这一步会让独立scheduler写自己的Lease、握手/短时节点锁及实验对象；不会切换DQN策略。HTTP Ready不是已获得leader、cache同步或目标节点正确的证据，要另查日志、Lease和cache目标。未确认不得创建测试Pod。

## 4. 六项真实实验顺序

先启动只读采集，或从另一管理终端运行：

```bash
sudo -n python3 scripts/round7_capture.py --kubeconfig /etc/kubernetes/admin.conf --output results/round7-real-run-01/capture --seconds 120
```

各用例都需记录真实UID。只运行一个单Pod用例，清理并确认账本恢复后再进入下一个；不要一次apply所有test文件。

| 实验 | 获准后准确提交命令 | 主要核验 |
|---|---|---|
| 1 training | `k apply -f k8s/round7-logical-window/test-a-training.yaml` | RF请求ID、原始分类与分项分数、最终Allocate UUID、插件实际消费UUID |
| 2 inference | `k apply -f k8s/round7-logical-window/test-b-inference.yaml` | 同样4096/20/1；不要求UUID必须与训练不同 |
| 3 A→B | 先`k apply -f k8s/round7-logical-window/test-c-first.yaml`，分配证据齐全后再`k apply -f k8s/round7-logical-window/test-c-second.yaml` | B的请求快照包含A占用；每卡均检查硬约束与直接账本增量 |
| 4 释放A再提交 | 用UID回滚脚本删除自己的A并证实释放，再`k apply -f k8s/round7-logical-window/test-d-after-release.yaml` | 配额恢复、重新分类和新UUID；不能靠重启scheduler来冒充释放 |
| 5 on/off | 基线恢复后切配置，分别提交`test-e-control.yaml`或`test-g-score-*.yaml` | 相同逻辑状态／请求／启发式权重，只切RF学习项；原生Binpack另列 |
| 6 服务不可用 | 基线恢复后切service-unavailable配置，再`k apply -f k8s/round7-logical-window/test-f-fallback.yaml` | Go回退原因、UUID合法性、账本增量与释放；不停止任何DQN服务 |

配置切换只修改**本轮**Deployment的卷，Recreate确保RF on/off实例不并行抢占。每次先清理自己的Pod、保留释放证据，再执行：

```bash
k -n gpu-rf-system patch deployment volcano-rf-test --type=strategic -p '{"spec":{"template":{"spec":{"volumes":[{"name":"config","configMap":{"name":"rf-v7-config-no-prediction"}}]}}}}'
# 恢复学习项：把上述name改为rf-v7-config-on。
# 服务异常：把name改为rf-v7-config-service-unavailable，endpoint为127.0.0.1:1。
# 整体策略对照：把name改为rf-v7-config-native-binpack，不能称为只关闭RF预测项。
```

确认首个链路后，如要扩大到4090/5090/L40共11卡，需另审查`expand-three-nodes/`的Deployment、节点RBAC、Policy和两个评分Pod差异，核对两新增节点固定sleep镜像缓存和启动授权；保持原窗口Binding生效。三个重复`--node-selector`按源码OR匹配，不添加/修改节点标签，不调DQN选节点规则。扩展文件仍为replicas=0和占位镜像，必须填实际digest，并明确扩展启动副本数；不能在初始4卡用例中悄悄扩大范围。

## 5. UUID与逻辑账本采集

```bash
k -n gpu-rf-tests get pod rf-v7-a-training -o json > results/round7-real-run-01/a-pod.json
k -n gpu-rf-system logs deployment/volcano-rf-test -c rf-model --timestamps > results/round7-real-run-01/rf-advice.log
k -n gpu-rf-system logs deployment/volcano-rf-test -c scheduler --timestamps > results/round7-real-run-01/native-allocate.log
python3 scripts/round7_collect.py --cluster --sudo-kubectl --kubeconfig /etc/kubernetes/admin.conf > results/round7-real-run-01/cluster-after.jsonl
python3 scripts/round7_collect.py --supplement > results/round7-real-run-01/checkpoint-after.jsonl
```

最后一条必须在实际分配节点执行，不能拿管理节点的checkpoint解释远程节点。按Pod UID+容器名+vgpu-number的AllocateResponse.envs查NVIDIA_VISIBLE_DEVICES，或读取对应CRI runtime配置；不需要exec进入容器。插件日志按相同UID/时间/UUID关联。如果日志没有UID或检查点格式不支持，记录缺失证据。

保留三项独立值：RF推荐UUID、成功原生Allocate后永久注解UUID、Device Plugin消费UUID。临时devices-to-allocate清空正常，不拿空串判成功或失败。rf_advice中的logical snapshot、Go日志logicalBefore/logicalAfter是直接账本来源；API注解重建单列。Pod Running只表示启动状态，不是CUDA或三方一致证明。

统计时给出有效样本数、缺失证据数。三方一致率只以证据完整样本为分母；未测试项为null，不能填0错误/100%成功。逻辑资源超分配每卡分别校验显存、cores≤100、实例≤注册上限。类别／UUID不变也保留完整结果，不调模型或分数制造差异。

## 6. 停止条件

- 窗口反例被准入、策略被修改、旧scheduler/其他入口出现新的GPU逻辑预留、无法排除既有在途预留。
- 任一层UUID不一致、未知UUID、配额超量/负值/重复、删除后不能恢复、节点锁长期不释放。
- 插件找错Pending Pod、Allocate失败、镜像/preload/runtime错误、模型版本或来源不符。
- 影响已有业务，或出现未批准的GPU/DRA新建中断范围、真实CUDA活动来自本实验容器。

只停止自己的后续提交并保留证据。不得结束其他人的进程、清理checkpoint/NodeLock或修改驱动/plugin/runtime。RF pending保守排序与服务不可用的Go回退分别记录；不能把回退也记成已证明RF学习项起效。

## 7. B完整回滚（保留窗口到最后）

先导出自己真实创建的对象，不能手写/生成假UID。以下均为GET或本地文件转换：

```bash
k -n gpu-rf-tests get pods -l gpu-lab/experiment=round7-rf -o json > results/round7-real-run-01/own-pods.json
python3 scripts/round7_record_ledger.py --input results/round7-real-run-01/own-pods.json --output results/round7-real-run-01/pods-ledger.json
python3 scripts/round7_rollback.py --ledger results/round7-real-run-01/pods-ledger.json
```

确认计划且回滚获准后，在具备本次批准的kubectl身份下执行：

```bash
GPU_LAB_ROUND7_ROLLBACK_APPROVED=yes python3 scripts/round7_rollback.py --ledger results/round7-real-run-01/pods-ledger.json --execute
```

该脚本在API Server删除请求中加UID precondition，避免误删同名替代对象。保留scheduler运行直到采到原生SubResource和配额恢复证据；再停止自己的scheduler：

```bash
k -n gpu-rf-system scale deployment volcano-rf-test --replicas=0
k -n gpu-rf-system get pods -l app=volcano-rf-test -o wide
k -n gpu-rf-system get deployment,configmap,role,rolebinding,serviceaccount -l gpu-lab/experiment=round7-rf -o json > results/round7-real-run-01/own-system.json
k -n gpu-rf-tests get podgroup,resourcequota -l gpu-lab/experiment=round7-rf -o json > results/round7-real-run-01/own-test-metadata.json
k get queue,clusterrole,clusterrolebinding -l gpu-lab/experiment=round7-rf -o json > results/round7-real-run-01/own-cluster.json
python3 scripts/round7_record_ledger.py --input results/round7-real-run-01/own-system.json results/round7-real-run-01/own-test-metadata.json results/round7-real-run-01/own-cluster.json --output results/round7-real-run-01/control-ledger.json
python3 scripts/round7_rollback.py --ledger results/round7-real-run-01/control-ledger.json
GPU_LAB_ROUND7_ROLLBACK_APPROVED=yes python3 scripts/round7_rollback.py --ledger results/round7-real-run-01/control-ledger.json --execute
```

最后保存准入对象UID，确认没有自己的Pod、RF Deployment/活动scheduler Pod，再撤销新请求暂停：

```bash
k get validatingadmissionpolicy,validatingadmissionpolicybinding -l gpu-lab/experiment=round7-rf -o json > results/round7-real-run-01/own-window.json
python3 scripts/round7_record_ledger.py --guard --input results/round7-real-run-01/own-window.json --output results/round7-real-run-01/window-ledger.json
python3 scripts/round7_window_rollback.py --ledger results/round7-real-run-01/window-ledger.json
GPU_LAB_ROUND7_ROLLBACK_APPROVED=yes python3 scripts/round7_window_rollback.py --ledger results/round7-real-run-01/window-ledger.json --execute
```

窗口脚本拒绝在RF仍运行或实验Pod仍存在时解封，按Binding→Policy删除，逐对象校验实验Label与UID并由服务器强制检查UID。它没有自动批准任何操作；本轮只离线验证脚本，从未执行DELETE。上述执行脚本需要能读admin.conf的批准身份；若管理账号没有权限，使用获准的管理员环境或`sudo -n env KUBECONFIG=/etc/kubernetes/admin.conf GPU_LAB_ROUND7_ROLLBACK_APPROVED=yes python3 ...`，不能复制凭据到项目。

复查原DQN Deployment、ConfigMap和二进制SHA与备份一致，其他用户任务不变。默认保留空实验Namespace和独立Lease用于审计，它们不再运行或预留GPU；如需删除，应在确认空且另有授权后精确操作。绝不删除root/default Queue、原DQN对象、Node或CRD，不清理历史设备记录。

## 8. A方案的替代命令与回滚边界

若管理员选择A，**不得部署B的独立scheduler**。它需要另行批准原scheduler滚动变更/重启及混合路径影响，并先确认所有非实验vGPU使用同一权威、普通GPU别名入口得到管理。备份原Deployment spec和配置。

```bash
# 只在明确选择并批准A、镜像digest固定后：
k apply -f k8s/round7-logical-window/A-canary-config-DRAFT.yaml
k -n volcano-system patch deployment volcano-scheduler --type=strategic --patch-file k8s/round7-logical-window/A-scheduler-patch-DRAFT.yaml
```

新配置仍为dqn-job、原DQN服务地址、原actions/plugins；只对gpu-rf-tests内带experiment=round7-rf且有workload-type的Pod启用RF，非法类型拒绝，缺失标签回原策略。实验Pod的schedulerName需改为`volcano`，不能原样使用B模板。A本身不需要第二个scheduler/独立Lease，也不能靠创建独立Lease让新旧两个权威并行预留。

回滚顺序：停止新增实验Pod→只删除自己的实验Pod并验证释放→先将新ConfigMap中RFCanaryEnable/RFEnable置false并确认下一Session生效，或直接恢复备份Deployment spec→确认原hostPath只读挂载、原配置和旧二进制SHA恢复且leader/cache正常→移除A新ConfigMap/sidecar对象。恢复应以保存的完整原spec为依据，包括strategy、volumes、mounts、token设置和container列表，不能只改回镜像；`rollout undo`不回滚ConfigMap内容。生产回滚JSON必须从当次实际备份生成、检查UID/resourceVersion并再次审查，本阶段不伪造一份未来可直接覆盖生产的通用restore文件。

当前未应用A/B，无集群对象需要回滚。下一步只等待具体窗口/目标/镜像/部署方案的明确批准；现有物理进程不需要先结束。
