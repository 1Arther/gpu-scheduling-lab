// Copyright 2026 The Volcano Authors. Apache-2.0.
package vgpu

import (
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"reflect"
	"testing"

	v1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
)

func plannerSetup(t *testing.T) {
	t.Helper()
	rfTestConfig(t)
	// If planning accidentally calls the native lock API, the absent client
	// will fail this test. No Kubernetes client or scheduler is installed.
	NodeLockEnable = true
	rfMockServer(t, rfMockResponse)
}

func plannerBackground(node *GPUDevices, name string, memory, cores int64) *v1.Pod {
	pod := rfTestPod(name, "training", memory, cores)
	pod.Annotations = map[string]string{AssignedIDsAnnotations: encodePodDevices([]ContainerDevices{{
		{UUID: node.Device[0].UUID, Type: NvidiaGPUDevice, Usedmem: uint(memory), Usedcores: uint(cores)},
	}})}
	node.AddResource(pod)
	return pod
}

func TestVGPUJobPlannerTemporarilyUnavailableAndComplete(t *testing.T) {
	plannerSetup(t)
	node := rfTestNode("node", "NVIDIA-L40", 1, 24564)
	background := plannerBackground(node, "background", 4096, 30)
	large := []*v1.Pod{rfTestPod("large-0", "training", 4096, 40), rfTestPod("large-1", "training", 4096, 40)}
	nodes := map[string]*GPUDevices{"node": node}
	before := vgpuJobSourceToken(node)
	beforePods := []*v1.Pod{large[0].DeepCopy(), large[1].DeepCopy()}
	plan, err := PlanCompleteVGPUJob(nodes, large)
	if plan != nil || !errors.Is(err, ErrVGPUPlanNoCandidate) {
		t.Fatalf("incomplete two-Pod plan must be rejected: plan=%+v err=%v", plan, err)
	}
	if vgpuJobSourceToken(node) != before || !reflect.DeepEqual(beforePods, large) {
		t.Fatal("failed plan changed production ledger or caller-owned Pods")
	}
	if node.Device[0].UsedCore != 30 || node.Device[0].UsedMem != 4096 || node.Device[0].UsedNum != 1 {
		t.Fatal("failed plan changed background allocation")
	}
	// The 60-core candidate remains feasible because the rejected Job's
	// scratch usage was never committed. This proves planner isolation only;
	// real queue bypass and Kubernetes binding are tested separately.
	small := rfTestPod("small", "inference", 4096, 60)
	smallPlan, err := PlanCompleteVGPUJob(nodes, []*v1.Pod{small})
	if err != nil || len(smallPlan.Members) != 1 || smallPlan.Members[0].Devices[0][0].UUID != node.Device[0].UUID {
		t.Fatalf("60-core planning after failed large Job: plan=%+v err=%v", smallPlan, err)
	}
	if vgpuJobSourceToken(node) != before {
		t.Fatal("successful proposal modified production ledger")
	}
	// Simulate a genuine native release before the next local plan.
	node.SubResource(background)
	plan, err = PlanCompleteVGPUJob(nodes, large)
	if err != nil || len(plan.Members) != 2 {
		t.Fatalf("full two-Pod plan should fit after release: plan=%+v err=%v", plan, err)
	}
	for i, member := range plan.Members {
		if member.PodUID != large[i].UID || member.Node != "node" || len(member.Devices) != 1 ||
			len(member.Devices[0]) != 1 || member.Devices[0][0].UUID != node.Device[0].UUID ||
			member.Devices[0][0].Usedmem != 4096 || member.Devices[0][0].Usedcores != 40 {
			t.Fatalf("wrong full-plan UUID/quota: %+v", member)
		}
	}
	if plan.Members[0].SnapshotToken == plan.Members[1].SnapshotToken {
		t.Fatal("second request did not include first scratch allocation")
	}
	if node.Device[0].UsedCore != 0 || node.Device[0].UsedMem != 0 || node.Device[0].UsedNum != 0 {
		t.Fatal("full proposal changed the production ledger")
	}
	if err := ValidateVGPUJobPlanSnapshot(nodes, plan); err != nil {
		t.Fatal(err)
	}
}

