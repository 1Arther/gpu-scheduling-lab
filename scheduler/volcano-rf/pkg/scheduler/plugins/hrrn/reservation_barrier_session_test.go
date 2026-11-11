package hrrn

import (
	"testing"
	"time"

	v1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	"volcano.sh/volcano/pkg/scheduler/api"
	"volcano.sh/volcano/pkg/scheduler/conf"
	"volcano.sh/volcano/pkg/scheduler/framework"
)

// Exercise the real plugin entry point, not a manually registered test guard.
// A bare Session has no Kubernetes client; invalid/unavailable configuration
// must still deny vGPU before allocation while CPU is unaffected by the guard.
func TestReservationBarrierOnSessionOpenConfiguration(t *testing.T) {
	gpu := func() *api.TaskInfo {
		return &api.TaskInfo{Pod: &v1.Pod{Spec: v1.PodSpec{Containers: []v1.Container{{Resources: v1.ResourceRequirements{Limits: v1.ResourceList{v1.ResourceName("volcano.sh/vgpu-number"): resource.MustParse("1")}}}}}}}
	}
	node := &api.NodeInfo{Name: "node"}
	cases := []struct {
		name    string
		args    framework.Arguments
		tiers   []conf.Tier
		guarded bool
	}{
		{name: "default-off", args: framework.Arguments{}, guarded: false},
		{name: "hrrn-disabled-explicit-barrier", args: framework.Arguments{"enabled": false, "reservation-barrier": true}, guarded: true},
		{name: "malformed-barrier-fails-closed", args: framework.Arguments{"reservation-barrier": "invalid"}, guarded: true},
		{name: "configured-source-unavailable", args: framework.Arguments{"reservation-barrier": true, "reservation-namespace": "ns", "reservation-name": "ledger", "reservation-uid": "uid", "reservation-epoch": "0123456789abcdef0123456789abcdef"}, guarded: true},
		{name: "unsafe-hrrn-order-does-not-disable-guard", args: framework.Arguments{"enabled": true, "reservation-barrier": true}, guarded: true},
		{name: "later-false-does-not-weaken-tier", args: framework.Arguments{"reservation-barrier": false}, tiers: []conf.Tier{{Plugins: []conf.PluginOption{{Name: PluginName, Arguments: framework.Arguments{"reservation-barrier": true}}}}}, guarded: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s := &framework.Session{Tiers: tc.tiers}
			p := NewWithClock(tc.args, time.Now, NewTracker())
			p.OnSessionOpen(s)
			if s.HasAllocationVeto() != tc.guarded {
				t.Fatalf("guard registered=%v, want %v", s.HasAllocationVeto(), tc.guarded)
			}
			if err := s.CheckAllocationVeto(&api.TaskInfo{Pod: &v1.Pod{}}, node); err != nil {
				t.Fatalf("CPU allocation rejected by reservation barrier: %v", err)
			}
			pod := gpu()
			err := s.CheckAllocationVeto(pod, node)
			if framework.IsAllocationVeto(err) != tc.guarded {
				t.Fatalf("unexpected direct veto result: %v", err)
			}
			if tc.guarded {
				if err := s.PredicateFn(pod, node); !framework.IsAllocationVeto(err) {
					t.Fatalf("configured guard did not affect real predicate: %v", err)
				}
				if err := s.Allocate(pod, node); !framework.IsAllocationVeto(err) || pod.Pod.Spec.NodeName != "" {
					t.Fatalf("configured guard did not stop real allocation before mutation: %v", err)
				}
			}
		})
	}
}
