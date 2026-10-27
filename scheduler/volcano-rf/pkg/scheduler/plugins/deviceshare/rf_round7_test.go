package deviceshare

import (
	v1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"testing"

	"volcano.sh/volcano/pkg/scheduler/api/devices/nvidia/vgpu"
	"volcano.sh/volcano/pkg/scheduler/framework"
)

func TestRFPredictionArgumentDefaultAndOverride(t *testing.T) {
	oldRF, oldPrediction, oldVGPU, oldPolicy := vgpu.RFEnabled, vgpu.RFPredictionEnabled, vgpu.VGPUEnable, vgpu.GPUSelectPolicy
	t.Cleanup(func() {
		vgpu.RFEnabled, vgpu.RFPredictionEnabled, vgpu.VGPUEnable, vgpu.GPUSelectPolicy = oldRF, oldPrediction, oldVGPU, oldPolicy
	})
	// VGPU disabled avoids any cluster config initialization in this unit test.
	New(framework.Arguments{VGPUEnable: false, RFPredictionArgument: false})
	if vgpu.RFPredictionEnabled || vgpu.RFActive() {
		t.Fatal("explicit RF-term disable not honored")
	}
	New(framework.Arguments{VGPUEnable: false})
	if !vgpu.RFPredictionEnabled || vgpu.RFEnabled {
		t.Fatal("default changed")
	}
}

func TestRFCanaryConfigKeepsLegacyWeightAndPolicy(t *testing.T) {
	oldRF, oldCanary, oldVGPU, oldPolicy, oldNS := vgpu.RFEnabled, vgpu.RFCanaryEnabled, vgpu.VGPUEnable, vgpu.GPUSelectPolicy, vgpu.RFCanaryNamespace
	t.Cleanup(func() {
		vgpu.RFEnabled, vgpu.RFCanaryEnabled, vgpu.VGPUEnable, vgpu.GPUSelectPolicy, vgpu.RFCanaryNamespace = oldRF, oldCanary, oldVGPU, oldPolicy, oldNS
	})
	dp := New(framework.Arguments{VGPUEnable: false, RFEnableArgument: true, RFCanaryArgument: true, GPUSelectPolicyArgument: vgpu.DQNJobPolicy}).(*deviceSharePlugin)
	vgpu.VGPUEnable = true // avoid cluster config initialization; policy logic only
	p := &v1.Pod{ObjectMeta: metav1.ObjectMeta{Namespace: "gpu-rf-tests", Labels: map[string]string{"gpu-lab/experiment": "round7-rf", vgpu.RFWorkloadLabel: "training"}}}
	if !vgpu.RFForPod(p) || vgpu.GPUSelectPolicy != vgpu.DQNJobPolicy || dp.weightForPod(p) != 1 {
		t.Fatal("canary not isolated from configured legacy policy")
	}
	p.Namespace = "default"
	if vgpu.RFForPod(p) || dp.weightForPod(p) != 0 {
		t.Fatal("legacy weight changed")
	}
}
