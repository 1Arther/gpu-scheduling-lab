"""Stable offline Pod/GPU inference API; no training or Kubernetes client."""


def classify_gpu(gpu_state):
    from .service import default_service
    return default_service().classify_gpu(gpu_state)


def rank_gpus(pod, gpu_list):
    from .service import default_service
    return default_service().rank_gpus(pod, gpu_list)
