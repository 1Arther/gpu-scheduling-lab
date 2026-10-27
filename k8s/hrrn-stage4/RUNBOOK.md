# 第四阶段：待执行集群验证手册

状态：仅完成本地原生 Go/fake-client 验证和材料准备。没有运行 kubectl、SSH、镜像推送或集群部署。以下命令是待审核执行说明。

## 部署前必须重新确认

1. 读取当前 context、Kubernetes/Volcano/设备插件版本、现有 scheduler Deployment/ConfigMap/Lease、队列及测试节点上的所有 Pod。历史 round7 配置不能作为当前集群状态。
2. 当前生产 DQN Scheduler 保持原部署、镜像、配置和副本数。只使用独立 `volcano-hrrn-lab` schedulerName、Lease、namespace 和 ServiceAccount。核实生产调度器不会为该 schedulerName 的 Pod 绑定。
3. **独立 schedulerName 不等于 GPU 账本隔离**。选取没有其他调度器写入的专用实验节点/维护窗口，确认设备注册、占用、UUID 和账本同步行为。若存在冲突任务，不启动实验。禁止仅依赖 NodeLock 声称跨调度器全局一致。
4. 将 `review-required-node` 替换为审核后的专用节点，逐条核对 RBAC 的 resourceNames、设备容量、设备配置 ConfigMap 和 Queue capability。保留 namespace 内写权限，不能扩展为任意生产 Pod 写权限。
5. Go HRRN 与 RF 必选测试必须通过；扩展回归中记录的 PVC 不稳定测试需要单独确认。API 绑定失败、真实设备插件释放、并发控制尚未由本地测试证明。不要以本地通过替代这些检查。
6. 导出实验前资源、镜像摘要、配置和账本，确定恢复负责人和测试结束时间。确认只读预检结束后，再批准短时实验启动。默认 YAML 副本数为 0。

## 镜像与配置

在项目根目录，已装 Docker 的构建环境可执行：

```powershell
./scripts/build_hrrn_native.ps1
docker build -f k8s/hrrn-stage4/Dockerfile.scheduler -t gpu-lab/volcano-hrrn:stage4-build-required .
docker build -f k8s/hrrn-stage4/Dockerfile.rf -t gpu-lab/rf-model:stage4-frozen-build-required .
```

本轮没有 Docker，也没有构建或推送镜像。应将审核后的镜像 digest 写入 Deployment；`Dockerfile.rf` 原样复用已有版本，只打包冻结模型，不训练。不要将 build-required 标签当作已存在镜像。

五组配置均为 `enqueue, allocate, backfill`。第一 tier：priority、gang、conformance；第二 tier：overcommit、drf、predicates、proportion、nodeorder、binpack、deviceshare、hrrn。A/B/C 的 RF 参数完全一致；D/E 只改变设备选择策略并关闭 RF。nodeorder/binpack 节点评分配置保持一致。切换实验组时先清空实验作业并核实释放，再停实验 scheduler，切换 ConfigMap，重启独立 scheduler。不可在有等待作业时切组后继续累计等待：进程重启会重置 ready 时间历史。

本地 fake-client 测试采用 `NodeLockEnable=false`，因为没有真实设备插件握手。集群草案统一为 true，需验证 handshake 和超时释放；这是一项明确的环境差异。

## 有限 sleep 实验

所有容器仅 `exec sleep N`，不运行 CUDA。预计时长是自定义调度输入，sleep 秒数仅为可控占用时间；二者不能作为 GPU 性能模型准确率证据。作业 Pod 设置 300 秒 activeDeadlineSeconds；另设整个实验的人工超时，取消还未启动的等待作业。

**顺序对照（A/B/C/D/E）：** 使用仅有一张可供实验分配的 GPU 的独占节点。不能在两张均空闲卡上照搬这个预期。每组使用新 Job UID 和相同输入时序：

1. 提交 `01-blocker.yaml`，确认 blocker 已绑定、逻辑核心配额占满。
2. 先提交 `02-long.yaml`，约 2 秒后提交 `03-short.yaml`。两者 initially scheduling-gated。
3. 确认各 Job 全部 Pod 已创建后，在同一个明确的控制步骤去除 long/short 的 schedulingGates；记录去除 API 完成时间及 scheduler 首次 `ready=true`。要得到公平且可复核的比较，两作业须在同一 Session 首次完整被观察；如未满足，记录偏差而不是改写时间。
4. 在 blocker 释放前保留至少 10 秒观察完整就绪。固定运行 45 秒的 blocker 给出了排队窗口，但真实 API 时序仍应以日志为准。
5. 从 PodScheduled/Binding 事件和真实容器启动时间核实顺序，不能只看 HRRN 分数。预期 A/D/E 在所有前置比较平局时选早建的 long；B/C 在等待 20 秒示例中 short=3、long=1.2，因此优先 short。

