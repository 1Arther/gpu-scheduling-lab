# 无 CUDA 的 vGPU 配额观察实验（待明确批准）

本文件是操作草案，当前没有执行 apply/delete。不能用此文件代替用户的明确批准。禁止以物理显存不足为条件故意测试 OOM。

目标：观察“申请 8192 MiB 配额、仅运行 sleep”的账本变化和宿主机物理显存变化。它不会验证 CUDA 实际可分配 8 GiB，也不能证明 HAMI 限额在所有 CUDA API 上可靠。

在管理节点准备本地 YAML 副本与输出目录。所有观察前后保留 UTC。以下函数仅简化后续手工命令：

```bash
k() { sudo -n kubectl --kubeconfig=/etc/kubernetes/admin.conf "$@"; }
date -u
k get pods -A -o json > pods-before.json
k get nodes -o json > nodes-before.json
k get cm -n volcano-system volcano-scheduler-configmap -o yaml > scheduler-config-before.yaml
```

先在 t3dgq 宿主机只读查询（不在 HAMI 容器内运行）：

```bash
nvidia-smi --query-gpu=index,uuid,name,memory.total,memory.used,memory.free,memory.reserved,utilization.gpu --format=csv,nounits
nvidia-smi --query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory --format=csv,nounits
sudo -n crictl images
```

确认所有可能被 scheduler 选中的 4090 均有足够物理 free：8192 MiB 加一段事先记录的安全余量（例如 2048 MiB，这是实验保守参数，不是显卡能力标签），账本容量和实例数也足够。否则停止。确认 YAML 中镜像已缓存，`imagePullPolicy: Never` 不会拉取缺失镜像。实际业务资源可能随时变化，采样与提交间应尽量缩短；此实验不承诺资源独占。

仅在用户明确批准后，执行一次：

```bash
k apply -f vgpu-quota-no-cuda.pending-approval.yaml
k get pod vgpu-quota-no-cuda-audit -o wide
k get pod vgpu-quota-no-cuda-audit -o json > pod-after.json
k get pods -A -o json > pods-after.json
k get events --field-selector involvedObject.name=vgpu-quota-no-cuda-audit --sort-by=.metadata.creationTimestamp
```

使用 `spec.nodeSelector` 而非 `spec.nodeName`，确保仍经 Volcano 选卡。读取注解而不是根据 GPU 序号猜 UUID：

```bash
k get pod vgpu-quota-no-cuda-audit -o jsonpath='{.metadata.annotations.volcano\.sh/vgpu-ids-new}{"\n"}{.metadata.annotations.volcano\.sh/devices-to-allocate}{"\n"}{.metadata.annotations.volcano\.sh/bind-phase}{"\n"}'
k logs -n kube-system -l name=volcano-device-plugin -c volcano-device-plugin --tail=300 --prefix=true > plugin-after.log
```

在日志中按该 Pod 名/UID、`deviceAllocateFromAnnotation`、`Allocate Response`、`TrySuccess` 查找。插件日志过少或 kubelet复用缓存时，不能仅凭没有日志断言 Allocate 没有发生；可只读查看该容器的 CRI 配置，核实 `NVIDIA_VISIBLE_DEVICES`、`CUDA_DEVICE_MEMORY_LIMIT_0=8192m`、`CUDA_DEVICE_SM_LIMIT=10` 和库挂载。只保存这些安全字段，不保存完整环境或 ServiceAccount 信息。

随后在宿主机重复 nvidia-smi 采样，不要向容器执行 CUDA 或使用容器 nvidia-smi 当物理真值。对比目标 UUID：

- 非终态 Pod 分配注解应记录 8192 MiB/10 quota/1 实例；从当时所有非终态 Pod 重建。Node Allocatable 通常保持总容量，不能靠它是否减少判断记账。
- 实际物理 used 不应仅因为 YAML 中写了 8192 就增加整整 8192 MiB。可能有少量驱动/上下文或其他用户任务变化，需结合进程数据解释。
- `devices-to-allocate` 被消费后可变空，而 `vgpu-ids-new` 保留最终分配记录；不要将空待消费注解误解为没有选 GPU。
- monitor 中 memory limit 不等于实际 memory usage；终态 Pod 的缓存指标可能残留，不计入当前账本。

sleep 约五分钟后结束，再采样 Pod.phase 与主机显存。若实验授权包含清理，最后只删除本测试 Pod：

```bash
k delete pod vgpu-quota-no-cuda-audit --wait=true
k get pods -A -o json > pods-final.json
```

观察结束和删除前后账本恢复；主机其他任务的显存不应因本 Pod 删除而消失。遇到 Pending、镜像缺失、注解失败、健康异常或物理资源变化就记录并停止，不扩大请求、不重复压测、不运行显存申请程序。