func TestVGPUJobPlannerBacktracksAcrossNodes(t *testing.T) {
	plannerSetup(t)
	a := rfTestNode("a", "NVIDIA-L40", 1, 8000)
	b := rfTestNode("b", "NVIDIA-L40", 1, 4096)
	pods := []*v1.Pod{rfTestPod("small-first", "training", 4096, 40), rfTestPod("large-second", "training", 6000, 40)}
	plan, err := PlanCompleteVGPUJob(map[string]*GPUDevices{"b": b, "a": a}, pods)
	if err != nil || len(plan.Members) != 2 {
		t.Fatalf("plan failed: %+v, %v", plan, err)
	}
	// Equal scores make a preferred initially; only backtracking lets the
	// second, larger Pod use a.
	if plan.Members[0].Node != "b" || plan.Members[1].Node != "a" {
		t.Fatalf("did not backtrack: %+v", plan.Members)
	}
	if a.Device[0].UsedNum != 0 || b.Device[0].UsedNum != 0 {
		t.Fatal("search modified the input nodes")
	}
}

func TestVGPUJobPlannerMultiGPUIsAllOrNothing(t *testing.T) {
	plannerSetup(t)
	node := rfTestNode("node", "NVIDIA-L40", 2, 24564)
	pod := rfTestPod("two-gpu", "training", 4096, 40)
	pod.Spec.Containers[0].Resources.Limits["volcano.sh/vgpu-number"] = *resource.NewQuantity(2, resource.DecimalSI)
	nodes := map[string]*GPUDevices{"node": node}
	plan, err := PlanCompleteVGPUJob(nodes, []*v1.Pod{pod})
	if err != nil || len(plan.Members) != 1 || len(plan.Members[0].Devices[0]) != 2 ||
		plan.Members[0].Devices[0][0].UUID == plan.Members[0].Devices[0][1].UUID {
		t.Fatalf("expected both distinct GPUs: plan=%+v err=%v", plan, err)
	}
	node.Device[1].Health = false
	if plan, err = PlanCompleteVGPUJob(nodes, []*v1.Pod{pod}); plan != nil || !errors.Is(err, ErrVGPUPlanNoCandidate) {
		t.Fatalf("must not return partial 2-GPU allocation: plan=%+v err=%v", plan, err)
	}
	if node.Device[0].UsedNum != 0 || node.Device[1].UsedNum != 0 {
		t.Fatal("partial multi-GPU plan changed input")
	}
}

func TestVGPUJobPlannerRejectsDriftAndIdentityReplacement(t *testing.T) {
	plannerSetup(t)
	node := rfTestNode("node", "NVIDIA-L40", 1, 24564)
	bg := plannerBackground(node, "old", 1024, 10)
	nodes := map[string]*GPUDevices{"node": node}
	plan, err := PlanCompleteVGPUJob(nodes, []*v1.Pod{rfTestPod("planned", "training", 4096, 40)})
	if err != nil {
		t.Fatal(err)
	}
	// Equal aggregate resource/model features are insufficient: an old Pod
	// UID replaced by a different UID is still a distinct source ledger.
	usage := node.Device[0].PodMap[string(bg.UID)]
	delete(node.Device[0].PodMap, string(bg.UID))
	node.Device[0].PodMap["replacement-uid"] = usage
	if !errors.Is(ValidateVGPUJobPlanSnapshot(nodes, plan), ErrVGPUPlanStale) {
		t.Fatal("changed source Pod identity was not detected")
	}
}

