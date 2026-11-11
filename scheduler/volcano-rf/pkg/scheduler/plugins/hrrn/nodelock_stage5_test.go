// Copyright 2026 The Volcano Authors. Apache-2.0.
package hrrn

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	v1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	clienttesting "k8s.io/client-go/testing"
	schedulingv1 "volcano.sh/apis/pkg/apis/scheduling/v1beta1"
	"volcano.sh/volcano/pkg/scheduler/api"
	"volcano.sh/volcano/pkg/scheduler/api/devices/nvidia/vgpu"
	"volcano.sh/volcano/pkg/scheduler/plugins/util/nodelock"
)

// This is a diagnostic regression capture of the FROZEN implementation, not a
// passing acceptance test for complete-job support on a live NodeLock backend.
// The earlier fixture intentionally disabled NodeLock. With the production
// setting enabled, one speculative Allocate acquires the node lock; native
// Discard restores resource counters and the HRRN hook restores Pod metadata,
// but neither releases that lock. No Pod is bound to trigger plugin consumption.
func TestDiagnosticFrozenNodeLockBlocksCompleteJobAndBypass(t *testing.T) {
	requireRF(t)
	var records []map[string]interface{}
	for _, backgroundCores := range []int{30, 0} {
		name := "temporary_capacity_30_plus_2x40_and_60"
		if backgroundCores == 0 {
			name = "fully_available_2x40_still_cannot_commit"
		}
		t.Run(name, func(t *testing.T) {
			groups := []*schedulingv1.PodGroup{pg("large", "10", 2, 1), pg("small", "100", 1, 1)}
			groups[1].CreationTimestamp = metav1.NewTime(base.Add(-30 * time.Second))
			pods := []*v1.Pod{pod("large", "large-0", 1, 40), pod("large", "large-1", 1, 40), pod("small", "small-0", 1, 60)}
			var background *v1.Pod
			if backgroundCores != 0 {
				bg := pg("background", "100", 1, 1)
				bg.Status.Phase = schedulingv1.PodGroupRunning
				groups = append(groups, bg)
				background = pod("background", "background-0", 1, backgroundCores)
				background.Spec.NodeName = "node"
				background.Status.Phase = v1.PodRunning
				background.Annotations[vgpu.AssignedIDsAnnotations] = "SIM-HRRN-GPU-0,NVIDIA,4096,30:"
				background.Annotations[vgpu.AssignedNodeAnnotations] = "node"
				pods = append(pods, background)
			}
			tracker := NewTracker()
			warm := setup(t, groups, pods, true, true, vgpu.RFJobPolicy, 1, tracker, func() time.Time { return base })
			closeFixture(warm)
			f := setup(t, groups, pods, true, true, vgpu.RFJobPolicy, 1, tracker, func() time.Time { return base.Add(20 * time.Second) })
			defer closeFixture(f)
			if background != nil {
				f.gpus.AddResource(background)
			}
			// RegisterSession seeds the scheduler cache, not API Nodes. The real
			// NodeLock implementation must see a Node in the fake Kubernetes API.
			apiNode := f.test.Nodes[0].DeepCopy()
			apiNode.Annotations = map[string]string{}
			if _, err := f.ssn.KubeClient().CoreV1().Nodes().Create(context.Background(), apiNode, metav1.CreateOptions{}); err != nil {
				t.Fatal(err)
			}
			oldEnabled, oldClient := vgpu.NodeLockEnable, nodelock.GetClient()
			// The existing setup opens the Session with NodeLock false. Enable
			// exactly the live flag before executing its genuine native actions.
			vgpu.NodeLockEnable = true
			t.Cleanup(func() {
				vgpu.NodeLockEnable = oldEnabled
				_ = nodelock.UseClient(oldClient)
			})

			f.run(t) // Diagnostic expectation: neither big nor small binds.
			for _, id := range []api.JobID{"hrrn-test/large", "hrrn-test/small"} {
				if f.ssn.Jobs[id].ReadyTaskNum() != 0 {
					t.Fatal("frozen NodeLock failure mechanism changed; review the diagnostic expectation", id)
				}
			}
			wantMemory, wantSlots := uint64(0), uint(0)
			if backgroundCores != 0 {
				wantMemory, wantSlots = 4096, 1
			}
			device := f.gpus.Device[0]
			if uint64(device.UsedMem) != wantMemory || device.UsedCore != uint(backgroundCores) || device.UsedNum != wantSlots {
				t.Fatal("resource rollback did not preserve the background-only ledger", device)
			}
			for _, name := range []string{"large-0", "large-1", "small-0"} {
				saved, err := f.ssn.KubeClient().CoreV1().Pods("hrrn-test").Get(context.Background(), name, metav1.GetOptions{})
				if err != nil {
					t.Fatal(err)
				}
				if saved.Spec.NodeName != "" {
					t.Fatal("diagnostic unexpectedly produced a bound Pod", name)
				}
				for _, key := range []string{vgpu.AssignedIDsAnnotations, vgpu.AssignedIDsToAllocateAnnotations, vgpu.AssignedNodeAnnotations, vgpu.AssignedTimeAnnotations, vgpu.DeviceBindPhase, vgpu.BindTimeAnnotations} {
					if saved.Annotations[key] != "" {
						t.Fatal("diagnostic has additional leaked Pod metadata", name, key)
					}
				}
			}
			locked, err := f.ssn.KubeClient().CoreV1().Nodes().Get(context.Background(), "node", metav1.GetOptions{})
			if err != nil {
				t.Fatal(err)
			}
			lockValue := locked.Annotations[vgpu.DeviceName]
			if _, err := time.Parse(time.RFC3339, lockValue); err != nil {
				t.Fatal("expected native NodeLock retained after Discard", lockValue, err)
			}
			if err := nodelock.LockNode("node", vgpu.DeviceName); err == nil || !strings.Contains(err.Error(), "locked within 5 minutes") {
				t.Fatal("retained lock did not reject a subsequent allocation", err)
			}
			// API action history proves that a speculative UUID patch occurred
			// before cleanup, instead of mistaking 'no plan was attempted' for
			// successful complete-job rollback.
			history, ok := f.ssn.KubeClient().(interface{ Actions() []clienttesting.Action })
			if !ok {
				t.Fatal("diagnostic requires fake API action history")
			}
			var speculative []string
			for _, action := range history.Actions() {
				patch, ok := action.(clienttesting.PatchAction)
				if !ok || patch.GetResource().Resource != "pods" {
					continue
				}
				var body struct {
					Metadata struct {
						Annotations map[string]*string `json:"annotations"`
					} `json:"metadata"`
				}
				if err := json.Unmarshal(patch.GetPatch(), &body); err != nil {
					t.Fatal(err)
				}
				if ids := body.Metadata.Annotations[vgpu.AssignedIDsToAllocateAnnotations]; ids != nil && strings.Contains(*ids, "SIM-HRRN-GPU-0,NVIDIA,4096,40:") {
					speculative = append(speculative, patch.GetName())
				}
			}
			if len(speculative) != 1 || !strings.HasPrefix(speculative[0], "large-") {
				t.Fatal("expected exactly one speculative large-member UUID patch", speculative)
			}
			records = append(records, map[string]interface{}{
				"scenario": name, "node_lock_enabled": true, "capacity_cores": 100,
				"background_cores": backgroundCores, "large_request_cores": []int{40, 40}, "small_request_cores": 60,
				"bound_candidate_pods": 0, "speculatively_annotated_pods": speculative,
				"ledger_cores_after_rollback": device.UsedCore, "pending_pod_annotations_restored": true,
				"node_lock_retained": lockValue, "subsequent_allocation_rejected_by_lock": true,
				"desired_complete_job_and_bypass_supported": false,
				"evidence_scope": "actual Go actions/frozen RF/native NodeLock/fake API; synthetic unit clock; no kubelet/device plugin or real cluster",
			})
		})
	}
	if out := os.Getenv("HRRN_STAGE5_RESULTS"); out != "" {
		body, err := json.MarshalIndent(records, "", "  ")
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(out, "nodelock-diagnostic.json"), body, 0644); err != nil {
			t.Fatal(err)
		}
	}
}

