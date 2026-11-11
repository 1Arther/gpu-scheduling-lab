# HRRN 当前范围与复现

2026-11-11 源码更新。基础 HRRN JobOrder 已实现；Priority/Gang/DRF 与原生资源约束保持。基础 A/B 的区别只有 HRRN enabled；complete-job、coordinated-complete-job 和 reservation-barrier 均为 false。独立配置在 `k8s/hrrn-basic-20261111/reviewed-v2/`，Deployment 副本数为零，尚需当前集群 RBAC、设备 ConfigMap、镜像和恢复路径审查，不能整体 apply。

原生 allocate 的可行性旁路是既有能力，不作为新贡献。基础单 Pod 本地 Go 对照确认原生先绑定 long、HRRN 先绑定 short；合成时钟等待 20 秒、分值 1.2/3，只代表本地 fake-client/binder 验证。当前真实环境预检条件不满足，本轮没有新的真实 A/B 绑定和三方 UUID 结论。

阶段九保留为后续工程：同账本持久预约和 nonce、官方 Device Plugin protobuf、本地 gRPC 已测试，但真实 HAMi/kubelet、可信节点确认与 fencing 尚未打通，多 Pod 完整启动没有证明。详见阶段六至九说明。停止继续增加协议，优先完成基础单 Pod 的受控真实验证；不训练 PPO/DQN/GNN，不改冻结 RF。

本地测试记录：阶段九相关 Linux Go 测试 174 顶层/692 叶场景；本轮基础配置与授权白名单 Python 测试 13 项，Go 定向复测 4 顶层/10 叶场景。以上不等于全 Volcano 测试全绿，历史 PVC 不稳定个例仍单独保留。

```bash
python -m pytest -q tests/test_hrrn_basic_prepare.py tests/test_hrrn_worker_readonly.py
```

Go 需要 Linux 或项目约定 WSL 环境；阶段九完整复现脚本为 `scripts/test_hrrn_stage9.ps1`，需要冻结 RF 本地服务和历史 checkpoint 夹具。原始服务器采集、可执行文件、OCI 和工具缓存未随本次源码更新上传，引用本地证据的测试需要单独准备这些输入；不能伪造输入替代。
