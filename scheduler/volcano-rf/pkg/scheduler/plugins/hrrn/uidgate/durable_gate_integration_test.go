package uidgate

import (
	"context"
	"fmt"
	"reflect"
	"strconv"
	"sync"
	"testing"
	"time"

	v1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes/fake"
	clienttesting "k8s.io/client-go/testing"
	"volcano.sh/volcano/pkg/scheduler/plugins/hrrn/coordination"
)

// This local integration uses the real DurableCoordinator with an explicitly
// versioned fake ConfigMap API. The gate Journal, observer, binding, kubelet
// checkpoint and device plugin are all simulations. The journal is NOT
// durable, and none of this proves real cross-process fencing or binding.
func durableGateFakeClient() *fake.Clientset {
	client := fake.NewSimpleClientset()
	var mu sync.Mutex
	var saved *v1.ConfigMap
	var revision int
	resource := schema.GroupResource{Resource: "configmaps"}
	client.PrependReactor("*", "configmaps", func(a clienttesting.Action) (bool, runtime.Object, error) {
		mu.Lock()
		defer mu.Unlock()
		switch a.GetVerb() {
		case "create":
			cm := a.(clienttesting.CreateAction).GetObject().(*v1.ConfigMap)
			if saved != nil {
				return true, nil, apierrors.NewAlreadyExists(resource, cm.Name)
			}
			revision++
			saved = cm.DeepCopy()
			saved.UID, saved.ResourceVersion = types.UID("fake-ledger-uid"), strconv.Itoa(revision)
			return true, saved.DeepCopy(), nil
		case "get":
			name := a.(clienttesting.GetAction).GetName()
			if saved == nil || saved.Name != name || saved.Namespace != a.GetNamespace() {
				return true, nil, apierrors.NewNotFound(resource, name)
			}
			return true, saved.DeepCopy(), nil
		case "update":
			cm := a.(clienttesting.UpdateAction).GetObject().(*v1.ConfigMap)
			if saved == nil || cm.Name != saved.Name || cm.Namespace != saved.Namespace || cm.UID != saved.UID || cm.ResourceVersion != saved.ResourceVersion {
				return true, nil, apierrors.NewConflict(resource, cm.Name, fmt.Errorf("simulated UID/resourceVersion conflict"))
			}
			revision++
			saved = cm.DeepCopy()
			saved.ResourceVersion = strconv.Itoa(revision)
			return true, saved.DeepCopy(), nil
		default:
			return true, nil, fmt.Errorf("unexpected fake ConfigMap operation %s", a.GetVerb())
		}
	})
	return client
}

type durableGateFixture struct {
	client   *fake.Clientset
	store    *coordination.DurableCoordinator
	key      coordination.DeviceKey
	job      coordination.JobPlan
	gate     *Gate
	journal  *journal // explicitly an in-memory synthetic journal, not durable
	observer *observer
}

func newDurableGateFixture(t *testing.T) *durableGateFixture {
	t.Helper()
	ctx := context.Background()
	client := durableGateFakeClient()
	key := coordination.DeviceKey{Node: "node", GPU: "GPU-A"}
	capacity := map[coordination.DeviceKey]coordination.Resources{key: {Memory: 16384, Cores: 100, Instances: 10}}
	store, err := coordination.CreateDurable(ctx, client, "test-ns", "ledger", capacity, map[coordination.DeviceKey]coordination.Resources{key: {}})
	if err != nil {
		t.Fatal(err)
	}
	alloc := coordination.Allocation{Container: "worker", Device: key, Resources: coordination.Resources{Memory: 4096, Cores: 40, Instances: 1}}
	job := coordination.JobPlan{UID: "job-a", Pods: []coordination.PodPlan{
		{UID: "pod-a-uid", Node: key.Node, Allocations: []coordination.Allocation{alloc}},
		{UID: "pod-b-uid", Node: key.Node, Allocations: []coordination.Allocation{alloc}},
	}}
	if err := store.Reserve(ctx, job); err != nil {
		t.Fatal(err)
	}
	now := time.Unix(1770000000, 0)
	j := &journal{}
	o := &observer{observed: now}
	gate := &Gate{Journal: j, Observer: o, Now: func() time.Time { return now }, MaxAge: 10 * time.Second}
	return &durableGateFixture{client: client, store: store, key: key, job: job, gate: gate, journal: j, observer: o}
}

