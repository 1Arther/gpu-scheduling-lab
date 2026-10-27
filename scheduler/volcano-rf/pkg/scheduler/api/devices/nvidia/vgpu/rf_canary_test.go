package vgpu

import (
	"context"
	"net"
	"os"
	"sync"
	"sync/atomic"
	"testing"

	"google.golang.org/grpc"
	v1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes/fake"

	dqngrpc "volcano.sh/volcano/pkg/scheduler/api/devices/nvidia/vgpu/dqngrpc"
)

func canaryTestConfig(t *testing.T) {
	rfTestConfig(t)
	old, ns, policy := RFCanaryEnabled, RFCanaryNamespace, GPUSelectPolicy
	RFCanaryEnabled, RFCanaryNamespace, GPUSelectPolicy = true, "gpu-rf-tests", BinpackPolicy
	t.Cleanup(func() { RFCanaryEnabled, RFCanaryNamespace, GPUSelectPolicy = old, ns, policy })
}

func canaryPod(name, kind string) *v1.Pod {
	p := rfTestPod(name, kind, 4096, 20)
	p.Namespace = "gpu-rf-tests"
	p.Labels["gpu-lab/experiment"] = "round7-rf"
	return p
}

func TestRFCanaryGateAndDisable(t *testing.T) {
	canaryTestConfig(t)
	p := canaryPod("test", "training")
	if !RFForPod(p) || !RFActive() {
		t.Fatal("canary not selected")
	}
	bad := p.DeepCopy()
	bad.Labels[RFWorkloadLabel] = ""
	if !RFForPod(bad) {
		t.Fatal("invalid label bypassed validation via legacy route")
	}
	if _, err := rfPodRequest(bad); err == nil {
		t.Fatal("empty workload label must be rejected")
	}
	for _, alter := range []func(*v1.Pod){
		func(p *v1.Pod) { p.Namespace = "default" },
		func(p *v1.Pod) { delete(p.Labels, "gpu-lab/experiment") },
		func(p *v1.Pod) { delete(p.Labels, RFWorkloadLabel) },
	} {
		q := p.DeepCopy()
		alter(q)
		if RFForPod(q) {
			t.Fatal("unrelated Pod routed to RF")
		}
	}
	RFCanaryEnabled = false
	if RFForPod(p) || RFActive() {
		t.Fatal("default legacy policy changed")
	}
}

type canaryDQN struct {
	dqngrpc.UnimplementedDQNSchedulerServer
	calls atomic.Int32
}

func (s *canaryDQN) ScheduleJob(_ context.Context, r *dqngrpc.ScheduleJobRequest) (*dqngrpc.ScheduleJobResponse, error) {
	s.calls.Add(1)
	if !rfLedgerMu.TryLock() {
		panic("DQN RPC holds shared ledger lock")
	}
	rfLedgerMu.Unlock()
	allocations := []*dqngrpc.PodAllocation{}
	for _, p := range r.Pods {
		allocations = append(allocations, &dqngrpc.PodAllocation{PodNamespace: p.PodNamespace, PodName: p.PodName, Success: true, SelectedIndexes: []int32{1}})
	}
	return &dqngrpc.ScheduleJobResponse{Allocations: allocations}, nil
}

