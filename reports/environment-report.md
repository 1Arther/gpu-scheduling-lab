# 第一阶段环境检查报告

采集日期：2026-10-15。集群快照 UTC：2026-10-15T12:00:01.110348+00:00。GPU 利用率另取后续短时快照，并非原子同步测量。

本报告以本轮 SSH、kubectl、nvidia-smi 和运行时命令输出为依据。没有创建 Pod、拉取镜像、安装或升级组件、重启服务、改动驱动/调度配置。没有复用旧实验结果作为本项目数据。

## 1. 结论

- 已连通并检查三台实验服务器，实测共 11 张 GPU；集群还保留一个不可达的 A40 节点，未主动 SSH 检查，未纳入实验。
- Kubernetes API Server 为 `v1.35.0`，kubectl 为 `v1.35.0`。三个实验节点均 Ready。
- Volcano 组件镜像标签为 v1.13.1，调度器、控制器、Admission 均为 1/1 Ready。实际挂载二进制的 `--version` 返回提交号 `0c6f3bf706ad6790b1e806ef5e65c2ffdb3418ba`，不是语义版本字符串；因此不能把镜像标签直接当作未修改的官方 scheduler 构建。
- `deviceshare.VGPUEnable: true`，已有 volcano-vgpu-device-plugin v1.11.0，三台实验节点均注册 vGPU 资源；不是仅凭“安装 Volcano”推断已启用。
- 普通 `nvidia.com/gpu` 资源只在 4090 节点提供。L40、5090 当前提供的是 `volcano.sh/vgpu-*` 接口，不能直接提交普通 GPU 资源请求期待在这两节点运行。
- 4090 有 8 个此前已 Succeeded 的 Volcano vGPU Pod，说明存在历史调度/容器退出成功记录；不是本阶段新测试，不能据此确认配额隔离、性能或其它两台节点的端到端成功。
- 5090 硬件、驱动及 NVIDIA runtime 的配置前提具备，但没有发现缓存的 CUDA/PyTorch 测试镜像。本轮没有创建测试 Pod 或运行 CUDA 内核，因此镜像、框架与 HAMi-core 的完整兼容性仍未验证。
- 当前存在本地 scheduler 二进制挂载和 DQN gRPC 配置/服务。本项目没有开发或导入这些源码，也没有修改现有服务；后续建立默认 Volcano 对照前必须明确这项现存依赖。

## 2. Kubernetes 节点

| 节点 | InternalIP | Ready | kubelet | 运行时 | 纳入实验 |
| --- | --- | --- | --- | --- | --- |
| r5300 | 172.16.20.32 | Unknown | v1.35.0 | containerd://2.2.2 | 否（用户排除的 A40） |
| t3dgq | 172.16.10.33 | True | v1.35.0 | containerd://2.2.6 | 是 |
| t3dgq-2 | 172.16.10.34 | True | v1.35.0 | containerd://2.2.6 | 是 |
| t3dmg | 172.16.20.34 | True | v1.35.6 | containerd://2.2.6 | 是 |

`r5300` 的 Ready 为 Unknown，带 unreachable 的 NoSchedule/NoExecute taint，状态和资源信息可能是历史残留。不能把其 Pod 显示 Running 当作当前健康证据。5090 kubelet 与控制面是同一 minor 的不同 patch，本阶段只记录，不升级。

管理账号 `/home/zbs/.kube/config` 不存在，默认 kubectl 报 `localhost:8080 refused`。随后只读确认并使用现有 `/etc/kubernetes/admin.conf` 成功查询；没有复制凭据或修改 current-context。

## 3. GPU 资源清单