// This simulates a published and bound Pod; no Kubernetes Binding API is used.
func (f *durableGateFixture) begin(t *testing.T, pod coordination.PodPlan) (coordination.Ticket, Claim) {
	t.Helper()
	ctx := context.Background()
	ticket, err := f.store.Begin(ctx, f.job.UID, pod.UID, pod.Node)
	if err != nil {
		t.Fatal(err)
	}
	if err = f.store.Published(ctx, ticket); err != nil {
		t.Fatal(err)
	}
	if err = f.store.RecordBound(ctx, ticket, coordination.BindingObservation{PodUID: pod.UID, Node: pod.Node, ObservationID: "synthetic-bind"}); err != nil {
		t.Fatal(err)
	}
	c := Claim{JobUID: f.job.UID, PodUID: pod.UID, Node: pod.Node, Container: "worker", Resource: "volcano.sh/vgpu-number", Attempt: coordination.DurableAttemptID(ticket), Allocations: append([]coordination.Allocation(nil), pod.Allocations...)}
	return ticket, c
}

// A synthetic observer supplies quiescence and binding claims after the gate's
// return. Its booleans are test inputs, not evidence from a real node.
func (f *durableGateFixture) recordSyntheticConsumption(t *testing.T, ticket coordination.Ticket, pod coordination.PodPlan, prepared Prepared) {
	t.Helper()
	if prepared.Claim.PodUID != pod.UID || prepared.Claim.Attempt != coordination.DurableAttemptID(ticket) || !reflect.DeepEqual(prepared.Claim.Allocations, pod.Allocations) {
		t.Fatal("confirmed gate attempt and reservation differ")
	}
	err := f.store.RecordConsumed(context.Background(), ticket, coordination.ConsumptionObservation{
		PodUID: pod.UID, Node: pod.Node, ObservationID: "synthetic-prestart-and-node-quiescence", Allocations: pod.Allocations,
		BoundUIDVerified: true, PluginResponseVerified: true, KubeletCheckpointVerified: true, NoInFlightAllocate: true, NodeLockUnheld: true,
	})
	if err != nil {
		t.Fatal(err)
	}
}

func TestDurableGateSequentialTwoPodSyntheticHandoff(t *testing.T) {
	f := newDurableGateFixture(t)
	ctx := context.Background()
	state, err := f.store.Snapshot(ctx)
	if err != nil || state.Used[f.key].Cores != 80 || state.Pods["pod-a-uid"] != coordination.Reserved || state.Pods["pod-b-uid"] != coordination.Reserved {
		t.Fatalf("whole two-Pod plan not reserved: %+v %v", state, err)
	}
	first, firstClaim := f.begin(t, f.job.Pods[0])
	firstResponse, err := f.gate.PrepareAllocate(ctx, firstClaim, []string{"slot-1"}, baseResponse())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.store.Begin(ctx, f.job.UID, f.job.Pods[1].UID, f.key.Node); err == nil {
		t.Fatal("same node accepted second Pod before first confirmation")
	}
	f.observer.raw = checkpoint(t, entry(firstClaim.PodUID, "worker", []string{"slot-1"}, firstResponse))
	p, err := f.gate.CheckPreStart(ctx, f.key.Node, []string{"slot-1"})
	if err != nil {
		t.Fatal(err)
	}
	f.recordSyntheticConsumption(t, first, f.job.Pods[0], p)
	// The memory-only journal is cleared by the fake peer after the first
	// synthetic handoff; this does not implement durable journal recovery.
	f.journal.prepared = nil
	second, secondClaim := f.begin(t, f.job.Pods[1])
	secondResponse, err := f.gate.PrepareAllocate(ctx, secondClaim, []string{"slot-2"}, baseResponse())
	if err != nil {
		t.Fatal(err)
	}
	f.observer.raw = checkpoint(t, entry(firstClaim.PodUID, "worker", []string{"slot-1"}, firstResponse), entry(secondClaim.PodUID, "worker", []string{"slot-2"}, secondResponse))
	p, err = f.gate.CheckPreStart(ctx, f.key.Node, []string{"slot-2"})
	if err != nil {
		t.Fatal(err)
	}
	f.recordSyntheticConsumption(t, second, f.job.Pods[1], p)
	state, err = f.store.Snapshot(ctx)
	if err != nil || state.Used[f.key].Cores != 80 || state.Pods[first.PodUID] != coordination.Consumed || state.Pods[second.PodUID] != coordination.Consumed {
		t.Fatalf("both synthetic handoffs should retain reservation: %+v %v", state, err)
	}
	for _, ticket := range []coordination.Ticket{first, second} {
		if err = f.store.Resolve(ctx, ticket, coordination.ReleaseObservation{PodUID: ticket.PodUID, Node: ticket.Node, ObservationID: "synthetic-release", PodTerminalOrAbsent: true, DeviceReleaseVerified: true, NoInFlightAllocate: true, NodeLockUnheld: true}); err != nil {
			t.Fatal(err)
		}
	}
	state, err = f.store.Snapshot(ctx)
	if err != nil || state.Used[f.key] != (coordination.Resources{}) || len(state.Slots) != 0 {
		t.Fatalf("synthetic releases did not clear ledger: %+v %v", state, err)
	}
}

