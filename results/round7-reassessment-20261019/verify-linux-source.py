from pathlib import Path
import hashlib,json,datetime
root=Path('/home/zbs/rf-round7-reassess-20261019')
expected={'scheduler/volcano-rf/pkg/scheduler/api/devices/nvidia/vgpu/device_info.go': '5b11dc1ee0ec27150471377cd1ed83453761c654783da27675e6c666d91a648d', 'scheduler/volcano-rf/pkg/scheduler/api/devices/nvidia/vgpu/utils.go': 'afd778d60a1c414dd6004707080b9dab6a1fb799c9b079cdbcf19f6396021dd4', 'scheduler/volcano-rf/pkg/scheduler/api/devices/nvidia/vgpu/rf_job.go': 'a1e818b40ea46c94bca90008ac8b2e64c8bcfa3e8d27d3a9a4659c4dd5614b23', 'scheduler/volcano-rf/pkg/scheduler/api/devices/nvidia/vgpu/rf_canary.go': '5e3468d946db7cbce7b03565a460ff6ef6d243bb0d03ccfc8f527a85919b23db', 'scheduler/volcano-rf/pkg/scheduler/api/devices/nvidia/vgpu/rf_canary_test.go': 'ddad98bae55b8354739a077c98a8530cbbed73766a7e6c2966ab489faefebbda', 'scheduler/volcano-rf/pkg/scheduler/api/devices/nvidia/vgpu/rf_window_policy_test.go': 'ec9172b3b78c7a78357525242b5f29de78131c619d24105561ec6469010425b7', 'scheduler/volcano-rf/pkg/scheduler/plugins/deviceshare/deviceshare.go': 'ce4978d9f6b675bee4b599e8bd7274eef637e86669ccab453597a994b9442edd', 'scheduler/volcano-rf/pkg/scheduler/plugins/deviceshare/rf_round7_test.go': '3203a74a7e5c3f5a45ca8ed09639d32e2c74a5ee470159af29ef39b9c93e48fe'}
actual={p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in expected}
record={'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'source_sha256':actual,'matches_final_local_source':actual==expected,
 'compiled_binary_sha256':hashlib.sha256((root/'results/reassess-release-cpu/vc-scheduler').read_bytes()).hexdigest(),
 'frozen_model_sha256':hashlib.sha256((root/'models/pod-gpu-rf-v1/random_forest.joblib').read_bytes()).hexdigest(),
 'online_old_binary_sha256_read_only':hashlib.sha256(Path('/home/zbs/volcano-v1.13.1-dqn/_output/bin/vc-scheduler').read_bytes()).hexdigest(),
 'cluster_resources_changed':False}
assert record['matches_final_local_source']
(root/'results/reassess-release-cpu/source-verification.json').write_text(json.dumps(record,indent=2));print(json.dumps(record,indent=2))
