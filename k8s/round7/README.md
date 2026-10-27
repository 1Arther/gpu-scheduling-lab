# 第七轮审查草案：当前 NO-GO

**不要 apply 本目录。当前只授权只读检查和文件准备，设备隔离未满足。**

- 4090/5090/L40的11张GPU均有进程使用；现有DQN scheduler未配置排他节点筛选。
- Namespace、schedulerName、Lease、NodeLock、标签/taint和RF侧白名单都不能单独解决跨scheduler、普通GPU/vGPU和宿主机进程的重叠预留。
- Deployment默认replicas=0；节点、镜像和节点RBAC仍含占位值。`isolation-gate.json`保持deployment_allowed=false。
- `03-configmaps.yaml`含on、仅关闭学习项、原生Binpack、服务异常四个配置。独立sidecar复用冻结模型，不训练。
- `test-*.yaml`均单容器单vGPU，训练/推理Label显式给定，只执行/bin/sleep 600，1实例、4096MiB、20算力配额。不要一次提交全部用例。
- 已确认4090缓存的固定digest镜像中有glibc版sleep；这不等于GPU计算兼容性或CUDA验证，其他节点尚无该镜像缓存证明。

先阅读项目 `docs/round7-preflight-report.md` 和 `docs/round7-execution-runbook.md`。完成独占设备、镜像构建与最终YAML审查后，仍须用户另行批准阶段二。当前没有集群对象需要回滚。