func TestDurableGateWrongPodStaleRecordAndRestartKeepReservation(t *testing.T) {
	f := newDurableGateFixture(t)
	ctx := context.Background()
	ticket, c := f.begin(t, f.job.Pods[0])
	response, err := f.gate.PrepareAllocate(ctx, c, []string{"slot-1"}, baseResponse())
	if err != nil {
		t.Fatal(err)
	}
	// Simulate kubelet giving A's response to B; the gate cannot authorize A.
	f.observer.raw = checkpoint(t, entry("pod-b-uid", "worker", []string{"slot-1"}, response))
	if _, err = f.gate.CheckPreStart(ctx, f.key.Node, []string{"slot-1"}); err == nil {
		t.Fatal("wrong Pod accepted")
	}
	// A historical record has a real nonce for the same Pod and device ID,
	// but belongs to a different attempt and cannot confirm the current one.
	previous := c
	previous.Attempt = "synthetic-earlier-attempt"
	previousGate := &Gate{Journal: &journal{}}
	oldResponse, err := previousGate.PrepareAllocate(ctx, previous, []string{"slot-1"}, baseResponse())
	if err != nil {
		t.Fatal(err)
	}
	f.observer.raw = checkpoint(t, entry(c.PodUID, c.Container, []string{"slot-1"}, oldResponse))
	if _, err = f.gate.CheckPreStart(ctx, f.key.Node, []string{"slot-1"}); err == nil {
		t.Fatal("old record accepted")
	}
	state, err := f.store.Snapshot(ctx)
	if err != nil || state.Used[f.key].Cores != 80 || state.Pods[c.PodUID] != coordination.Bound || state.Slots[f.key.Node] != c.PodUID || f.journal.confirms != 0 {
		t.Fatalf("invalid evidence released or changed reservation: %+v %v", state, err)
	}
	old := f.store
	restarted, err := coordination.TakeoverDurable(ctx, f.client, "test-ns", "ledger", old.UID(), old.Epoch())
	if err != nil {
		t.Fatal(err)
	}
	state, err = restarted.Snapshot(ctx)
	if err != nil || state.Used[f.key].Cores != 80 || state.Pods[c.PodUID] != coordination.Uncertain || state.Pods[f.job.Pods[1].UID] != coordination.Reserved {
		t.Fatalf("restart failed to preserve unknown reservation: %+v %v", state, err)
	}
	if _, err = restarted.Begin(ctx, f.job.UID, f.job.Pods[1].UID, f.key.Node); err == nil {
		t.Fatal("restart quarantine did not block same node")
	}
	if err = old.Published(ctx, ticket); err == nil {
		t.Fatal("old epoch still mutates ledger")
	}
	// A restarted gate has no record: the test journal is memory-only. This
	// intentionally demonstrates the missing persistent-journal integration.
	restartedGate := &Gate{Journal: &journal{}, Observer: f.observer, Now: f.gate.Now, MaxAge: f.gate.MaxAge}
	if _, err = restartedGate.CheckPreStart(ctx, f.key.Node, []string{"slot-1"}); err == nil {
		t.Fatal("empty restarted journal incorrectly confirms old nonce")
	}
}