| 节点 | GPU | 型号 | UUID | 总显存 MiB | Compute capability |
| --- | --- | --- | --- | --- | --- |
| t3dgq | 0 | NVIDIA GeForce RTX 4090 | GPU-7b94f673-ce00-719b-2c94-706baa032dae | 24564 | 8.9 |
| t3dgq | 1 | NVIDIA GeForce RTX 4090 | GPU-fb6aaeaf-9ca4-3c9a-7b3b-f56714c1f7cd | 24564 | 8.9 |
| t3dgq | 2 | NVIDIA GeForce RTX 4090 | GPU-28cf7186-0bdd-be9f-92e5-3c59b4e92c1e | 24564 | 8.9 |
| t3dgq | 3 | NVIDIA GeForce RTX 4090 | GPU-27be0024-0498-4114-e6b9-b2d0db148309 | 24564 | 8.9 |
| t3dmg | 0 | NVIDIA GeForce RTX 5090 | GPU-fffbc9bd-6d21-4287-ff97-7a02b17e9ad6 | 32607 | 12.0 |
| t3dmg | 1 | NVIDIA GeForce RTX 5090 | GPU-412f170c-a414-091e-1203-28751d20a1f3 | 32607 | 12.0 |
| t3dmg | 2 | NVIDIA GeForce RTX 5090 | GPU-d078c4f4-f0ac-6469-7960-0beee158ca7a | 32607 | 12.0 |
| t3dmg | 3 | NVIDIA GeForce RTX 5090 | GPU-9ba09ba0-9b12-8014-4c2e-fd48889ccc9f | 32607 | 12.0 |
| t3dgq-2 | 0 | NVIDIA L40 | GPU-d8482de5-c31d-9b24-f28b-217dc071a5d9 | 46068 | 8.9 |
| t3dgq-2 | 1 | NVIDIA L40 | GPU-69fd2a04-950c-e3fd-3c78-2c2525113383 | 46068 | 8.9 |
| t3dgq-2 | 2 | NVIDIA L40 | GPU-ea84e352-a3f2-bdfe-03a9-f33051f2609e | 46068 | 8.9 |

显存为 `nvidia-smi --query-gpu=...memory.total...` 的 MiB 口径，不以市场标称容量代替实测值。

### 驱动与容器工具

| 节点 | 驱动 | 驱动报告 CUDA 上限 | Container Toolkit CLI | NVIDIA runtime | containerd | 默认 runtime |
| --- | --- | --- | --- | --- | --- | --- |
| t3dgq | 550.163.01 | 12.4 | NVIDIA Container Toolkit CLI version 1.18.2 | NVIDIA Container Runtime version 1.18.2 | containerd containerd v2.2.6 11ce9d5f3c68c941867e82890e93e815c1304f1b | runc |
| t3dmg | 610.43.02 | 13.3 | NVIDIA Container Toolkit CLI version 1.20.1 | NVIDIA Container Runtime version 1.20.1 | containerd containerd v2.2.6 11ce9d5f3c68c941867e82890e93e815c1304f1b | nvidia |
| t3dgq-2 | 550.163.01 | 12.4 | NVIDIA Container Toolkit CLI version 1.19.1 | NVIDIA Container Runtime version 1.19.1 | containerd containerd v2.2.6 11ce9d5f3c68c941867e82890e93e815c1304f1b | nvidia |

4090 宿主机 nvcc 为 CUDA 12.4，L40 为 CUDA 12.2；5090 的 PATH 未找到 nvcc。宿主机 nvcc 缺失不等于无法运行带 CUDA 用户态库的容器。`nvidia-smi` 中 CUDA/UMD Version 是驱动能力，不能当作已安装 Toolkit 版本。

4090、5090 的 containerd 主文件使用 imports 导入 runtime 配置；只检查主文件会漏掉 NVIDIA handler。本轮已查询合并后的 `containerd config dump`。4090 还用 `crictl info` 核对了运行时：默认 runc，但存在 nvidia handler，使用 `runtimeClassName: nvidia`。5090 的合并配置默认 nvidia，存在 NVIDIA handler 和 CDI 设备；其 PATH 缺少 crictl，已用现有 ctr 读取 server 版本与 CRI 插件状态，未安装新工具。L40 的 CRI 信息显示默认 nvidia。

三台节点的 NVIDIA Container Runtime 均能返回版本。RuntimeClass `nvidia` 已在集群存在。这里只确认工具、配置和组件状态，不冒称新容器的 GPU 注入已经测试通过。

### 检查时已有 GPU 占用