func TestVGPUJobPlannerRejectsDuplicateOrAlreadyAllocatedPod(t *testing.T) {
	plannerSetup(t)
	node := rfTestNode("node", "NVIDIA-L40", 1, 24564)
	pod := rfTestPod("job", "training", 4096, 40)
	nodes := map[string]*GPUDevices{"node": node}
	if p, err := PlanCompleteVGPUJob(nodes, []*v1.Pod{pod, pod.DeepCopy()}); err == nil || p != nil {
		t.Fatal("duplicate UID accepted")
	}
	bg := plannerBackground(node, "allocated", 4096, 30)
	if p, err := PlanCompleteVGPUJob(nodes, []*v1.Pod{bg}); err == nil || p != nil {
		t.Fatal("already accounted Pod UID accepted")
	}
	bound := pod.DeepCopy()
	bound.Spec.NodeName = "node"
	if p, err := PlanCompleteVGPUJob(nodes, []*v1.Pod{bound}); err == nil || p != nil {
		t.Fatal("bound Pod accepted as pending")
	}
	stale := pod.DeepCopy()
	stale.Annotations = map[string]string{AssignedIDsToAllocateAnnotations: "stale"}
	if p, err := PlanCompleteVGPUJob(nodes, []*v1.Pod{stale}); err == nil || p != nil {
		t.Fatal("pending Pod with device-consumable metadata accepted")
	}
	done := pod.DeepCopy()
	done.Status.Phase = v1.PodSucceeded
	if p, err := PlanCompleteVGPUJob(nodes, []*v1.Pod{done}); err == nil || p != nil {
		t.Fatal("completed Pod accepted as pending")
	}
}

func TestVGPUJobPlannerDetectsSourceChangeDuringRFRequest(t *testing.T) {
	rfTestConfig(t)
	NodeLockEnable = true
	node := rfTestNode("node", "NVIDIA-L40", 1, 24564)
	calls := rfMockServer(t, func(request rfRequest) *rfResponse {
		// Mutate only the original ledger while the planner is waiting for RF;
		// an external concurrent allocation must invalidate the snapshot.
		rfLedgerMu.Lock()
		node.Device[0].UsedCore++
		rfLedgerMu.Unlock()
		return rfMockResponse(request)
	})
	plan, err := PlanCompleteVGPUJob(map[string]*GPUDevices{"node": node}, []*v1.Pod{rfTestPod("race", "training", 4096, 40)})
	if plan != nil || !errors.Is(err, ErrVGPUPlanStale) || calls.Load() != 1 {
		t.Fatalf("concurrent source update was not rejected: plan=%+v err=%v calls=%d", plan, err, calls.Load())
	}
	if node.Device[0].UsedCore != 1 || node.Device[0].UsedMem != 0 || node.Device[0].UsedNum != 0 {
		t.Fatal("planner changed the source beyond the simulated external update")
	}
}

func TestVGPUJobPlannerRFTransportFailureUsesNativeFallback(t *testing.T) {
	rfTestConfig(t)
	NodeLockEnable = true
	rfMockServer(t, func(rfRequest) *rfResponse { return nil })
	node := rfTestNode("node", "NVIDIA-L40", 1, 24564)
	plan, err := PlanCompleteVGPUJob(map[string]*GPUDevices{"node": node}, []*v1.Pod{rfTestPod("fallback", "training", 4096, 40)})
	if err != nil || len(plan.Members) != 1 || plan.Members[0].Fallback == "" {
		t.Fatalf("native fallback policy was not preserved/reported: plan=%+v err=%v", plan, err)
	}
	if node.Device[0].UsedCore != 0 || node.Device[0].UsedMem != 0 || node.Device[0].UsedNum != 0 {
		t.Fatal("fallback proposal modified original ledger")
	}
}

func TestVGPUJobPlannerRejectsChangedPolicy(t *testing.T) {
	plannerSetup(t)
	node := rfTestNode("node", "NVIDIA-L40", 1, 24564)
	nodes := map[string]*GPUDevices{"node": node}
	plan, err := PlanCompleteVGPUJob(nodes, []*v1.Pod{rfTestPod("config-test", "training", 4096, 40)})
	if err != nil {
		t.Fatal(err)
	}
	RFPredictionEnabled = !RFPredictionEnabled
	if !errors.Is(ValidateVGPUJobPlanSnapshot(nodes, plan), ErrVGPUPlanStale) {
		t.Fatal("plan was accepted after RF policy changed")
	}
}

