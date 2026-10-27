# HRRN 元数据协议 v1（项目自定义）

适用有限、固定成员的 Volcano batch Job。以下不是 Kubernetes/Volcano 原生时长字段。

| 位置 | 字段 | 含义 |
|---|---|---|
| Job.metadata.annotations | gpu-lab/estimated-duration-seconds | 正有限浮点秒；调度服务时长估计，不读取实际未来结束时间 |
| Job.metadata.annotations | gpu-lab/total-pods | 所有 task replicas 的总和，正整数且不小于 minAvailable |
| PodGroup.metadata.annotations | 同上两个字段 | 调度插件实际读取位置 |
| Pod template.metadata.labels | gpu-lab/workload-type | 既有 RF 训练/推理类型输入；本轮不改变语义 |

真实数据流为 job controller 的 createOrUpdatePodGroup 在创建 PodGroup 时复制 Job.Annotations；不是从 Pod template 读取预计服务时长。已有 PodGroup 更新分支主要同步 priority/minMember/minResources/minTaskMember，不自动同步任意 annotation。实验应在创建 Job 前填好元数据并固定；如需更新已存在 Job 的估计，必须显式更新对应 PodGroup，并记录变更，下一 Session 才采用。固定对照实验禁止中途改变估计。扩缩容/成员重试不在本轮实验协议内。

调度器观察到全部声明 Pod（精确数量、UID 集合）、没有删除/Failed 成员、没有 schedulingGate 时，记录首次完整就绪 ready_at。这里是可进入调度的成员就绪，不是容器 readinessProbe，也不是资源已经能放下，更不是 Pod 创建时间。过早提交部分 Pod 不累计其余成员未齐的等待。缺失/删除/gated/UID 替换会重置历史；消失 Job 在下一 Session 清理，重新创建的 PodGroup UID 不继承旧等待。

ready_at 保存在调度器进程内并跨 Session；没有注入用户可伪造的等待时间字段。进程重启/leader 切换后保守重新观察计时。这会低估重启前等待，**不具备持久等待历史恢复**。控制面观察存在 Session/缓存延迟，不能宣称精确等于最后一个 Pod 的 API 创建时间。

每 Session 仅读一次时钟，冻结 score、估计、ready_at；等分以 ready_at、Volcano JobInfo.UID（namespace/PodGroupName，Session 内唯一）稳定比较。HRRN = 1 + max(0, now-ready_at) / max(estimate, tau)。默认 tau=1 秒；估计缺失、0、负数、NaN、Inf、解析错误均回退 60 秒并记录不同原因。非法 tau/default-duration 参数回退 1/60。

插件默认 enabled=false。enabled=true 时也只在同 Queue、同 Priority 且 priority/gang/drf 先行比较为平局后生效。配置顺序错误自动禁用并输出错误。complete-job=false 保留原生 Gang/minAvailable 提交条件；complete-job=true 要求 metadata 完整、非 BestEffort 的全部声明成员准备好，且全体剩余成员能通过原生分配规划。为避免 tier 的早期 Permit 留下部分规划，完整模式对 JobPipelined 使用仅能拒绝的硬 veto。

完整模式只改变调度提交条件和失败清理，不修改冻结 RF 评分、分类或 native AddPod/SubPod 账本。原生节点/GPU 规划是贪心路径，并非枚举所有组合的完备求解器。Statement.Commit 逐 Pod 发往绑定队列，多 Pod 的 API 失败仍可能部分绑定。设备分配回调在 Commit 前已 patch UUID，失败时额外清理未绑定 Pod 的元数据；读取/更新冲突会记录错误，未实现跨 API 的分布式事务。当前不支持 BestEffort 经 backfill 绕过完整检查；无 CPU/内存请求的请求应明确配置而不是默默部分提交。

该实现没有容量保护、EASY 时间预约、抢占动作或强化学习模型。它不能据此保证全局平均等待降低，也不能消除负载过高时的长等待。
