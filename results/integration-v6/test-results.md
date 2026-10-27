# 第六轮离线联调实测记录

本文件仅从实际测试记录和原生 Allocate 输出生成。没有部署集群。

Python：{'tests': 167, 'failures': 0, 'errors': 0, 'skipped': 0}；Go 含子测试通过 60，失败 0，跳过 0。

## 原生最终 UUID

|阶段|任务|推荐 UUID|Allocate 注解 UUID|分数|
|---|---|---|---|---|
|A-training|training|SIM-sim-5090-GPU-00|SIM-sim-5090-GPU-00|70.3983|
|A-inference|inference|SIM-sim-5090-GPU-00|SIM-sim-5090-GPU-00|70.3983|
|B-allocate-A|training|SIM-sim-5090-GPU-00|SIM-sim-5090-GPU-00|80.0419|
|B-allocate-B|inference|SIM-sim-5090-GPU-01|SIM-sim-5090-GPU-01|70.4952|
|B-after-release-A|training|SIM-sim-5090-GPU-00|SIM-sim-5090-GPU-00|70.3983|
|B-single-compute|training|SIM-sim-single-l40-GPU-00|SIM-sim-single-l40-GPU-00|68.2968|
|B-single-memory|inference|SIM-sim-single-l40-GPU-00|SIM-sim-single-l40-GPU-00|30.6423|

## L40 逐步分类

|阶段|剩余 MiB|剩余配额|实例|类别|状态|
|---|---|---|---|---|---|
|initial|46068|100|0|balanced|provisional|
|after-compute|42068|35|1|training_preferred|provisional|
|after-memory|8068|15|2|unavailable|provisional|
|release-compute|12068|80|1|balanced|provisional|
|release-all|46068|100|0|balanced|provisional|

## Go 包

|包|结果|秒|
|---|---|---|
|volcano.sh/volcano/pkg/scheduler/api/devices/nvidia/vgpu|pass|2.418|

冻结文件全部通过原 manifest SHA256 检查：True
；原 DQN 客户端/生成代码全部未改：True。

Windows 当前没有可用的 C 编译器；未运行 Go -race。普通多 Pod 并发与 stale/retry 单测已执行，不等同于线上多调度器竞态证明。

详细原始记录：native-experiments.json、go-tests.jsonl、go-test-cases.csv、pytest-all.xml。
