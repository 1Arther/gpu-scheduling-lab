# NVIDIA 官方规格来源审计

15款独立型号/配置。沿用第三轮12条NVIDIA记录，新增3款；A40的FP32等参数由本轮官方PDF补齐，原数据文件不修改。

7项静态字段全部保留。共同模型有数值信息的静态列是显存容量、带宽、非Tensor FP32及功耗；3个Tensor低精度列在本轮统一屏蔽为null，以免不一致的累加精度、稀疏口径和缺失模式成为架构/型号的暗示。中位数填补+缺失指示仅在训练集拟合；全空列0是占位，不代表硬件性能为0。

FP32非Tensor CUDA Core峰值与TF32/Tensor Core峰值分开；带宽gbps字段仍按原项目约定使用十进制GB/s。nominal GB换算为MiB仅为保守离线容量假设。FP16/BF16注明累加精度；不从不明Tensor总值推导。

| 型号/配置 | 架构 | 关键参数完整 | 来源 |
|---|---|---|---|
| GeForce RTX 3090 | Ampere | True | [NVIDIA](https://images.nvidia.com/aem-dam/Solutions/geforce/blackwell/nvidia-rtx-blackwell-gpu-architecture.pdf) |
| GeForce RTX 4090 | Ada Lovelace | True | [NVIDIA](https://images.nvidia.com/aem-dam/Solutions/geforce/blackwell/nvidia-rtx-blackwell-gpu-architecture.pdf) |
| GeForce RTX 5090 | Blackwell | True | [NVIDIA](https://images.nvidia.com/aem-dam/Solutions/geforce/blackwell/nvidia-rtx-blackwell-gpu-architecture.pdf) |
| L40 | Ada Lovelace | True | [NVIDIA](https://images.nvidia.com/content/Solutions/data-center/vgpu-L40-datasheet.pdf) |
| L40S | Ada Lovelace | True | [NVIDIA](https://www.nvidia.com/en-us/data-center/l40s/) |
| L4 | Ada Lovelace | True | [NVIDIA](https://www.nvidia.com/en-us/data-center/l4/) |
| A10 | Ampere | True | [NVIDIA](https://www.nvidia.com/en-us/data-center/products/a10-gpu/) |
| A30 | Ampere | True | [NVIDIA](https://www.nvidia.com/en-us/data-center/products/a30-gpu/) |
| A40 | Ampere | True | [NVIDIA](https://images.nvidia.com/content/Solutions/data-center/a40/nvidia-a40-datasheet.pdf) |
| A100 80GB SXM standard 400W | Ampere | True | [NVIDIA](https://www.nvidia.com/en-us/data-center/a100/) |
| H100 SXM | Hopper | True | [NVIDIA](https://www.nvidia.com/en-us/data-center/h100/) |
| H200 SXM | Hopper | True | [NVIDIA](https://www.nvidia.com/en-us/data-center/h200/) |
| RTX A6000 48GB PCIe | Ampere | True | [NVIDIA](https://www.nvidia.com/content/dam/en-zz/Solutions/products/workstations/nvidia-rtx-a6000-datasheet.pdf) |
| RTX 6000 Ada 48GB PCIe | Ada Lovelace | True | [NVIDIA](https://www.nvidia.com/content/dam/en-zz/Solutions/design-visualization/rtx-6000/proviz-print-rtx6000-datasheet-web-2504660.pdf) |
| GeForce RTX 5080 16GB reference | Blackwell | True | [NVIDIA](https://images.nvidia.com/aem-dam/Solutions/geforce/blackwell/nvidia-rtx-blackwell-gpu-architecture.pdf) |

A100仅80GB SXM标准400W；H100仅80GB SXM，H200仅141GB SXM，未合并PCIe/SXM配置。RTX A6000与RTX6000 Ada是两个型号。RTX5080使用官方reference规格。B200未完成配置特定的完整审计，本轮不凑数纳入。

RTX A6000的309.7 Tensor数字带稀疏注脚，RTX6000 Ada的1457是稀疏FP8，都没有冒充dense FP16。A40: FP16为FP16累加，BF16为FP32累加；数据库保存149.7的非稀疏值，但统一模型中屏蔽。

来源为人工核实的官方文档URL、定位、单位及口径，非真实性能测量，也非下载的不可变网页快照。所有性能标签仍为unknown；监督实验另标synthetic。