| 节点/GPU | 已用显存 MiB | 空闲显存 MiB | GPU 利用率 % |
| --- | --- | --- | --- |
| t3dgq/0 | 4145 | 20067 | 91.0 |
| t3dgq/1 | 11869 | 12342 | 22.0 |
| t3dgq/2 | 11221 | 12990 | 53.0 |
| t3dgq/3 | 15157 | 9055 | 99.0 |
| t3dmg/0 | 31044 | 1107 | 0.0 |
| t3dmg/1 | 31044 | 1107 | 0.0 |
| t3dmg/2 | 31182 | 969 | 0.0 |
| t3dmg/3 | 31182 | 969 | 0.0 |
| t3dgq-2/0 | 44049 | 1325 | 100.0 |
| t3dgq-2/1 | 42369 | 3004 | 92.0 |
| t3dgq-2/2 | 36601 | 8773 | 0.0 |

本轮 API 快照中，活跃 Pod 声明 GPU 资源的数量为 0。但宿主机 GPU 已有显存/计算占用，说明不能用 Kubernetes 未声明配额或瞬时利用率 0% 作为 GPU 独占空闲的证明。本次没有停止任何进程。

## 4. Volcano 及 vGPU

### 组件现状

| 资源 | 镜像 | 当前状态 |
| --- | --- | --- |
| volcano-system/dqn-scheduler-grpc | pytorch/pytorch:2.2.2-cuda12.1-cudnn8-runtime | 1/1 Ready |
| volcano-system/volcano-admission | docker.io/volcanosh/vc-webhook-manager:v1.13.1 | 1/1 Ready |
| volcano-system/volcano-controllers | docker.io/volcanosh/vc-controller-manager:v1.13.1 | 1/1 Ready |
| volcano-system/volcano-scheduler | volcanosh/vc-scheduler:v1.13.1 | 1/1 Ready |
| kube-system/volcano-device-plugin | docker.io/projecthami/volcano-vgpu-device-plugin:v1.11.0, docker.io/projecthami/volcano-vgpu-device-plugin:v1.11.0 | 3/3 Ready |
| nvidia-device-plugin/nvidia-device-plugin-daemonset | nvcr.io/nvidia/k8s-device-plugin:v0.17.0 | 1/2 Ready |

vGPU DaemonSet 的在役目标为 3，Ready 为 3；另有 A40 上的历史 Pod/numberMisscheduled 信息。NVIDIA Device Plugin v0.17.0 的目标为 2，Ready 为 1，覆盖 4090 和被排除的 A40；其 nodeSelector 是 `nvidia.com/gpu.present=true`，L40/5090 没有这个选择标签。

### 安装/管理方式证据

部署形式是 Deployment、DaemonSet、ConfigMap、Volcano CRD 等 Kubernetes 对象。管理记录包含 `kubectl-client-side-apply`、`kubectl-patch` 和 `kubectl-rollout`，并存在 last-applied 注解，与通过 YAML/apply 部署和维护一致。未发现 Helm 管理标识，helm 命令不在当前 PATH；初始安装命令本身没有核实，不能仅凭没有 Helm CLI 断言最初一定不是 Helm 安装。以下是带 managedFields 的只读查询结果：

```json
[
  {
    "name": "dqn-scheduler-grpc",
    "managed_by_commands": [
      "kube-controller-manager",
      "kubectl-client-side-apply"
    ],
    "has_last_applied_annotation": true,
    "helm_annotations": {}
  },
  {
    "name": "volcano-admission",
    "managed_by_commands": [
      "kube-controller-manager",
      "kubectl-client-side-apply",
      "kubectl-patch"
    ],
    "has_last_applied_annotation": true,
    "helm_annotations": {}
  },
  {
    "name": "volcano-controllers",
    "managed_by_commands": [
      "kube-controller-manager",
      "kubectl-client-side-apply",
      "kubectl-rollout"
    ],
    "has_last_applied_annotation": true,
    "helm_annotations": {}
  },
  {
    "name": "volcano-scheduler",
    "managed_by_commands": [
      "kube-controller-manager",
      "kubectl-client-side-apply",
      "kubectl-patch",
      "kubectl-rollout"
    ],
    "has_last_applied_annotation": true,
    "helm_annotations": {}
  }
]
```

