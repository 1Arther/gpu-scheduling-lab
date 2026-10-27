| 节点/卡号 | 型号 | UUID | 物理总量 MiB | 实际已用 MiB | 实际空闲 MiB | 利用率 % | 账面已分配* MiB / 核配额 / 实例数 | 账面剩余* MiB |
|---|---|---|---:|---:|---:|---:|---|---:|
| t3dgq/0 | NVIDIA GeForce RTX 4090 | GPU-7b94f673-ce00-719b-2c94-706baa032dae | 24564 | 1 | 24211 | 0 | 0 / 0 / 0 | 24564 |
| t3dgq/1 | NVIDIA GeForce RTX 4090 | GPU-fb6aaeaf-9ca4-3c9a-7b3b-f56714c1f7cd | 24564 | 1 | 24211 | 0 | 0 / 0 / 0 | 24564 |
| t3dgq/2 | NVIDIA GeForce RTX 4090 | GPU-28cf7186-0bdd-be9f-92e5-3c59b4e92c1e | 24564 | 1 | 24211 | 0 | 0 / 0 / 0 | 24564 |
| t3dgq/3 | NVIDIA GeForce RTX 4090 | GPU-27be0024-0498-4114-e6b9-b2d0db148309 | 24564 | 1 | 24211 | 0 | 0 / 0 / 0 | 24564 |
| t3dgq-2/0 | NVIDIA L40 | GPU-d8482de5-c31d-9b24-f28b-217dc071a5d9 | 46068 | 44049 | 1325 | 0 | 0 / 0 / 0 | 46068 |
| t3dgq-2/1 | NVIDIA L40 | GPU-69fd2a04-950c-e3fd-3c78-2c2525113383 | 46068 | 42369 | 3004 | 0 | 0 / 0 / 0 | 46068 |
| t3dgq-2/2 | NVIDIA L40 | GPU-ea84e352-a3f2-bdfe-03a9-f33051f2609e | 46068 | 36601 | 8773 | 0 | 0 / 0 / 0 | 46068 |
| t3dmg/0 | NVIDIA GeForce RTX 5090 | GPU-fffbc9bd-6d21-4287-ff97-7a02b17e9ad6 | 32607 | 31044 | 1107 | 100 | 0 / 0 / 0 | 32607 |
| t3dmg/1 | NVIDIA GeForce RTX 5090 | GPU-412f170c-a414-091e-1203-28751d20a1f3 | 32607 | 31044 | 1107 | 100 | 0 / 0 / 0 | 32607 |
| t3dmg/2 | NVIDIA GeForce RTX 5090 | GPU-d078c4f4-f0ac-6469-7960-0beee158ca7a | 32607 | 31182 | 969 | 0 | 0 / 0 / 0 | 32607 |
| t3dmg/3 | NVIDIA GeForce RTX 5090 | GPU-9ba09ba0-9b12-8014-4c2e-fd48889ccc9f | 32607 | 31182 | 969 | 0 | 0 / 0 / 0 | 32607 |

*由当前非终态 Pod 注解及当前源码重建；未直接读取 scheduler 内部账本。