func TestRFCanaryDQNAndRFShareNativeLedger(t *testing.T) {
	canaryTestConfig(t)
	GPUSelectPolicy = DQNJobPolicy
	dqn := &canaryDQN{}
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	dqngrpc.RegisterDQNSchedulerServer(server, dqn)
	go server.Serve(l)
	endpoint, kube := DQNGRPCEndpoint, dqnKubeClient
	resetDQNClient()
	DQNGRPCEndpoint = l.Addr().String()
	p := canaryPod("experiment", "training")
	q := rfTestPod("business", "inference", 4096, 20) // no canary namespace/label
	client := fake.NewSimpleClientset(p.DeepCopy(), q.DeepCopy())
	dqnKubeClient = client
	dqnJobCacheMu.Lock()
	dqnJobCache = map[string]dqnJobCacheEntry{}
	dqnJobCacheMu.Unlock()
	t.Cleanup(func() { resetDQNClient(); server.Stop(); l.Close(); DQNGRPCEndpoint, dqnKubeClient = endpoint, kube })
	gs := rfTestNode("shared", "NVIDIA-GeForce RTX 4090", 2, 24564)
	calls := rfMockServer(t, rfMockResponse)
	for _, item := range []*v1.Pod{q, p} {
		if _, _, err := gs.FilterNode(item, "binpack"); err != nil {
			t.Fatal(err)
		}
		_ = gs.ScoreNode(item, "binpack")
		if err := gs.Allocate(client, item); err != nil {
			t.Fatal(err)
		}
	}
	if dqn.calls.Load() != 1 || calls.Load() != 1 {
		t.Fatalf("wrong dispatch DQN=%d RF=%d", dqn.calls.Load(), calls.Load())
	}
	if gs.Device[1].UsedMem != 4096 || gs.Device[0].UsedMem != 4096 {
		t.Fatal("legacy/RF UUID or shared accounting changed")
	}
	for _, item := range []*v1.Pod{q, p} {
		saved, e := client.CoreV1().Pods(item.Namespace).Get(context.Background(), item.Name, metav1.GetOptions{})
		if e != nil {
			t.Fatal(e)
		}
		gs.AddResource(saved) // native informer replay must not reserve twice
		if gs.Device[0].UsedNum != 1 || gs.Device[1].UsedNum != 1 {
			t.Fatal("duplicate informer accounting")
		}
		_ = saved
	}
	for _, item := range []*v1.Pod{q, p} {
		gs.SubResource(item)
	}
	rfAssertEmpty(t, gs)
}

func TestRFCanaryConcurrentPoliciesCannotOverallocate(t *testing.T) {
	canaryTestConfig(t)
	rfMockServer(t, rfMockResponse)
	gs := rfTestNode("one-slot", "NVIDIA-L40", 1, 4096)
	ps := []*v1.Pod{canaryPod("rf", "training"), rfTestPod("legacy", "", 4096, 20)}
	objects := []runtime.Object{ps[0].DeepCopy(), ps[1].DeepCopy()}
	client := fake.NewSimpleClientset(objects...)
	var wg sync.WaitGroup
	var succeeded atomic.Int32
	for _, p := range ps {
		wg.Add(1)
		go func(p *v1.Pod) {
			defer wg.Done()
			if gs.Allocate(client, p) == nil {
				succeeded.Add(1)
			}
		}(p)
	}
	wg.Wait()
	if succeeded.Load() != 1 || gs.Device[0].UsedMem != 4096 || gs.Device[0].UsedNum != 1 {
		t.Fatal("shared policies overallocated")
	}
	for _, p := range ps {
		if rfAlreadyAllocated(gs, p) {
			gs.SubResource(p)
		}
	}
	rfAssertEmpty(t, gs)
}

func TestRFCanaryLiveFrozenModel(t *testing.T) {
	endpoint := os.Getenv("RF_LIVE_ENDPOINT")
	if endpoint == "" {
		t.Skip("requires isolated Python service")
	}
	canaryTestConfig(t)
	RFEndpoint = endpoint
	gs := rfTestNode("live-canary", "NVIDIA-GeForce RTX 4090", 2, 24564)
	p := canaryPod("live-a", "training")
	q := canaryPod("live-b", "inference")
	client := fake.NewSimpleClientset(p.DeepCopy(), q.DeepCopy())
	initial := rfSnapshotToken(gs)
	for _, item := range []*v1.Pod{p, q} {
		plan, err := rfPlanFor(gs, item)
		if err != nil || plan.Fallback != "" {
			t.Fatalf("no real RF advice: %+v %v", plan, err)
		}
		if _, _, err = gs.FilterNode(item, "binpack"); err != nil {
			t.Fatal(err)
		}
		_ = gs.ScoreNode(item, "binpack")
		if err = gs.Allocate(client, item); err != nil {
			t.Fatal(err)
		}
		saved, err := client.CoreV1().Pods(item.Namespace).Get(context.Background(), item.Name, metav1.GetOptions{})
		if err != nil {
			t.Fatal(err)
		}
		if saved.Annotations[AssignedIDsAnnotations] != encodePodDevices(plan.Devices) {
			t.Fatal("RF/final Allocate UUID mismatch")
		}
		if rfSnapshotToken(gs) == initial {
			t.Fatal("logical state not updated")
		}
	}
	gs.SubResource(p)
	gs.SubResource(q)
	rfAssertEmpty(t, gs)
	if rfSnapshotToken(gs) != initial {
		t.Fatal("state not restored")
	}
	if _, err := rfPlanFor(gs, canaryPod("after-release", "training")); err != nil {
		t.Fatal(err)
	}
}