### 当前调度器配置

```yaml
actions: "enqueue, allocate, backfill"
tiers:
- plugins:
  - name: priority
  - name: gang
    enablePreemptable: false
  - name: conformance
- plugins:
  - name: overcommit
  - name: drf
    enablePreemptable: false
  - name: deviceshare
    arguments:
      deviceshare.VGPUEnable: true
      deviceshare.GPUSelectPolicy: dqn-job
      deviceshare.DQNGRPCEndpoint: dqn-scheduler-grpc.volcano-system.svc.cluster.local:50051
  - name: predicates
  - name: proportion
  - name: nodeorder
  - name: binpack
```

已配置 enqueue/allocate/backfill、priority、gang、drf、predicates、proportion、nodeorder、binpack 和 deviceshare。这是已配置功能清单，不表示每项策略都完成了本项目验证。default/root 队列为 Open。

现存 `pg-gpu-2`、`pg-gpu-test` 出现 0/0 tasks 的 gang 未就绪事件。这是已有 PodGroup 的状态，不是本轮新任务，也不能直接当成 GPU/CUDA 故障。本阶段未清理任何旧对象。

**现有自定义依赖：** scheduler 通过 hostPath 把 `/home/zbs/volcano-v1.13.1-dqn/_output/bin/vc-scheduler` 挂载到 `/vc-scheduler`；文件 SHA256 为 `9f2e1ae3359ce234fd048bc48aab082add99a686631808908b5ae518bdebf4f4`。DQN Service 为 `dqn-scheduler-grpc.volcano-system.svc.cluster.local:50051`，本轮 EndpointSlice 返回 Ready 地址。没有调用 gRPC 推理，也未验证每次调度是否实际经过该接口。

实际二进制仅执行版本查询，输出如下；没有以第二个调度服务启动：

```text
API Version: v1alpha1
Version: 0c6f3bf706ad6790b1e806ef5e65c2ffdb3418ba
Git SHA: 0c6f3bf706ad6790b1e806ef5e65c2ffdb3418ba
Built At: 2026-06-04 20:40:37
Go Version: go1.26.3
Go OS/Arch: linux/amd64
```

### 节点已注册资源

| 节点 | nvidia.com/gpu | vgpu-number | vgpu-cores | vgpu-memory | NVIDIA 插件选择标签 |
| --- | --- | --- | --- | --- | --- |
| t3dgq | 4 | 40 | 400 | 98256 | true |
| t3dgq-2 | 未注册 | 30 | 300 | 138204 | 未设置 |
| t3dmg | 未注册 | 40 | 400 | 130428 | 未设置 |

vGPU 插件参数为 `--device-split-count=10`。因此 4090 的 40、5090 的 40、L40 的 30 是可分配份额计数，不是物理 GPU 数量。cores 注册量相当于每张物理卡 100；memory 注册值与本轮各卡 MiB 容量之和相符。

`volcano-vgpu-node-config` 另含名为 `aio-node67` 的配置（memory scaling 1.8），它不匹配当前三台节点；不能把该值当作当前节点实际生效配置。当前节点注册模式均为 hami-core。没有把 4090、5090、L40 当作支持 MIG 的设备。

4090 同时公布普通 GPU 与 vGPU 两套资源。它们对应相同物理卡，不能相加；后续实验应选择一种资源接口，避免两套账本同时分配同一 UUID。

### 当前可用程度

| 项目 | 判断 |
| --- | --- |
| Volcano 基础服务 | 当前 Running/Ready，API 与配置可读 |
| 普通 GPU Pod，使用 nvidia.com/gpu | 只有 4090 具备已注册资源及 NVIDIA 插件覆盖；Volcano 普通 GPU 路径尚未做本轮端到端验证 |
| L40/5090 普通 GPU Pod | 未注册 nvidia.com/gpu，当前请求该资源不会匹配到这两节点 |
| vGPU 配置与注册 | 三台实验节点具备 scheduler flag、插件、资源、runtime 配置前提 |
| vGPU Pod 端到端 | 4090 有历史 Succeeded 记录；L40/5090 本轮未执行测试，保持未验证 |
| vGPU 显存/算力隔离和性能 | 尚未测试；nvidia-smi 不能证明 CUDA hook 的配额隔离 |
| 基于训练/推理异构画像的差异化调度 | 本新项目尚未开发和验证 |

