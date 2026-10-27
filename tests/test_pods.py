import pytest

from gpu_lab.pods import TASK_LABEL, load_pod_type, task_type_from_pod
from gpu_lab.profiles import ROOT


@pytest.mark.parametrize("task_type", ["training", "inference"])
def test_explicit_pod_label(task_type):
    pod = {"kind": "Pod", "metadata": {"labels": {TASK_LABEL: task_type}}}
    assert task_type_from_pod(pod) == task_type
    assert load_pod_type(ROOT / f"examples/pod-label-{task_type}.yaml") == task_type


@pytest.mark.parametrize("labels", [{}, {TASK_LABEL: "auto"}, {TASK_LABEL: "Training"}])
def test_no_automatic_task_type_inference(labels):
    pod = {"kind": "Pod", "metadata": {"labels": labels}, "spec": {"command": ["train.py"]}}
    with pytest.raises(ValueError, match="explicitly"):
        task_type_from_pod(pod)
