# 第三轮公开硬件来源审计

规格事实与性能标签分开保存。下列记录不是 22 张实测 GPU，也不是 22 款全部参数齐全的设备。

共 22 条设备记录；16 条具备共同 FP32 理论模型所需的容量、带宽和向量 FP32。真实训练/推理性能标签为 0。

`hardware-catalog.csv` 是汇总；`parameter-sources.csv` 和 JSON 按参数保存原值、原单位、归一值、来源、定位、口径及屏蔽原因。带宽字段 gbps 按本项目约定实际单位为 GB/s，不是 Gbit/s。

| 型号 | 厂商 | 理论原型输入齐全 | 缺失规格 | 来源 |
|---|---|---|---|---|
| GeForce RTX 3090 | NVIDIA | True | 无 | [官方](https://images.nvidia.com/aem-dam/Solutions/geforce/blackwell/nvidia-rtx-blackwell-gpu-architecture.pdf) |
| GeForce RTX 4090 | NVIDIA | True | 无 | [官方](https://images.nvidia.com/aem-dam/Solutions/geforce/blackwell/nvidia-rtx-blackwell-gpu-architecture.pdf) |
| GeForce RTX 5090 | NVIDIA | True | 无 | [官方](https://images.nvidia.com/aem-dam/Solutions/geforce/blackwell/nvidia-rtx-blackwell-gpu-architecture.pdf) |
| L40 | NVIDIA | True | 无 | [官方](https://images.nvidia.com/content/Solutions/data-center/vgpu-L40-datasheet.pdf) |
| L40S | NVIDIA | True | 无 | [官方](https://www.nvidia.com/en-us/data-center/l40s/) |
| L4 | NVIDIA | True | fp16_dense_tflops, bf16_dense_tflops, int8_dense_tops | [官方](https://www.nvidia.com/en-us/data-center/l4/) |
| A10 | NVIDIA | True | 无 | [官方](https://www.nvidia.com/en-us/data-center/products/a10-gpu/) |
| A30 | NVIDIA | True | 无 | [官方](https://www.nvidia.com/en-us/data-center/products/a30-gpu/) |
| A40 | NVIDIA | False | fp32_tflops, fp16_dense_tflops, bf16_dense_tflops, int8_dense_tops | [官方](https://www.nvidia.com/en-us/data-center/a40/) |
| A100 80GB SXM standard 400W | NVIDIA | True | 无 | [官方](https://www.nvidia.com/en-us/data-center/a100/) |
| H100 SXM | NVIDIA | True | fp16_dense_tflops, bf16_dense_tflops, int8_dense_tops | [官方](https://www.nvidia.com/en-us/data-center/h100/) |
| H200 SXM | NVIDIA | True | fp16_dense_tflops, bf16_dense_tflops, int8_dense_tops | [官方](https://www.nvidia.com/en-us/data-center/h200/) |
| Instinct MI100 | AMD | True | 无 | [官方](https://www.amd.com/en/products/accelerators/instinct/mi100.html) |
| Instinct MI210 | AMD | True | 无 | [官方](https://www.amd.com/content/dam/amd/en/documents/partner-hub/instinct/5-reasons-amd-instinct-mi210-research-innovation.pdf) / [官方](https://www.amd.com/en/products/accelerators/instinct/mi200/mi210.html) |
| Instinct MI250X | AMD | True | 无 | [官方](https://www.amd.com/en/products/accelerators/instinct/mi200/mi250x.html) |
| Instinct MI300X | AMD | True | 无 | [官方](https://www.amd.com/en/products/accelerators/instinct/mi300/mi300x.html) |
| Data Center GPU Max 1550 | Intel | False | fp32_tflops, fp16_dense_tflops, bf16_dense_tflops, int8_dense_tops | [官方](https://www.intel.com/content/www/us/en/products/sku/232873/intel-data-center-gpu-max-1550/specifications.html) |
| Data Center GPU Max 1100 | Intel | False | memory_bandwidth_gbps, fp32_tflops, fp16_dense_tflops, bf16_dense_tflops, int8_dense_tops | [官方](https://www.intel.com/content/dam/www/central-libraries/us/en/documents/2023-01/data-center-gpu-max-series-product-brief.pdf) |
| MTT S4000 | Moore Threads | True | fp16_dense_tflops, bf16_dense_tflops, int8_dense_tops | [官方](https://docs.mthreads.com/s4000/s4000-doc-online/introduction/) |
| MLU370-X8 | Cambricon | False | fp16_dense_tflops, bf16_dense_tflops, int8_dense_tops | [官方](https://www.cambricon.com/index.php?m=content&c=index&a=lists&catid=406) |
| Ascend 910 processor (2019) | Huawei | False | memory_capacity_mib, memory_bandwidth_gbps, fp32_tflops, fp16_dense_tflops, bf16_dense_tflops, int8_dense_tops, max_board_power_w | [官方](https://solar.huawei.com/~/media/Solar/company/AnnualReport2019_en.pdf) |
| Bili 166C | Biren | False | memory_capacity_mib, memory_bandwidth_gbps, fp32_tflops, fp16_dense_tflops, bf16_dense_tflops, int8_dense_tops | [官方](https://sgc-prod.birentech.com/product/hardware/166c/) |

## 审计边界

- FP32 vector/general peaks only; AMD matrix FP32 excluded
- all nominal capacity GB -> conservative decimal MiB model, not live capacity
- unqualified sparse figures retained only as rejected observations
- unaligned low-precision accumulator/engine figures masked before RF
- MI250X multi-die package hypothetical pooled resources ignore locality
- no NPU unified supervision/execution model
- Huawei large-PDF evidence limited to official indexed excerpt; all seven numeric fields remain null

公开 dense 数值可以保存在数据库中，但不等于各家累加精度、运算单元、编译器和工作负载表现可直接比较。本轮统一标签只使用 FP32 参考合同；不将 FP16/INT8 峰值或厂商定位视为训练/推理真值。

未下载完整网页快照。来源访问日期和定位保存在 source-records.json；网页未来变化时需要重新人工审计。