// Positive controls isolate the single-Pod A/B path from the multi-Pod defect
// above. NodeLock is expected to remain held here: the fake binder has accepted
// one committed Pod, but no kubelet/Device Plugin exists to consume its UUID.
func TestNodeLockEnabledSinglePodOrderingControls(t *testing.T) {
	requireRF(t)
	var records []map[string]interface{}
	for _, mode := range []struct {
		name, winner, loser string
		enabled             bool
	}{
		{"A-native-rf", "long", "short", false},
		{"B-hrrn-rf", "short", "long", true},
	} {
		t.Run(mode.name, func(t *testing.T) {
			groups := []*schedulingv1.PodGroup{pg("long", "100", 1, 1), pg("short", "10", 1, 1)}
			groups[1].CreationTimestamp = metav1.NewTime(base.Add(-30 * time.Second))
			pods := []*v1.Pod{pod("long", "long-0", 1, 100), pod("short", "short-0", 1, 100)}
			tracker := NewTracker()
			warm := setup(t, groups, pods, mode.enabled, false, vgpu.RFJobPolicy, 1, tracker, func() time.Time { return base })
			closeFixture(warm)
			f := setup(t, groups, pods, mode.enabled, false, vgpu.RFJobPolicy, 1, tracker, func() time.Time { return base.Add(20 * time.Second) })
			defer closeFixture(f)
			apiNode := f.test.Nodes[0].DeepCopy()
			apiNode.Annotations = map[string]string{}
			if _, err := f.ssn.KubeClient().CoreV1().Nodes().Create(context.Background(), apiNode, metav1.CreateOptions{}); err != nil {
				t.Fatal(err)
			}
			oldEnabled, oldClient := vgpu.NodeLockEnable, nodelock.GetClient()
			vgpu.NodeLockEnable = true
			t.Cleanup(func() {
				vgpu.NodeLockEnable = oldEnabled
				_ = nodelock.UseClient(oldClient)
			})
			winner := f.ssn.Jobs[api.JobID("hrrn-test/"+mode.winner)]
			loser := f.ssn.Jobs[api.JobID("hrrn-test/"+mode.loser)]
			if winner.Queue != loser.Queue || winner.Priority != loser.Priority || !f.ssn.JobOrderFn(winner, loser) {
				t.Fatal("single-Pod control does not have the expected native JobOrder result")
			}
			if mode.enabled {
				long, _ := f.plugin.Snapshot("hrrn-test/long")
				short, _ := f.plugin.Snapshot("hrrn-test/short")
				if long.WaitingSeconds != 20 || short.WaitingSeconds != 20 || long.Score != 1.2 || short.Score != 3 {
					t.Fatal("synthetic unit-test HRRN inputs changed", long, short)
				}
			} else if len(f.plugin.snapshots) != 0 {
				t.Fatal("disabled HRRN should not create Tracker snapshots")
			}
			// CheckBind verifies the committed fake-binder request, not merely a
			// speculative allocation log. Exactly one full 100-core Pod may bind.
			f.run(t, mode.winner+"-0")
			if winner.ReadyTaskNum() != 1 || loser.ReadyTaskNum() != 0 {
				t.Fatal("single-Pod A/B control did not commit exactly the selected Job")
			}
			device := f.gpus.Device[0]
			if device.UsedCore != 100 || device.UsedMem != 4096 || device.UsedNum != 1 {
				t.Fatal("committed single-Pod ledger is inconsistent", device)
			}
			saved, err := f.ssn.KubeClient().CoreV1().Pods("hrrn-test").Get(context.Background(), mode.winner+"-0", metav1.GetOptions{})
			if err != nil {
				t.Fatal(err)
			}
			ids := "SIM-HRRN-GPU-0,NVIDIA,4096,100:"
			if saved.Annotations[vgpu.AssignedIDsToAllocateAnnotations] != ids || saved.Annotations[vgpu.AssignedIDsAnnotations] != ids || saved.Annotations[vgpu.AssignedNodeAnnotations] != "node" {
				t.Fatal("committed Pod did not retain the exact native UUID allocation", saved.Annotations)
			}
			unselected, err := f.ssn.KubeClient().CoreV1().Pods("hrrn-test").Get(context.Background(), mode.loser+"-0", metav1.GetOptions{})
			if err != nil {
				t.Fatal(err)
			}
			for _, key := range []string{vgpu.AssignedIDsAnnotations, vgpu.AssignedIDsToAllocateAnnotations, vgpu.AssignedNodeAnnotations, vgpu.AssignedTimeAnnotations, vgpu.DeviceBindPhase, vgpu.BindTimeAnnotations} {
				if unselected.Annotations[key] != "" {
					t.Fatal("unselected Pod acquired assignment metadata", key)
				}
			}
			locked, err := f.ssn.KubeClient().CoreV1().Nodes().Get(context.Background(), "node", metav1.GetOptions{})
			if err != nil {
				t.Fatal(err)
			}
			if _, err := time.Parse(time.RFC3339, locked.Annotations[vgpu.DeviceName]); err != nil {
				t.Fatal("native lock should await Device Plugin consumption", err)
			}
			records = append(records, map[string]interface{}{
				"mode": mode.name, "node_lock_enabled": true, "complete_job": false,
				"capacity_cores": 100, "request_cores_per_job": 100,
				"estimated_seconds":         map[string]int{"long": 100, "short": 10},
				"committed_fake_bind_count": 1, "committed_pod": mode.winner + "-0",
				"pending_pod": mode.loser + "-0", "native_uuid_annotation": ids,
				"ledger_cores_after_commit":           device.UsedCore,
				"node_lock_waiting_for_device_plugin": locked.Annotations[vgpu.DeviceName],
				"evidence_scope":                      "actual Go actions/frozen RF/native NodeLock/fake binder and API; synthetic unit clock; no kubelet/device plugin or real cluster",
			})
		})
	}
	if out := os.Getenv("HRRN_STAGE5_RESULTS"); out != "" {
		body, err := json.MarshalIndent(records, "", "  ")
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(out, "nodelock-ordering-controls.json"), body, 0644); err != nil {
			t.Fatal(err)
		}
	}
}