## 5. RTX 5090 与 CUDA 测试镜像

实测 5090 compute capability 为 12.0，驱动 610.43.02，驱动报告 CUDA UMD 13.3，Toolkit/Runtime 为 1.20.1。NVIDIA 官方将 RTX 5090 列为 12.0 架构；CUDA 12.8 开始提供该代架构支持。对新程序优先选包含 sm_120 或兼容 PTX 的框架镜像，并检查镜像实际支持的架构列表。[架构表](https://developer.nvidia.com/cuda/gpus)、[驱动/Toolkit/架构对应表](https://docs.nvidia.com/datacenter/tesla/drivers/latest/cuda-toolkit-driver-and-architecture-matrix.html)

4090 的现有缓存包括 `nvcr.io/nvidia/cuda:12.2.0-base-ubuntu22.04` 和 `pytorch/pytorch:2.2.2-cuda12.1-cudnn8-runtime`。5090 的 CRI 缓存只发现 vGPU 插件，Docker 也没有 CUDA/PyTorch 镜像。用户本阶段没有指定一个已准备好的测试镜像，不能对“当前镜像”作肯定运行结论。

旧 CUDA 程序若包含合适 PTX，可能向前兼容；不能一概断言旧版本全部不可用。反过来，仅能运行 nvidia-smi 也不能证明框架 CUDA 内核可运行。[Blackwell 兼容说明](https://docs.nvidia.com/cuda/archive/12.8.2/blackwell-compatibility-guide/index.html)

## 6. 缺口与下一阶段准备

1. 现有 vGPU 关键组件已安装，本轮没有依据要求重装 Volcano 或 Device Plugin。若后续需要 L40/5090 使用普通 nvidia.com/gpu，须另行评估插件覆盖和双资源接口冲突，不能直接在本阶段安装。
2. 确认后续对照基线：当前不是仅由官方镜像二进制决定的默认 Volcano，存在本地 scheduler 和 DQN 服务依赖。本阶段全部保留原状。
3. 选择并固定能覆盖 sm_89 与 sm_120 的训练/推理镜像和 digest，同时核对 550 与 610 两组驱动约束。没有统一镜像兼容实测前，不把不同框架版本的结果混为异构硬件差异。
4. 下一阶段先编写最小 CUDA 可见性/矩阵运算程序，确认 GPU 执行成功；随后写一个固定输入的轻量训练程序和对应推理程序。训练包含反向传播和 optimizer.step；推理使用 eval/inference_mode，统一模型、输入形状、精度和 batch。
5. 记录 GPU UUID、镜像 digest、驱动、框架/CUDA、batch、预热步数、吞吐和时延；真实性能测量时记录已有负载。初期只做单 Pod、单 GPU，后续再验证 vGPU 共享与资源限制。
6. `benchmarks/`、`profiles/`、`k8s/`、`scheduler/` 当前只有目录占位，未生成业务代码、性能数据、测试 YAML 或自定义算法。

## 7. 可直接复查的准确命令

管理节点中使用现有管理配置；这些命令只读：

```bash
k() { sudo -n kubectl --kubeconfig=/etc/kubernetes/admin.conf "$@"; }
k version -o json
k get nodes -o wide
k get daemonsets -A
k get deployments -n volcano-system
k get runtimeclass nvidia -o yaml
k get configmap volcano-scheduler-configmap -n volcano-system -o yaml
k get node t3dgq -o json
k get node t3dgq-2 -o json
k get node t3dmg -o json
python3 scripts/check_cluster.py --kubeconfig /etc/kubernetes/admin.conf --sudo-kubectl > results/cluster-check.jsonl
ssh bszh@172.16.20.34 'python3 -' < scripts/check_node.py > results/node-5090-check.jsonl
ssh bszh@172.16.10.34 'python3 -' < scripts/check_node.py > results/node-l40-check.jsonl
ssh bszh@172.16.20.34 'sudo -n ctr -n k8s.io images list'
```

本机复现整轮采集，不保存密码：

```text
python scripts/collect_remote.py --known-hosts ../server_known_hosts --management-kubeconfig /etc/kubernetes/admin.conf --sudo-kubectl
```

### 下一阶段的普通 GPU 可见性任务示例（本阶段未执行）

下列命令先生成本地 YAML。只有在下一阶段确认 GPU 可用、允许创建测试工作负载后，才执行提交和等待命令；测试只检查容器 GPU 可见性，不是性能基准或 CUDA 内核兼容性验证。镜像来自本轮 4090 已有缓存。

```bash
k run gpu-smoke-4090 --image=nvcr.io/nvidia/cuda:12.2.0-base-ubuntu22.04 --restart=Never --dry-run=client -o yaml \
  --overrides='{"spec":{"schedulerName":"volcano","runtimeClassName":"nvidia","nodeSelector":{"kubernetes.io/hostname":"t3dgq"},"containers":[{"name":"gpu-smoke-4090","image":"nvcr.io/nvidia/cuda:12.2.0-base-ubuntu22.04","imagePullPolicy":"IfNotPresent","command":["nvidia-smi","-L"],"resources":{"requests":{"nvidia.com/gpu":"1"},"limits":{"nvidia.com/gpu":"1"}}}]}}' > k8s/gpu-smoke-4090.yaml
# 以下会创建 Pod，只供下一阶段使用，本阶段未执行：
k apply -f k8s/gpu-smoke-4090.yaml
k wait '--for=jsonpath={.status.phase}=Succeeded' pod/gpu-smoke-4090 --timeout=120s
k logs gpu-smoke-4090
```

5090 的测试镜像尚未核定，本阶段不提供一个未经验证的镜像标签冒充可运行结果。下一阶段需先准备兼容镜像与最小 CUDA 程序，再生成对应 vGPU/普通 GPU YAML。

## 8. 原始证据与限制

- `results/phase1-20261015-live/`：三节点版本、GPU 查询、配置、包版本；同时保留默认 kubeconfig 失败记录。
- `results/phase1-20261015-access/`：已有 kubeconfig 路径可读性、ctr 版本/CRI 插件/缓存镜像。
- `results/phase1-20261015-admin/`：使用现有管理配置成功查询的 nodes、pods、controllers、ConfigMap 和系统组件日志。
- `results/phase1-20261015-followup/`：合并 runtime 配置、CDI、现有二进制哈希、DQN Service/EndpointSlice。
- `results/phase1-20261015-gpu-status/`：简明 nvidia-smi、各卡显存/利用率实际输出。
- `results/phase1-20261015-provenance/`：显式保留 managedFields 的 Deployment 管理方式查询。
- `results/phase1-20261015-version/`、`results/phase1-20261015-version-retry/`：末次版本查询曾 SSH 超时，重试后成功，均保留原始结果。
- `results/phase1-inventory.json`：由上述记录提取的机器可读清单，不是性能画像或实验训练数据。

首轮 4090/5090 的 nvidia-smi XML 超过旧采集长度，被截断；本报告的型号/UUID/容量采用完整 CSV 查询，CUDA 上限和当前占用采用后续完整简明查询，不依赖截断 XML。后续采集脚本已提高限制并显式记录截断标记。

部分 A40 Pod 的日志查询返回认证/访问错误，且该节点 Ready Unknown，未据其历史 Pod 状态推断健康。远端 helm/crictl/nvcc 等命令缺失保留原始错误，不把诊断 CLI 缺失直接当作运行时或 GPU 故障。

参考：[Kubernetes GPU 资源](https://kubernetes.io/docs/tasks/manage-gpus/scheduling-gpus/)、[Volcano vGPU 组件与配置](https://github.com/volcano-sh/volcano/blob/master/docs/user-guide/how_to_use_volcano_vgpu.md)、[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)。文档仅用于解释检查标准，集群结论来自本轮真实输出。