// Optional explicit integration with the already-frozen CPU RF service. No
// cluster, GPU, Kubernetes API, model training, or device plugin is involved.
func TestVGPUJobPlannerLiveFrozenRF(t *testing.T) {
	endpoint := os.Getenv("RF_LIVE_ENDPOINT")
	if endpoint == "" {
		t.Skip("set RF_LIVE_ENDPOINT to run the frozen RF integration")
	}
	rfTestConfig(t)
	NodeLockEnable = true // A successful run must not attempt a real node lock.
	RFEndpoint = endpoint
	trace := &rfTraceTransport{base: rfHTTP.Transport}
	rfHTTP = &http.Client{Transport: trace, CheckRedirect: rfHTTP.CheckRedirect}
	// Match the frozen RF hardware catalogue rather than inventing a
	// registered-memory value for this synthetic L40 resource ledger.
	node := rfTestNode("frozen-rf", "NVIDIA-L40", 1, 46068)
	background := plannerBackground(node, "live-background", 4096, 30)
	pods := []*v1.Pod{rfTestPod("live-large-0", "training", 4096, 40), rfTestPod("live-large-1", "training", 4096, 40)}
	nodes := map[string]*GPUDevices{"frozen-rf": node}
	before := vgpuJobSourceToken(node)
	if plan, err := PlanCompleteVGPUJob(nodes, pods); plan != nil || !errors.Is(err, ErrVGPUPlanNoCandidate) {
		t.Fatalf("30+2x40 must not yield a plan: %+v, %v", plan, err)
	}
	small, err := PlanCompleteVGPUJob(nodes, []*v1.Pod{rfTestPod("live-small", "inference", 4096, 60)})
	if err != nil || len(small.Members) != 1 || small.Members[0].Fallback != "" {
		t.Fatalf("60-core plan needs an actual RF response: %+v, %v", small, err)
	}
	if before != vgpuJobSourceToken(node) {
		t.Fatal("proposal changed original background allocation")
	}
	node.SubResource(background)
	complete, err := PlanCompleteVGPUJob(nodes, pods)
	if err != nil || len(complete.Members) != 2 {
		t.Fatalf("2x40 after release: %+v, %v", complete, err)
	}
	for _, member := range complete.Members {
		if member.Fallback != "" || member.RequestID == "" || member.Devices[0][0].UUID != node.Device[0].UUID {
			t.Fatalf("complete plan must use a validated frozen RF response: %+v", member)
		}
	}
	trace.mu.Lock()
	defer trace.mu.Unlock()
	if len(trace.rows) < 3 {
		t.Fatalf("expected real RF responses; got %d", len(trace.rows))
	}
	for _, row := range trace.rows {
		var response rfResponse
		if err := json.Unmarshal(row["response"].(json.RawMessage), &response); err != nil ||
			row["http_status"] != http.StatusOK || response.ModelVersion != RFModelVersion ||
			!response.ModelReady || response.Status != "ranked" {
			t.Fatalf("unexpected frozen RF response: %+v / %v", row, err)
		}
	}
	if node.Device[0].UsedCore != 0 || node.Device[0].UsedMem != 0 || node.Device[0].UsedNum != 0 {
		t.Fatal("RF planner modified original node")
	}
	if output := os.Getenv("VGPU_PLANNER_TRACE"); output != "" {
		file, err := os.OpenFile(output, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
		if err != nil {
			t.Fatal(err)
		}
		data := map[string]interface{}{"scope": "offline synthetic vGPU ledger, actual frozen RF HTTP adviser; no Kubernetes or device plugin",
			"model_version": RFModelVersion, "adviser_calls": len(trace.rows), "rf_http": trace.rows,
			"background_cores": 30, "large_members_cores": []int{40, 40}, "small_cores": 60,
			"initial_large_outcome": "no_complete_candidate", "small_plan": small, "large_plan_after_release": complete}
		if err := json.NewEncoder(file).Encode(data); err != nil {
			file.Close()
			t.Fatal(err)
		}
		if err := file.Close(); err != nil {
			t.Fatal(err)
		}
	}
	t.Logf("frozen RF adviser calls=%d, version=%s, no fallbacks", len(trace.rows), RFModelVersion)
}
