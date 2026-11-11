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
	schedulingv1 "volcano.sh/apis/pkg/apis/scheduling/v1beta1"
	"volcano.sh/volcano/pkg/scheduler/api"
	"volcano.sh/volcano/pkg/scheduler/api/devices/nvidia/vgpu"
)

// Unlike 2x60 on a 100-core GPU, this Job is structurally feasible. Only a
// currently running 30-core background Pod prevents the complete 2x40 plan.
func TestTemporarilyUnavailableCompleteJobEventuallyBinds(t *testing.T) {
	requireRF(t)
	var records []map[string]interface{}
	for _, mode := range []struct {
		name              string
		enabled, complete bool
	}{
		{"A-native", false, false}, {"B-hrrn", true, false}, {"C-hrrn-complete", true, true},
	} {
		t.Run(mode.name, func(t *testing.T) {
			groups := []*schedulingv1.PodGroup{pg("background", "100", 1, 1), pg("large", "10", 2, 1), pg("small", "100", 1, 1)}
			groups[0].Status.Phase = schedulingv1.PodGroupRunning
			groups[2].CreationTimestamp = metav1.NewTime(base.Add(-30 * time.Second))
			background := pod("background", "background-0", 1, 30)
			background.Spec.NodeName = "node"
			background.Status.Phase = v1.PodRunning
			background.Annotations[vgpu.AssignedIDsAnnotations] = "SIM-HRRN-GPU-0,NVIDIA,4096,30:"
			background.Annotations[vgpu.AssignedNodeAnnotations] = "node"
			pods := []*v1.Pod{background, pod("large", "large-0", 1, 40), pod("large", "large-1", 1, 40), pod("small", "small-0", 1, 60)}
			tracker := NewTracker()
			warm := setup(t, groups, pods, mode.enabled, mode.complete, vgpu.RFJobPolicy, 1, tracker, func() time.Time { return base })
			closeFixture(warm)
			f := setup(t, groups, pods, mode.enabled, mode.complete, vgpu.RFJobPolicy, 1, tracker, func() time.Time { return base.Add(20 * time.Second) })
			defer closeFixture(f)
			// The fixture installs its virtual device after cache creation. Replay
			// the observed running Pod through the unchanged native AddResource.
			f.gpus.AddResource(background)
			if f.gpus.Device[0].UsedCore != 30 || f.gpus.Device[0].UsedNum != 1 {
				t.Fatal("background ledger is wrong")
			}
			if mode.complete {
				f.run(t, "small-0")
			} else {
				f.run(t, "large-0")
			}
			large := f.ssn.Jobs["hrrn-test/large"]
			expected := uint(70)
			if mode.complete {
				expected = 90
			}
			if f.gpus.Device[0].UsedCore != expected || f.gpus.Device[0].UsedNum != 2 || f.gpus.Device[0].UsedMem != 8192 {
				t.Fatal("competition ledger is wrong", f.gpus.Device[0])
			}
			row := map[string]interface{}{"mode": mode.name, "capacity": 100, "background": 30, "large_request": []int{40, 40}, "small_request": 60, "after_competition_cores": expected, "large_bound_members": large.ReadyTaskNum(), "structurally_feasible": true}
			if mode.complete {
				if large.ReadyTaskNum() != 0 {
					t.Fatal("partial Job survived complete-mode rollback")
				}
				var pending []*v1.Pod
				for _, name := range []string{"large-0", "large-1"} {
					p, err := f.ssn.KubeClient().CoreV1().Pods("hrrn-test").Get(context.Background(), name, metav1.GetOptions{})
					if err != nil {
						t.Fatal(err)
					}
					for _, key := range []string{vgpu.AssignedIDsAnnotations, vgpu.AssignedIDsToAllocateAnnotations, vgpu.AssignedNodeAnnotations, vgpu.AssignedTimeAnnotations, vgpu.DeviceBindPhase} {
						if p.Annotations[key] != "" {
							t.Fatalf("stale allocation %s on %s: %v", key, name, p.Annotations)
						}
					}
					pending = append(pending, p.DeepCopy())
				}
				// Model actual completion/removal events, not a counter reset. The
				// native NodeInfo.RemoveTask invokes native device SubResource.
				for _, name := range []string{"background", "small"} {
					j := f.ssn.Jobs[api.JobID("hrrn-test/"+name)]
					for _, task := range j.Tasks {
						if err := f.ssn.Nodes["node"].RemoveTask(task); err != nil {
							t.Fatal(err)
						}
						if err := j.UpdateTaskStatus(task, api.Succeeded); err != nil {
							t.Fatal(err)
						}
						uid := task.Pod.UID
						if err := f.ssn.KubeClient().CoreV1().Pods(task.Namespace).Delete(context.Background(), task.Name, metav1.DeleteOptions{Preconditions: &metav1.Preconditions{UID: &uid}}); err != nil {
							t.Fatal(err)
						}
					}
				}
				if f.gpus.Device[0].UsedCore != 0 || f.gpus.Device[0].UsedMem != 0 || f.gpus.Device[0].UsedNum != 0 {
					t.Fatal("native completion release leaked quotas")
				}
				row["after_release_cores"] = 0
				// A subsequent real native Session receives the remaining pending
				// membership after the observed completion events.
				next := setup(t, []*schedulingv1.PodGroup{groups[1]}, pending, true, true, vgpu.RFJobPolicy, 1, tracker, func() time.Time { return base.Add(40 * time.Second) })
				defer closeFixture(next)
				next.run(t, "large-0", "large-1")
				if next.gpus.Device[0].UsedCore != 80 || next.gpus.Device[0].UsedNum != 2 || next.gpus.Device[0].UsedMem != 8192 {
					t.Fatal("eventual complete plan ledger is wrong")
				}
				for _, p := range pending {
					saved, err := next.ssn.KubeClient().CoreV1().Pods(p.Namespace).Get(context.Background(), p.Name, metav1.GetOptions{})
					if err != nil {
						t.Fatal(err)
					}
					if !strings.Contains(saved.Annotations[vgpu.AssignedIDsToAllocateAnnotations], "SIM-HRRN-GPU-0,NVIDIA,4096,40:") {
						t.Fatal("eventual bind UUID/quota mismatch")
					}
				}
				row["eventual_large_bound_members"] = 2
				row["eventual_cores"] = 80
				row["rollback_annotations_clean"] = true
			}
			records = append(records, row)
		})
	}
	if out := os.Getenv("HRRN_STAGE5_RESULTS"); out != "" {
		data, _ := json.MarshalIndent(records, "", "  ")
		if err := os.WriteFile(filepath.Join(out, "temporary-capacity-results.json"), data, 0644); err != nil {
			t.Fatal(err)
		}
	}
}
