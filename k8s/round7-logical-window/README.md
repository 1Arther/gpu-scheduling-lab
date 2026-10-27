# 第七轮调整后的逻辑资源验证草案

**当前未获部署授权，禁止直接 apply。本目录任何 YAML 都未提交集群。**

最新复核：[最小待批准操作](../../docs/round7-final-approval.md)。优先评估管理员协调的4090短时提交窗口；**不默认创建集群级准入策略**。管理员须控制所有可能使用4090的普通GPU/vGPU/DRA提交入口，并核对旧在途预留；仅API零请求或监控没有告警不能代替这项保证。物理GPU正在被使用不单独否决sleep实验。原DQN继续运行、不改二进制或配置。

如果无法可靠协调，停止独立scheduler部署，再单独审批强制准入或统一scheduler灰度；不强行并行分配。协调窗口和强制准入都是有条件的时间边界，不是多scheduler共享账本。

- `05-window-policy-DRAFT.yaml` 和 `06-window-binding-DRAFT.yaml` 只保留为**可选、另行审批**的集群准入备选，会拒绝其他新增GPU/DRA Pod；不在默认准备包内。
- 本地 `scripts/round7_render_minimal.py` 复用00–04，仅接受实际镜像digest，输出副本0的独立控制面与5个sleep用例；不调用kubectl，不包含DQN补丁。不要apply本目录或将fixture镜像哈希当真实镜像。
- 独立 Deployment 默认 `replicas: 0`，镜像仍有 build-required 占位。RF 只监听 loopback，不持 Kubernetes token，不维护资源账本。
- 初始 scope 是 `t3dgq` 的4张4090；已有固定 sleep 镜像缓存及 glibc 文件只读证据。9个 test 文件涵盖6项实验和逐卡评分采集；全部只 `/bin/sleep 600`、单容器、1个vGPU、4096MiB、20配额，不可同时全部提交。
- `expand-three-nodes/` 为4090/5090/L40共11卡的可选扩展草案：重复 node-selector 按源码作 OR、节点RBAC扩展、测试Pod使用三节点 affinity，A40不参与。5090/L40镜像缓存与启动授权尚待确认；不能直接应用此扩展。
- `A-*` 文件是**互斥备选方案**：统一 scheduler 灰度，需要升级原 scheduler 并重启其Pod；保留 dqn-job 和原插件，仅本实验标记走RF。不是本轮推荐的短时上线方式，不能和B的独立scheduler同时部署。
- `policy-cases.json` 和扩展同名文件是合成协议测试，不能视为真实 API Server 已放行/拒绝的记录。
- `execution-gate.json` 保持 `deployment_allowed: false`。RF模型、标签合同及评分权重未修改。

最新评估见 `docs/round7-logical-reassessment.md`，准确命令与回滚见 `docs/round7-logical-runbook.md`。旧 `k8s/round7/` 和原只读报告保留为历史记录；不再以“必须清空物理显存”作为本轮准入条件。