去 gate 示例（每个实际 Pod 都要执行；仅作说明，本轮未执行）：

```sh
kubectl -n hrrn-lab-tests patch pod ACTUAL_POD_NAME --type=json \
  -p='[{"op":"remove","path":"/spec/schedulingGates"}]'
```

**完整作业旁路：** 另开空白单 GPU 轮次，提交 `05-two-pods.yaml` 和 `06-small-bypass.yaml`，后者晚提交；全部就绪并等待后解除控制资源占用。大作业两个 Pod 每个请求 60 核心配额，Gang minAvailable=1、声明 total-pods=2；A/B 允许满足 min 的部分 Job，C 应回滚大作业未完成规划并绑定 small。另把两 Pod 请求各改成 40 作正对照，C 应同时规划两 Pod 后提交。记录 API 绑定仍逐 Pod 完成，不能称原子提交。

**双 GPU 完整性：** 在已批准的双 GPU 专用节点，用 `04-multi-gpu.yaml` 检查一个 Pod 请求两张卡时具有两个不同 UUID；只有一张空闲卡时不得部分满足。单 Pod 多 GPU 与多 Pod Job 是两个不同的测试。

**取消与恢复：** 删除尚未绑定的大 Job，确认后续 Job 能启动且 GPU 账本无残留；检查 scheduler 进程重启后等待历史按协议重置。不得删除生产作业。

## 采集与时间口径

```sh
kubectl -n hrrn-lab-tests get pods -o json > pods.json
kubectl -n hrrn-lab-tests get jobs.batch.volcano.sh,podgroups.scheduling.volcano.sh -o yaml > jobs-pg.yaml
kubectl -n hrrn-lab-tests get events -o json > events.json
kubectl -n hrrn-lab-system logs deploy/volcano-hrrn-lab -c scheduler --timestamps > scheduler.log
```

离线处理：

```powershell
.venv/Scripts/python.exe scripts/collect_hrrn_timing.py --pods pods.json --log scheduler.log --output timings.csv
```

脚本分别输出首次完整就绪、PodScheduled、container started/finished、逻辑释放，以及它们的时间差。整个 Job 的完整启动/完成分别取所有声明 Pod 的最后启动/完成；缺任一成员不得按完成统计。顺序判据以 Job 的所有成员为单位，另保留首 Pod 启动时间。

调度决策时间不能从排队或 sleep 推算。本地 Go 数据中的 actionsWallSeconds 是完整动作执行墙钟时间（含 RF HTTP），不是 Pod 启动时延。真实集群先用独立 trace/metrics 明确测量范围后提供 `--decisions decisions.csv`；未采集时该列为空。原生 A 没有 HRRN ready 日志，正式 A/B/C/D/E 等待对比必须统一使用外部完整成员/gate 观察记录，通过 `--readiness readiness.csv` 输入 `job,ready_at`（job 是 namespace/实际 PodGroupName；时间为 RFC3339）。不要混用 A 的外部观察时间和 B/C 的内部 Session 时间。当前采集脚本不会伪造缺失值；B/C 内部日志用于检查额外的观察延迟。

逻辑释放日志与真实设备插件释放/Node annotation 变更要双重核对；脚本仅解析日志，不能自行证明实际驱动层释放。不要把逻辑配额分配率称为物理 GPU 利用率。

## 停止与回退

结束时先停止创建实验 Job，取消实验 namespace 中剩余等待任务；等待本轮运行任务退出并核实逐 UUID 释放，再将独立 Deployment 缩回 0。A 配置即可关闭 HRRN 与完整规划；完全移除 hrrn 项也恢复原生 hooks。保留所有日志、YAML、镜像 digest 和失败数据。不要更换生产 DQN Deployment、不要修改生产配置、不要批量删除命名空间或节点资源。若 GPU 释放不一致，停止实验并按原设备插件恢复流程处理，不能通过清空账本伪造空闲。
