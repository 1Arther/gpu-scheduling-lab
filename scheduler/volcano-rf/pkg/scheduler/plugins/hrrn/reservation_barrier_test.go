package hrrn

import (
	"context"
	"fmt"
	"strconv"
	"strings"
	"testing"

	v1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes/fake"
	clienttesting "k8s.io/client-go/testing"
	"volcano.sh/volcano/pkg/scheduler/api"
	"volcano.sh/volcano/pkg/scheduler/conf"
	"volcano.sh/volcano/pkg/scheduler/framework"
	"volcano.sh/volcano/pkg/scheduler/plugins/hrrn/coordination"
)

// This fake explicitly models ConfigMap resourceVersion changes. It is not a
// real API server, external fencing, NodeLock or a device-plugin integration.
func barrierFakeStore(t *testing.T) *coordination.DurableCoordinator {
	t.Helper()
	c := fake.NewSimpleClientset()
	gvr := schema.GroupVersionResource{Version: "v1", Resource: "configmaps"}
	version := 0
	c.PrependReactor("create", "configmaps", func(a clienttesting.Action) (bool, runtime.Object, error) {
		obj := a.(clienttesting.CreateAction).GetObject().(*v1.ConfigMap).DeepCopy()
		version++
		obj.UID = types.UID("barrier-ledger")
		obj.ResourceVersion = strconv.Itoa(version)
		if err := c.Tracker().Create(gvr, obj, a.GetNamespace()); err != nil {
			return true, nil, err
		}
		return true, obj.DeepCopy(), nil
	})
	c.PrependReactor("update", "configmaps", func(a clienttesting.Action) (bool, runtime.Object, error) {
		obj := a.(clienttesting.UpdateAction).GetObject().(*v1.ConfigMap).DeepCopy()
		old, err := c.Tracker().Get(gvr, a.GetNamespace(), obj.Name)
		if err != nil {
			return true, nil, err
		}
		if obj.UID != old.(*v1.ConfigMap).UID || obj.ResourceVersion != old.(*v1.ConfigMap).ResourceVersion {
			return true, nil, fmt.Errorf("fake conflict")
		}
		version++
		obj.ResourceVersion = strconv.Itoa(version)
		if err = c.Tracker().Update(gvr, obj, a.GetNamespace()); err != nil {
			return true, nil, err
		}
		return true, obj.DeepCopy(), nil
	})
	key := coordination.DeviceKey{Node: "node", GPU: "GPU-1"}
	d, err := coordination.CreateDurable(context.Background(), c, "test", "ledger", map[coordination.DeviceKey]coordination.Resources{key: {Memory: 10000, Cores: 100, Instances: 4}}, map[coordination.DeviceKey]coordination.Resources{key: {}})
	if err != nil {
		t.Fatal(err)
	}
	return d
}

func TestReservationBarrierReadsDurableViewButNeverGrantsVGPU(t *testing.T) {
	d := barrierFakeStore(t)
	reads := 0
	view := func(ctx context.Context) (coordination.ReservationView, error) {
		reads++
		return d.ReservationView(ctx)
	}
	cpu := &api.TaskInfo{Pod: &v1.Pod{}}
	gpu := &api.TaskInfo{Pod: &v1.Pod{Spec: v1.PodSpec{Containers: []v1.Container{{Resources: v1.ResourceRequirements{Limits: v1.ResourceList{v1.ResourceName("volcano.sh/vgpu-number"): resource.MustParse("1")}}}}}}}
	node := &api.NodeInfo{Name: "node"}
	guard := newReservationBarrier(view)
	if err := guard(cpu, node); err != nil || reads != 0 {
		t.Fatal("CPU task was blocked or touched reservation store")
	}
	if err := guard(gpu, node); err == nil || !strings.Contains(err.Error(), "RV=1") {
		t.Fatalf("empty view incorrectly granted or not read: %v", err)
	}
	key := coordination.DeviceKey{Node: "node", GPU: "GPU-1"}
	plan := coordination.JobPlan{UID: "job", Pods: []coordination.PodPlan{{UID: "pod", Node: "node", Allocations: []coordination.Allocation{{Container: "worker", Device: key, Resources: coordination.Resources{Memory: 1000, Cores: 40, Instances: 1}}}}}}
	if err := d.Reserve(context.Background(), plan); err != nil {
		t.Fatal(err)
	}
	if err := guard(gpu, node); err == nil || !strings.Contains(err.Error(), "RV=2") {
		t.Fatalf("view cached or GPU granted: %v", err)
	}
	if reads != 2 {
		t.Fatalf("want 2 independent reads, got %d", reads)
	}
	s := &framework.Session{}
	s.AddAllocationVetoFn("durable", guard)
	if !framework.IsAllocationVeto(s.CheckAllocationVeto(gpu, node)) {
		t.Fatal("fresh Session failed to enforce barrier")
	}
}

func TestReservationBarrierErrorsAndConfigurationFailClosed(t *testing.T) {
	gpu := &api.TaskInfo{Pod: &v1.Pod{Spec: v1.PodSpec{Containers: []v1.Container{{Resources: v1.ResourceRequirements{Requests: v1.ResourceList{v1.ResourceName("volcano.sh/vgpu-cores"): resource.MustParse("1")}}}}}}}
	node := &api.NodeInfo{Name: "node"}
	for _, view := range []reservationViewFunc{nil, func(context.Context) (coordination.ReservationView, error) {
		return coordination.ReservationView{}, fmt.Errorf("API down")
	}, func(context.Context) (coordination.ReservationView, error) {
		return coordination.ReservationView{Quarantined: true}, nil
	}} {
		if err := newReservationBarrier(view)(gpu, node); err == nil {
			t.Fatal("missing, unavailable or quarantined ledger granted GPU")
		}
	}
	if reservationBarrierRequested(nil, nil) {
		t.Fatal("barrier must be default off")
	}
	if !reservationBarrierRequested(nil, framework.Arguments{"reservation-barrier": "malformed"}) {
		t.Fatal("malformed opt-in must fail closed")
	}
	tiers := []conf.Tier{{Plugins: []conf.PluginOption{{Name: PluginName, Arguments: framework.Arguments{"reservation-barrier": true}}}}}
	if !reservationBarrierRequested(tiers, framework.Arguments{"reservation-barrier": false}) {
		t.Fatal("duplicate configuration weakened barrier")
	}
}
