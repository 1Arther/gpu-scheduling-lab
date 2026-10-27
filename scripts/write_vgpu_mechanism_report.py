#!/usr/bin/env python3
"""Render the audit report from preserved evidence. No network or model training."""
import argparse
import json
from pathlib import Path


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--evidence',required=True);a=ap.parse_args()
    evidence=Path(a.evidence).resolve();project=Path(__file__).resolve().parents[1]
    summary=json.loads((evidence/'summary/audit-summary.json').read_text(encoding='utf-8'))
    def link(path,label=None,line=None):
        p=Path(path).resolve().as_posix()+(':'+str(line) if line else '')
        return '['+(label or Path(path).name)+'](<'+p+'>)'
    def src(path,line,label):return link(evidence/path,label,line)
    s='deployed-source/pkg/scheduler/'
    u='sources/device-plugin/'
    h='sources/hami-core/'
    k='sources/kubernetes/'
    table=(evidence/'summary/gpu-comparison-table.md').read_text(encoding='utf-8')
    source_table=['| 部署目录源码 | 与官方 v1.13.1 对照 |','|---|---|']
    for r in summary['source_comparison']:
        p=r['file'];status='逐字节一致' if r['matches_official_v1_13_1'] else ('存在定制差异' if r['official_file'] else '自定义文件或未下载官方对照')
        source_table.append('| '+link(evidence/p,p.replace('deployed-source\\',''))+' | '+status+' |')
    process_table=['| 节点 | GPU UUID | PID | 进程 | NVML 进程显存 MiB | 归属依据 |','|---|---|---:|---|---:|---|']
    for p in summary['gpu_processes']:
        cg=p['ownership'].get('cgroup','').strip()
        owner='宿主机用户会话' if '/user.slice/' in cg else ('独立 Docker 容器' if '/docker-' in cg else '见原始 cgroup')
        process_table.append('| '+' | '.join(str(x) for x in [p['host'],p['uuid'],p['pid'],p['process_name'],p['used_memory_mib'],owner+'；`'+cg+'`'])+' |')
    pod_table=['| 当前仍保留的 Pod | 状态 | 节点 | 历史分配 UUID 注解 | 当前活跃扣账 |','|---|---|---|---|---|']
    for p in summary['pods']:
        annotation=p.get('annotations',{}).get('volcano.sh/vgpu-ids-new')
        if annotation:
            active=p.get('phase') not in ('Succeeded','Failed')
            pod_table.append('| '+' | '.join(str(x) for x in [p['namespace']+'/'+p['name'],p['phase'],p.get('node'),annotation,'需计入' if active else '不计入'])+' |')
    text=f'''# Volcano vGPU 逻辑分配与物理资源占用机制审查报告

审查日期：2026-10-19（Asia/Shanghai）。真实采样及源码读取约 10:35—10:47；关键显存对照采样为 10:41:20—10:41:25。对象：4090×4、5090×4、L40×3；A40/r5300 不纳入 GPU 对照分析。

第六轮 RandomForest/Volcano 集成保持暂停。本次只进行了读取、官方源码下载、本地证据整理和离线解析测试，没有创建测试 Pod，没有执行 CUDA 程序，没有修改在线配置、二进制、驱动、模型或原调度算法。

## 1. 可以直接回答的结论

**当前 HAMi-core 软切分路径的调度准入主要依据逻辑资源账本。申请显存是配额上限与逻辑承诺，不是在调度或 Device Plugin Allocate 时立即占用等量物理显存。宿主机未记账的显存占用不会自动扣进该账本。**

这不是说整个系统完全不使用 NVML。插件用 NVML 注册设备总容量、型号与健康状态；独立 monitor 可以读取全卡实际占用；HAMI-core 在应用运行时拦截 CUDA/NVML API。关键在于：**这些实际占用值没有在本次核对的 scheduler 设备适配判定中成为实时空闲显存硬约束。**

| 问题 | 结论 | 证据与边界 |
|---|---|---|
| Volcano vGPU 分配什么？ | 逻辑显存/计算配额、具体设备 UUID 的访问配置，以及运行时软件限额相结合。 | 不是对指定容量显存的提前物理保留；也不是 MIG 硬件分区。当前 11 张卡登记模式均为 hami-core。 |
| YAML 请求 8 GiB 是否立即占用 8 GiB？ | 否。当前 factor=1 的语义为 vgpu-memory:8192；Allocate 设置限额为 8192m。 | 物理显存由后续 CUDA/驱动实际申请；不能保证 sleep 容器绝对增加 0 字节，但不会因为请求值而预占整整 8 GiB。 |
| 主机其他程序占满 GPU，而账本空闲，Pod 还能被选中吗？ | **可能。**相关逻辑显存、算力、实例数及其他约束通过后，设备可能仍是候选，Pod 也可能绑定成功。 | “可能”不等于保证当前 DQN 排名一定选中该卡；队列、锁、插件、健康、镜像、运行时等仍可导致其他失败。 |
| 通常在哪一步暴露物理内存不足？ | 不会因本路径的物理空闲显存硬检查而在调度时拒绝；普通 sleep 容器可能正常启动，应用建立 CUDA 上下文或申请显存时才可能失败。 | 启动脚本若立即调用 CUDA，表现也可能是容器启动后立刻退出/重启；不是一定停留在 Pending，也不一定是 Kubernetes OOMKilled。 |
| 当前监控能否感知物理状态？ | 已实读到三个节点 monitor 的 HostGPUMemoryUsage、HostCoreUtilization；宿主机 nvidia-smi 可直接查询 free。 | 当前 monitor v1.11.0 这条指标代码主要导出 used 和 utilization，未直接导出独立 free/total/reserved gauge；有监控不表示 scheduler 已消费。 |
| RF 应读哪种剩余资源？ | **两者都保留且分字段。**账本维护承诺；物理数据反映当下可用性。 | 现有冻结 RF 的 free_memory 输入仍按旧特征合同表示账面剩余；先将物理显存作为模型之外的硬过滤，避免悄悄改变训练/推理输入语义。 |
| 最小改造是什么？ | 复用节点 monitor 采样 UUID→物理 free/total/reserved/利用率/时间，scheduler 缓存并在过滤与最终选卡处复核。 | 本次只提出接入位置，没有实现、部署或修改在线服务。见第 9 节。 |

针对“32GB 卡被其他进程占用 30GB、申请 8GB”的五个问题：逻辑账本为空时可能仍认为足够；可能调度和绑定成功；Device Plugin 不会替它物理预留 8GB；不运行 CUDA 不会因为这份申请自动增加 8GB 显存占用；后续真正申请显存可能出现物理 OOM。这里区分 GB 和 GiB，本文实际实验草案使用 8192 MiB，即 8 GiB。

## 2. 版本与证据分级

本报告把“实际部署读取”“部署目录现存源码”“官方对照源码”“尚未实际运行验证”分开。现存源码与运行二进制有很强关联，但没有进行可复现构建、反编译或运行路径追踪，因此不能把所有工作树文本都宣称为运行二进制的逐行证明。

| 组件 | 本次确认 | 证据范围 |
|---|---|---|
| Kubernetes | API server v1.35.0；t3dgq、t3dgq-2 kubelet v1.35.0；t3dmg kubelet **v1.35.6**。 | 当前 kubectl version 和 Node.status.nodeInfo；不把所有节点版本都写成 v1.35.0。kubelet 调用链以官方 v1.35.0 源码对照，5090 补丁版本的逐字源码未核对。 |
| Volcano | 镜像标签 v1.13.1，scheduler 实际执行 hostPath 二进制。 | 部署挂载 /home/zbs/volcano-v1.13.1-dqn/_output/bin/vc-scheduler → /vc-scheduler；运行 PID 1978316 的 exe 与磁盘文件 SHA256 一致。 |
| 定制 scheduler | Go build 元数据为 v1.13.1+dirty，vcs.revision=0c6f3bf706ad6790b1e806ef5e65c2ffdb3418ba，vcs.modified=true。 | 二进制 SHA256：9f2e1ae3359ce234fd048bc48aab082add99a686631808908b5ae518bdebf4f4。不能只凭镜像标签当成纯官方 scheduler。 |
| 当前策略 | deviceshare.VGPUEnable=true；GPUSelectPolicy=dqn-job；DQNGRPCEndpoint 指向现有服务。 | 当前 ConfigMap；没有调用 DQN 调度 RPC，也没有修改服务。 |
| vGPU Device Plugin | kube-system/volcano-device-plugin；三台目标节点各有插件和 monitor；镜像 v1.11.0。 | 三节点 Pod.status.imageID 相同：sha256:08bbb8241ed78b8c51a6ac1a54fcf5ecfe8a29cf5f1aa3cf2052f9235a491ed1。不是从 volcano-system 找不到插件就判断缺失。 |
| HAMI-core 注入库 | 三节点 host /usr/local/vgpu/libvgpu.so 与各自插件镜像内 /k8s-vgpu/lib/nvidia/libvgpu.so 完全同哈希。 | SHA256：3b2139aeee5ffd70779059eb3f133b56fc14c99de4d8cacc1693b70d38c14f65。确认实际注入文件与现有镜像对应。 |
| 官方 HAMI-core 对照 | v1.11.0 插件 Git 子模块指向 3506e6222ca0f235cb745bc805832f422c484c17。 | 已下载该固定提交。镜像库没有额外提取出可证明此源码提交的构建证明，故具体 hook 源码结论注明来自官方对应构建源码。 |

部署读取及原始命令：{link(evidence/'live-final-evidence','插件、监控与当前物理采样')}、{link(evidence/'live-linkage','运行二进制及库哈希')}。官方固定版本获取记录及 SHA256：{link(evidence/'sources/manifest.json','官方源码清单')}；额外调用链清单为 linkage-manifest.json、predicates-manifest.json。所有官方源码仅下载为文本，没有编译或执行。

可公开复核的原始版本链接：[Volcano v1.13.1 GPUDevices](https://github.com/volcano-sh/volcano/blob/v1.13.1/pkg/scheduler/api/devices/nvidia/vgpu/device_info.go)、[HAMICoreFactory](https://github.com/volcano-sh/volcano/blob/v1.13.1/pkg/scheduler/api/devices/nvidia/vgpu/hamicore.go)、[Device Plugin v1.11.0 Allocate](https://github.com/Project-HAMi/volcano-vgpu-device-plugin/blob/v1.11.0/pkg/plugin/vgpu/plugin.go)、[设备总量注册](https://github.com/Project-HAMi/volcano-vgpu-device-plugin/blob/v1.11.0/pkg/plugin/vgpu/register.go)、[HAMI-core 固定提交](https://github.com/Project-HAMi/HAMi-core/tree/3506e6222ca0f235cb745bc805832f422c484c17)。定制 utils.go、DQN 文件以本次部署目录副本为准，不能直接用这些官方链接代替。

部署目录的关键 GPUDevice/账本、HAMICoreFactory、NodeInfo、cache、predicates、framework 实现与官方对照逐字节一致；主要定制点在 utils.go、type.go、deviceshare.go 和新增 dqn_client.go。完整对照如下：

{chr(10).join(source_table)}

## 3. A/B/C 三个阶段的实际调用链

```mermaid
flowchart TD
    A["A: API server 保存 Pod 的 vGPU requests/limits"] --> B1["deviceshare Predicate → GPUDevices.FilterNode"]
    B1 --> B2["resourcereqs 解析请求；快照上检查逻辑显存/核配额/实例数"]
    B2 --> B3["可选 DQN 排序；最终仍做逻辑硬约束检查"]
    B3 --> B4["Statement.Allocate → predicates AllocateFunc → GPUDevices.Allocate"]
    B4 --> B5["记账、写入 vgpu-ids-new / devices-to-allocate / vgpu-node"]
    B5 --> B6["DefaultBinder.Bind → API server Binding"]
    B6 --> C1["C: 节点 kubelet DeviceManager.Allocate → 插件 Allocate RPC"]
    C1 --> C2["插件读取注解 UUID；返回 NVIDIA_VISIBLE_DEVICES、限额和库挂载"]
    C2 --> C3["CRI / NVIDIA runtime 创建容器并暴露该设备"]
    C3 --> C4["应用 cuInit / 上下文 / cuMemAlloc 等调用"]
    C4 --> C5["HAMI 限额检查与驱动实际显存分配；可能 CUDA OOM"]
    M["独立 monitor / NVML：全卡实际占用与利用率"] -. "已观测；现有 scheduler 过滤不读取此实时 free" .-> B2
```

| 阶段 | 真实物理显存分配 | 逻辑资源预留 | 物理 GPU UUID | 检查实时物理 free | 逻辑足够但物理不足 |
|---|---|---|---|---|---|
| A：创建 Pod | 没有按请求分配显存 | 保存请求；尚未在某张 GPU 上预留 | 尚未确定 | 不检查 | API 可以接受请求，但不保证能够运行 |
| B：设备过滤和排序 | 不分配 | Predicate 在快照试配；正式 Allocate 更新账本 | 过滤可试选，正式 Allocate 确定并写注解；不能把试选当最终 UUID | 本次核对路径不检查 NVML free | **可能** |
| B：Bind | 不分配请求的显存 | 保持设备及节点账面占用 | 已由注解指定 | Bind 不查询 GPU free | **可能绑定成功** |
| C1：插件 Allocate、容器配置 | 不会预占所请求的 8 GiB；执行的是设备/环境/挂载配置 | kubelet 维护自己的扩展资源设备 ID 记录；不是 CUDA 内存 | 从 Pod 注解消费并设置可见设备 | HAMi-core 分支没有 8 GiB 物理 free 准入检查 | **可能启动成功** |
| C2：CUDA 应用运行 | 此时由上下文、缓冲区、内存池等触发实际占用 | 原申请配额继续作为限额 | 已配置，进程设备可见性生效 | 驱动负责真实分配成功与否；HAMI 另查配额。容器内 free 可能被虚拟化 | **可能 CUDA 内存不足/初始化失败** |

### 3.1 YAML 请求解析与单位

部署源码 {src(s+'api/devices/nvidia/vgpu/utils.go',289,'utils.go:resourcereqs')} 从每个普通 container 的资源配置构造 ContainerDeviceRequest：Nums、Memreq、MemPercentagereq、Coresreq。优先读 limits，显存/cores 可回看 requests；没有 number 但指定 memory 的分支可推成一个设备。未指定显存时可能落到整卡百分比默认值，未指定 cores 默认 0，并非“自动预留剩余物理算力”。这些默认行为不适合当成未声明资源时的物理隔离保证。

本次使用的资源名是 volcano.sh/vgpu-number、volcano.sh/vgpu-memory、volcano.sh/vgpu-cores。memory 读取 Kubernetes Quantity 的整数值，而非自动把“8Gi”转成该协议的 8192 个 MiB 单位。官方插件 {src(u+'pkg/plugin/vgpu/register.go',55,'register.go:apiDevices')} 使用 NVML **memory.Total/(1024×1024)/GPUMemoryFactor** 注册容量；{src(u+'pkg/plugin/vgpu/plugin.go',471,'plugin.go:Allocate 限额环境')} 把 Usedmem×factor 编成尾缀 m，官方 HAMI 的 get_limit_from_env 将 m 乘 1024²。当前注册总量与宿主机 MiB 总量一致，启动参数未覆盖 memory factor，按 factor=1 路径解释；这也是测试草案用整数 8192 的原因。

### 3.2 scheduler 依据总容量减申请账本

部署 {src(s+'api/devices/nvidia/vgpu/device_info.go',93,'device_info.go:NewGPUDevices')} 读取 Node 的 volcano.sh/node-vgpu-register；解码得到 Memory、Number、UUID、Type、Health、sharing mode。**Memory 是注册总量，不是 NVML Free**。

{src(s+'api/devices/nvidia/vgpu/utils.go',573,'utils.go:canFitForOrder')} 和 {src(s+'api/devices/nvidia/vgpu/utils.go',768,'checkNodeGPUSharingPredicateAndScore')} 使用：Memory−UsedMem ≥ Memreq，UsedCore+Coresreq ≤ 100，Number−UsedNum 足够；另有类型、共享模式、满配额独占相关条件。这里没有 nvidia-smi/NVML free 查询。{src(s+'api/devices/nvidia/vgpu/hamicore.go',31,'hamicore.go:TryAddPod/AddPod/SubPod')} 只修改 UsedMem、UsedCore、UsedNum 及 PodMap，不向 CUDA 分配内存。

定制 DQN 也没有把问题自动解决：{src(s+'api/devices/nvidia/vgpu/dqn_client.go',94,'dqn_client.go:gpuStatesForDQN')} 传给服务的是总量和这些账本值；部署目录 {src('deployed-source/DQN2/grpc/dqn_scheduler_server.py',36,'grpc_gpu_to_sim')} 把 memory_free 计算为 total_mem−used_mem，把 core_free 计算为 100−used_core，甚至 util 字段也源自 used_core，**不是实测 GPU utilization**。服务当前源码的排队/排序接收的是这份账本输入；本次没有重新训练或调用它。现存自定义源码无法替代“构建时源码完整性”的独立证明，这一限制仍保留。

节点 Allocatable 是可分配总容量，通常不会随着每个 Pod 请求而直接减少；它也不能当成“当前空闲值”。每卡分配必须结合 Pod 注解、调度器内存账本或对应分配指标重建。

### 3.3 UUID 是如何进入容器的

{src(s+'framework/statement.go',263,'Statement.Allocate')} 通过已注册的 AllocateFunc 事件回调进入 {src(s+'plugins/predicates/predicates.go',201,'predicates.OnSessionOpen:AllocateFunc')}，其中调用 devices.Allocate。它不是 Device Plugin RPC。

随后 {src(s+'api/devices/nvidia/vgpu/device_info.go',261,'GPUDevices.Allocate')} 再试配、在启用时取得节点锁、记录资源，并写 Pod 注解：volcano.sh/vgpu-ids-new、volcano.sh/devices-to-allocate、volcano.sh/vgpu-node、时间与 bind-phase。格式为 UUID,NVIDIA,显存,cores:；多个 container 使用分组编码。{src(s+'cache/cache.go',209,'DefaultBinder.Bind')} 才向 API server 创建 Binding。

在节点，官方 kubelet {src(k+'pkg/kubelet/cm/devicemanager/manager.go',366,'ManagerImpl.Allocate')} → allocateContainerResources → {src(k+'pkg/kubelet/cm/devicemanager/endpoint.go',102,'endpointImpl.allocate')} 调用插件 Allocate RPC。它发生在 kubelet 准备/准入容器设备资源期间，常规路径在容器运行前；GetDeviceRunContainerOptions 在缓存缺失时也可触发 Allocate。不会因为 kubectl apply 刚保存对象就立即调用。设备插件机制说明见 [Kubernetes 官方文档](https://kubernetes.io/docs/concepts/extend-kubernetes/compute-storage-net/device-plugins/)。

官方插件 {src(u+'pkg/plugin/vgpu/plugin.go',416,'NvidiaDevicePlugin.Allocate')}：memory、cores 两类资源调用返回空配置；number 对应调用通过 GetPendingPod/GetNextDeviceRequest 读取调度注解，检查设备数量对应关系，设置 NVIDIA_VISIBLE_DEVICES，注入 CUDA_DEVICE_MEMORY_LIMIT_i、CUDA_DEVICE_SM_LIMIT、共享缓存和 libvgpu.so/ld.so.preload。**这里没有 cudaMalloc、cuMemAlloc 或为请求容量作预留的逻辑。**PreStartContainer 也是空实现。

### 3.4 HAMI-core 何时约束真实使用

本节细节来自与官方 v1.11.0 子模块对应的固定提交；部署库的镜像/宿主机同哈希已核实，具体源码提交的可复现构建验证未做。

动态加载器根据 /etc/ld.so.preload 将 libvgpu.so 加入应用进程；这不是当场把限额容量全部申请出来。{src(h+'src/libvgpu.c',838,'preInit/postInit/cuInit')} 在 CUDA 初始化路径准备共享状态与利用率控制；{src(h+'src/cuda/memory.c',135,'cuMemAlloc_v2 hook')}、{src(h+'src/allocator/allocator.c',36,'oom_check')} 和 add_chunk 检查该容器共享区域的使用量与限额，然后调用原始 CUDA 驱动分配函数，并传播错误。算力控制通过 utilization_watcher/rate_limiter 等限制应用调用节奏，并非提前切出一组专属物理 CUDA 核，也不是保证每个进程始终获得申请百分比的吞吐。

因此存在两种不同的内存不足：容器超过软件配额，或配额内请求遇到实际物理内存不足。申请 8 GiB 配额后再 malloc 整整 8 GiB 缓冲区，还可能因上下文和其他开销而超限；不能拿这种失败直接证明是宿主机物理 OOM。CUDA OOM 一般表现为 CUDA API 错误/框架异常，不能与 CPU/cgroup 内存导致的 Kubernetes OOMKilled 混为一谈。

{src(h+'src/nvml/hook.c',317,'HAMI NVML memory-info hook')} 和 {src(h+'src/cuda/memory.c',491,'cuMemGetInfo_v2 hook')}（后者受编译开关影响）可以把容器看到的 total/free/used 改成配额视图。因此容器内 nvidia-smi 显示“8 GiB 总量/尚有配额”也不证明宿主机真正有这些空闲显存。必须以不加载该 hook 的宿主机或可信节点采样器为物理测量来源。

### 3.5 Pod 结束后两套资源如何释放

逻辑侧：cache.event_handlers 的 addTask 排除终态任务；UpdatePod/deleteTask → {src(s+'api/node_info.go',461,'NodeInfo.RemoveTask/subResource')} → GPUDevices.SubResource → HAMICoreFactory.SubPod，递减申请账本。{src(s+'api/devices/nvidia/vgpu/device_info.go',243,'GPUDevices.Release')} 在此版本为空实现，不能理解成调用了 cudaFree。已经 Succeeded/Failed、仍保留在 API 中的 Pod 不应继续按活跃申请扣账。

物理侧：应用 cuMemFree/上下文销毁、进程退出和驱动清理实际分配；kubelet 停止容器进程后最终释放相关资源。kubelet 自身回收分配的插件设备 ID，与 CUDA 显存释放是不同动作。API 删除时间、scheduler 去账时间、容器优雅退出时间与驱动释放时间并不严格同步；强制删除、MPS、遗留进程等更不能假设“对象消失即显存全部回收”。主机上与该 Pod 无关的进程不会被 Pod 删除释放。

## 4. 当前物理资源与逻辑申请对照

以下均来自本次真实命令输出，不使用历史 5090 快照替代。显存单位 MiB。利用率是瞬时采样：5090 初次采样四卡均为 0%，稍后卡 0/1 为 100%，显存占用基本不变，这说明“低 GPU 利用率”不等于“显存空闲”。

{table}

**星号的 0 是按当前非终态 Pod、分配注解和已核对源码重建的账本值，不能写成直接读取了 scheduler 内部的 UsedMem。**已尝试当前 scheduler Pod 的 /metrics，连接被拒绝；当前启动参数未开启 enable-metrics，现存 options 默认 false；没有现成缓存导出文件。本次没有开启 metrics、发送信号或触发缓存导出。若内部存在未反映到 API 的泄漏/短暂预留，现有只读重建不能完全排除。

当前相关活跃 GPU 请求 Pod 数为 0，八个 vgpu-test-* 为 Succeeded，仍有历史 UUID 注解。没有新的运行中 vGPU 业务 Pod 可供本次无侵入跟踪 Allocate。三台节点各卡仍以总容量和 hami-core/healthy 注册，每卡最多 10 个实例，未因为宿主机 vLLM 占用而减少注册容量。

{chr(10).join(pod_table)}

表中 UUID 来自本次 API 返回的注解，保留在已结束对象上不意味着该进程当前仍占用 GPU。插件和 monitor Pod 的全卡可见性也不能等同于申请了业务 vGPU 配额。

物理表直接采用 nvidia-smi 的 memory.free，**没有用 total−used 替代 free**。当前另读到驱动 reserved：4090 354 MiB、5090 458 MiB、L40 696 MiB；字段存在舍入与 API 口径差异，不能要求显示整数严格相加。原始 NVML v1 monitor used 与新版 nvidia-smi 的 used 对保留内存的计入口径需要核对，例如 5090 monitor 的 33,030,668,288 字节约为 31,500 MiB，而 nvidia-smi used=31,044 MiB、free=1,107 MiB；差值接近 reserved，但本次没有单独追踪所有 API/驱动字段转换，不能把差值全部精确归因。这不是一个可以不解释单位就直接相减的“额外 Pod 占用”。[NVIDIA NVML 文档](https://docs.nvidia.com/deploy/nvml-api/latest/api/group__nvmlDeviceQueries.html) 给出了 free、total 与 reserved 查询的定义，使用 v2 时还需留意 used 是否已包含 reserved，避免重复扣减。

机读资源表：{link(evidence/'summary/gpu-logical-physical.csv','每卡完整 CSV')}；完整时间、口径和重建限制：{link(evidence/'summary/audit-summary.json','audit-summary.json')}。

## 5. 5090 差异原因与进程归属

**当前仍然存在，而且可以解释：5090 的四张卡由宿主机 vLLM worker 占用了 31,032/31,170 MiB 级显存，而不是 Kubernetes vGPU Pod 的当前请求。** PID 1248740/1248741 属于 user-1007.slice/session-83.scope；545242/545243 属于 user-1007.slice/session-50.scope。它们的 cgroup 不在 kubepods 下，NSpid 没有容器内层 PID；读取映射时没有匹配到 vgpu/hami 库路径。最后一点只表示未发现这些库名，不是对所有可能绕过/重命名机制的形式证明。

L40 同样有未经过该账本的 vLLM、pegainfer、MPS 与独立 Docker 进程。仅查询 Kubernetes Pod 的 GPU limits 会漏掉这些真实占用。

{chr(10).join(process_table)}

进程列表和所有权在 GPU 查询后顺序采集，不能视为原子快照；NVML 进程显存合计与全卡 used 也可能因驱动上下文、其他图形占用及采样时差不完全一致。本次没有读取进程完整环境、口令或业务文件，也没有停止任何进程。

## 6. monitor 能看到什么，以及它为什么没有替 scheduler 挡住

官方 {src(u+'cmd/vgpu-monitor/metrics.go',139,'monitor:ClusterManagerCollector.Collect')} 调用 NVML.GetMemoryInfo/UtilizationRates，导出 HostGPUMemoryUsage 和 HostCoreUtilization。本次从真实 Pod IP 读取成功：

| 节点 | 当前 monitor 地址 | 是否获取全卡物理指标 | 是否直接导出 free gauge |
|---|---|---|---|
| t3dgq | http://10.244.0.121:9394/metrics | 是 | 本版该指标组没有 |
| t3dmg | http://10.244.2.15:9394/metrics | 是，观察到上述 5090 高显存占用 | 本版该指标组没有 |
| t3dgq-2 | http://10.244.3.3:9394/metrics | 是 | 本版该指标组没有 |

这些是集群内读取端点，不是需要公网访问的链接。monitor 与 plugin 同 Pod，但职责不同。插件 register.apiDevices 即便调用 NVML memory-info，也**只拿 Total 注册**，每约 30 秒报告设备，并没有把 Free 写入 register 容量字段。把 register 的 Memory 改成实时 Free 还会破坏原有总量−已分配的账本公式，因此不能这么简单修。

monitor 还导出了八个 Succeeded 的 vgpu-test-* 的 4 GiB memory limit，usage=0。这些共享缓存记录和仍存的 Pod 对象被 monitor 关联后可以继续产生指标；它们不是当前活跃申请，不能把 limit 全量求和当成 scheduler 已分配。部署 scheduler 的 vgpu_device_allocated_memory 指标本来也是请求账本语义，不能当作真实物理 used。三种概念必须分别命名：**承诺配额、实际显存、缓存中的容器限额**。

## 7. 已确认与待验证

| 项目 | 状态 |
|---|---|
| 5090 目前高占用、Pod 活跃 GPU 请求为零、宿主机会话 vLLM 归属 | 当前只读输出确认 |
| 插件三节点镜像摘要、HAMI 注入库与镜像内文件一致、定制 scheduler 的运行文件哈希 | 当前只读输出确认 |
| 当前部署目录 GPU 逻辑账本算法、DQN 输入来源、UUID 注解传递 | 现存部署源码确认；关键基础实现与官方逐字节一致 |
| 官方 v1.11.0 Device Plugin Allocate 不预分配整份显存；HAMI quota 检查发生在应用 API 路径 | 官方固定版本源码确认；部署镜像与库关联确认，未做二进制可复现构建 |
| 现网 scheduler 内存账本每卡确切 UsedMem/UsedCore/UsedNum | 未直接读取；当前以 API+源码重建，不能排除内部瞬时预留/错误泄漏 |
| 新 sleep Pod 申请 8 GiB 后实测物理显存差值、精确 Allocate 日志及返回配置 | **本次未运行，准备了安全实验，等待明确批准** |
| 物理显存不足时的具体 CUDA 错误、哪一个初始化步骤先失败 | 源码支持“可能失败”，没有执行 OOM 实验，不声称现网已复现 |

## 8. 待批准的最小实验（尚未提交）

草案：{link(project/'k8s/audit/vgpu-quota-no-cuda.pending-approval.yaml','vgpu-quota-no-cuda.pending-approval.yaml')}。单 Pod、1 vGPU、8192 MiB、10 核配额、4090 节点选择、只运行 sleep 300。没有 CUDA 计算、显存申请脚本、自动重试压测或任何模型训练。imagePullPolicy=Never，使用已知曾运行的镜像名称，但仍需确认现在节点缓存中存在；不存在就停止，不为实验自动拉取镜像。

执行前必须同时满足：用户已明确批准；目标节点所有可能被选中的卡当前物理 free 都高于请求并留足安全余量；配额也足够；镜像已缓存；保留观察前后时间戳。**不要在当前只剩约 1 GiB 的 5090 上执行 8 GiB 草案，更不运行试图触发 OOM 的程序。**本次 4090 表显示空闲较多，但未来执行前仍必须重新查询。

详细操作与停止条件：{link(project/'docs/vgpu-quota-no-cuda-experiment.md','无 CUDA 实验步骤')}。其中 apply/delete 命令只是交付给后续获准实验的操作说明，本次没有执行。

## 9. 对 RandomForest 研究及最小状态感知改造的影响

现有 RF 结果表示基于合成理论标签和账面状态的参考负载适配性，不是对当前物理空闲或性能的保证。本次 5090 是直接反例：账面 free≈32607 MiB，但物理 free 仅 969—1107 MiB。仅基于前者预测“可用”会把未记账占用漏掉。

建议保留 frozen 模型、特征顺序和账面资源合同；新采样另加 physical_free_memory_mib、physical_used_memory_mib、physical_reserved_memory_mib、gpu_utilization_percent、sampled_at、source、freshness，并保留 logical_free_memory_mib/logical_free_compute_quota。**不要把实测 free 偷换进原先以逻辑剩余训练的同名特征，也不要把 100−utilization 当成可预留 vGPU compute quota。**利用率是时间采样的忙闲比例，配额是承诺和上限，二者不同。

最小工程改造路线（仅设计，未实施）：

1. 扩展已有 monitor 的 Collect：直接导出 NVML free/total/reserved 及时间和 UUID；使用宿主机真实视图，不取被 hook 的容器配额视图。也可增加专用只读采样器，但当前已有 monitor 可以复用。
2. scheduler 增加按 node+UUID 的物理状态缓存，带采样时间和过期处理；不要每次节点评分启动 nvidia-smi 子进程，也不要改写注册总容量。
3. 在当前 {src(s+'api/devices/nvidia/vgpu/utils.go',573,'canFitForOrder')} 与 {src(s+'api/devices/nvidia/vgpu/utils.go',768,'checkNodeGPUSharingPredicateAndScore')} 引入额外物理显存过滤，并在 {src(s+'api/devices/nvidia/vgpu/device_info.go',261,'GPUDevices.Allocate 最终选卡')} 对最终 UUID 再校验。RF 只排列同时通过逻辑和物理过滤的候选。
4. 在现有节点锁/资源账本机制下，为尚未被采样反映的并发分配记录待启动预算，失败回滚，并确保最终写入注解的 UUID 与校验通过的 UUID 一致。插件继续消费原有注解，保持原账本格式；最小版本不必改变插件 Allocate 的配额注入语义。
5. 物理数据缺失/过期时返回明确状态；要求物理保证的请求应延后/重试，不能静默回退成“物理资源已确认足够”。推理服务故障可以回退确定性排序，但不能绕过有效物理硬约束。

在没有其他已承诺但未实际使用显存的简化情形，可使用候选条件：`request <= min(logical_free, max(0, physical_free - safety_margin - pending_unobserved_budget))`。这只是当前准入保护，不是真实物理预留。

若要同时保护所有已接受 Pod 未来兑现配额，还要考虑“已经记账但暂未用满”的承诺。拥有可靠逐 Pod 实际使用时，可保守扣减 `sum(max(0, quota_i - actual_used_i))`，其中含尚未启动任务；不知道实际使用时只能更保守或限制绕过调度的外部任务。**单次 NVML 检查、RF 得分和节点锁，都不能阻止宿主机外部程序在检查之后继续抢显存。**要获得更强保证，需要统一管理访问者/预留机制或适用硬件隔离，不应在论文里把最小过滤描述为绝对 OOM 防护。

这将研究目标明确分为两个可验证部分：资源安全准入依赖账本与物理状态；训练/推理适配性排序由 RF 提供。两者的作用、数据来源和评价指标应分开。

## 10. 本地复现与交付文件

仅重放已采证据、生成报告和运行解析测试，不连接集群、不训练模型：

```powershell
cd "{project.as_posix()}"
.venv/Scripts/python.exe scripts/summarize_vgpu_audit.py --evidence results/vgpu-mechanism-audit-20261019
.venv/Scripts/python.exe scripts/write_vgpu_mechanism_report.py --evidence results/vgpu-mechanism-audit-20261019
.venv/Scripts/python.exe -m pytest tests/test_vgpu_audit_evidence.py -q -p no:cacheprovider --basetemp=.tmp/vgpu-audit-replay
```

本次解析测试：7 passed。它验证注解解析、终态 Pod 不扣账、未解析活跃申请不伪造零值、物理 free 直接采用测量值、待批准 YAML 不绕过 scheduler/不调用 CUDA，以及 11 张卡证据覆盖。它不是运行中的 GPU/Volcano 分配验收，也没有重新训练历史模型或执行历史训练测试。

重新采集可用 {link(project/'scripts/collect_vgpu_audit.py','collect_vgpu_audit.py')} 与 {link(project/'scripts/audit_vgpu_readonly.py','audit_vgpu_readonly.py')}，复用已有受信任 host key 与环境变量凭据；默认排除 A40。supplement/linkage 中某些 PID/Pod IP 是本次已确认对象的取证定位，未来复查需重新定位，不能把旧 PID 结果当现况。SSH 及远端命令失败保存在 transport/JSONL，原始输出带 UTC 与 SHA256。索引：{link(evidence/'summary/evidence-manifest.json','证据文件哈希清单')}。
'''
    out=project/'docs/Volcano vGPU 逻辑分配与物理资源占用机制审查报告.md'
    out.write_text(text,encoding='utf-8');print(out)


if __name__=='__main__':main()
