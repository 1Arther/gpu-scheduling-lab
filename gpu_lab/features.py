from .resources import GPUState

FEATURE_NAMES = (
    "memory_gb", "bandwidth_gb_s", "fp32_tflops", "cuda_cores", "tdp_w",
    "remaining_memory_mib", "remaining_compute_units", "allocated_instances",
    "training_instances", "inference_instances", "max_instances",
)


def state_features(state: GPUState) -> dict:
    p = state.profile
    return dict(zip(FEATURE_NAMES, (
        p.memory_gb, p.bandwidth_gb_s, p.fp32_tflops,
        p.cuda_cores, p.tdp_w,
        state.remaining_memory_mib, state.remaining_compute_units,
        state.allocated_instances, state.instances_of_type("training"),
        state.instances_of_type("inference"), state.max_instances,
    )))


def feature_values(features):
    return [features[name] for name in FEATURE_NAMES]
