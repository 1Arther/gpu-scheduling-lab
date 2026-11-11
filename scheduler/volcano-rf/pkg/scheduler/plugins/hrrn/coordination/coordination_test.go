package coordination

import (
	"errors"
	"fmt"
	"math"
	"reflect"
	"sync"
	"testing"
)

var gpu0 = DeviceKey{Node: "node-0", GPU: "GPU-0"}
var gpu1 = DeviceKey{Node: "node-0", GPU: "GPU-1"}
var gpu2 = DeviceKey{Node: "node-1", GPU: "GPU-2"}

func allocation(container string, device DeviceKey, memory, cores, instances int64) Allocation {
	return Allocation{Container: container, Device: device, Resources: Resources{memory, cores, instances}}
}

func plan(uid, node string, allocations ...Allocation) PodPlan {
	return PodPlan{UID: uid, Node: node, Allocations: allocations}
}

func mustCoordinator(t *testing.T, capacities, existing map[DeviceKey]Resources) *Coordinator {
	t.Helper()
	c, err := New(capacities, existing)
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func mustReserve(t *testing.T, c *Coordinator, job string, pods ...PodPlan) {
	t.Helper()
	if err := c.Reserve(JobPlan{UID: job, Pods: pods}); err != nil {
		t.Fatal(err)
	}
}

func mustBegin(t *testing.T, c *Coordinator, job string, pod PodPlan) Ticket {
	t.Helper()
	ticket, err := c.Begin(job, pod.UID, pod.Node)
	if err != nil {
		t.Fatal(err)
	}
	return ticket
}

func bind(t *testing.T, c *Coordinator, ticket Ticket) {
	t.Helper()
	if err := c.Published(ticket); err != nil {
		t.Fatal(err)
	}
	if err := c.RecordBound(ticket, BindingObservation{PodUID: ticket.PodUID, Node: ticket.Node, ObservationID: "API-bound-UID"}); err != nil {
		t.Fatal(err)
	}
}

func consumed(t *testing.T, c *Coordinator, ticket Ticket, pod PodPlan) {
	t.Helper()
	obs := ConsumptionObservation{PodUID: ticket.PodUID, Node: ticket.Node, ObservationID: "plugin-plus-checkpoint-UID",
		Allocations: pod.Allocations, BoundUIDVerified: true, PluginResponseVerified: true, KubeletCheckpointVerified: true,
		NoInFlightAllocate: true, NodeLockUnheld: true}
	if err := c.RecordConsumed(ticket, obs); err != nil {
		t.Fatal(err)
	}
}

func releaseProof(ticket Ticket) ReleaseObservation {
	return ReleaseObservation{PodUID: ticket.PodUID, Node: ticket.Node, ObservationID: "verified-release-UID",
		PodTerminalOrAbsent: true, DeviceReleaseVerified: true, NoInFlightAllocate: true, NodeLockUnheld: true}
}

func mustReject(t *testing.T, err error) {
	t.Helper()
	if !errors.Is(err, ErrRejected) {
		t.Fatalf("expected rejection, got %v", err)
	}
}

func TestCompleteReservationFailureAllowsSmallJobAndIsSideEffectFree(t *testing.T) {
	c := mustCoordinator(t, map[DeviceKey]Resources{gpu0: {Memory: 100, Cores: 100, Instances: 10}},
		map[DeviceKey]Resources{gpu0: {Memory: 30, Cores: 30, Instances: 1}})
	large0 := plan("large-0", "node-0", allocation("c0", gpu0, 40, 40, 1))
	large1 := plan("large-1", "node-0", allocation("c0", gpu0, 40, 40, 1))
	before := c.Snapshot()
	mustReject(t, c.Reserve(JobPlan{UID: "large", Pods: []PodPlan{large0, large1}}))
	if after := c.Snapshot(); !reflect.DeepEqual(after, before) {
		t.Fatalf("failed plan changed state: %#v", after)
	}
	small := plan("small-uid", "node-0", allocation("c", gpu0, 60, 60, 1))
	mustReserve(t, c, "small", small)
	if got := c.Snapshot().Used[gpu0]; got != (Resources{90, 90, 2}) {
		t.Fatal(got)
	}
	ticket := mustBegin(t, c, "small", small)
	bind(t, c, ticket)
	consumed(t, c, ticket, small)
	if err := c.Resolve(ticket, releaseProof(ticket)); err != nil {
		t.Fatal(err)
	}
	if got := c.Snapshot().Used[gpu0]; got != (Resources{30, 30, 1}) {
		t.Fatal(got)
	}
	// Existing allocations are deliberately immutable in this snapshot. A new
	// externally reconciled empty snapshot is required after background release.
	empty := mustCoordinator(t, map[DeviceKey]Resources{gpu0: {100, 100, 10}}, nil)
	mustReserve(t, empty, "large", large0, large1)
	if got := empty.Snapshot().Used[gpu0]; got != (Resources{80, 80, 2}) {
		t.Fatal(got)
	}
}

func TestSameNodeSerialHandshakeAndIndependentGPUReservation(t *testing.T) {
	c := mustCoordinator(t, map[DeviceKey]Resources{gpu0: {100, 100, 4}, gpu1: {100, 100, 4}}, nil)
	a := plan("a", "node-0", allocation("trainer", gpu0, 40, 40, 1), allocation("trainer", gpu1, 20, 20, 1))
	b := plan("b", "node-0", allocation("worker", gpu0, 40, 40, 1))
	mustReserve(t, c, "job", a, b)
	ta := mustBegin(t, c, "job", a)
	_, err := c.Begin("job", "b", "node-0")
	mustReject(t, err)
	bind(t, c, ta)
	// An annotation-only or partial signal does not establish actual consumption.
	bad := ConsumptionObservation{PodUID: "a", Node: "node-0", ObservationID: "bind-phase-success"}
	mustReject(t, c.RecordConsumed(ta, bad))
	bad = ConsumptionObservation{PodUID: "a", Node: "node-0", ObservationID: "bad-uuid", Allocations: []Allocation{allocation("trainer", gpu0, 40, 40, 1)},
		BoundUIDVerified: true, PluginResponseVerified: true, KubeletCheckpointVerified: true, NoInFlightAllocate: true, NodeLockUnheld: true}
	mustReject(t, c.RecordConsumed(ta, bad))
	if c.Snapshot().Slots["node-0"] != "a" {
		t.Fatal("failed proof released node slot")
	}
	consumed(t, c, ta, a)
	tb := mustBegin(t, c, "job", b)
	bind(t, c, tb)
	consumed(t, c, tb, b)
	if got := c.Snapshot().Used[gpu0]; got != (Resources{80, 80, 2}) {
		t.Fatal(got)
	}
	if err := c.Resolve(ta, releaseProof(ta)); err != nil {
		t.Fatal(err)
	}
	if got := c.Snapshot().Used[gpu0]; got != (Resources{40, 40, 1}) {
		t.Fatal(got)
	}
	if err := c.Resolve(tb, releaseProof(tb)); err != nil {
		t.Fatal(err)
	}
	if got := c.Snapshot(); got.Used[gpu0] != (Resources{}) || got.Used[gpu1] != (Resources{}) {
		t.Fatal(got)
	}
}

func TestPartialProofMismatchCannotAdvanceOrFree(t *testing.T) {
	c := mustCoordinator(t, map[DeviceKey]Resources{gpu0: {100, 100, 4}}, nil)
	pod := plan("uid", "node-0", allocation("c", gpu0, 40, 40, 1))
	mustReserve(t, c, "job", pod)
	ticket := mustBegin(t, c, "job", pod)
	mustReject(t, c.RecordBound(ticket, BindingObservation{PodUID: "uid", Node: "node-0", ObservationID: "out-of-order"}))
	if err := c.Published(ticket); err != nil {
		t.Fatal(err)
	}
	for _, o := range []BindingObservation{{PodUID: "old-uid", Node: "node-0", ObservationID: "id"}, {PodUID: "uid", Node: "node-1", ObservationID: "id"}, {PodUID: "uid", Node: "node-0"}} {
		mustReject(t, c.RecordBound(ticket, o))
	}
	if err := c.RecordBound(ticket, BindingObservation{PodUID: "uid", Node: "node-0", ObservationID: "bound"}); err != nil {
		t.Fatal(err)
	}
	good := ConsumptionObservation{PodUID: "uid", Node: "node-0", ObservationID: "all", Allocations: pod.Allocations,
		BoundUIDVerified: true, PluginResponseVerified: true, KubeletCheckpointVerified: true, NoInFlightAllocate: true, NodeLockUnheld: true}
	for name, mutate := range map[string]func(*ConsumptionObservation){
		"old pod":             func(o *ConsumptionObservation) { o.PodUID = "old" },
		"wrong node":          func(o *ConsumptionObservation) { o.Node = "other" },
		"missing evidence":    func(o *ConsumptionObservation) { o.ObservationID = "" },
		"binding not checked": func(o *ConsumptionObservation) { o.BoundUIDVerified = false },
		"plugin absent":       func(o *ConsumptionObservation) { o.PluginResponseVerified = false },
		"checkpoint absent":   func(o *ConsumptionObservation) { o.KubeletCheckpointVerified = false },
		"allocate in flight":  func(o *ConsumptionObservation) { o.NoInFlightAllocate = false },
		"node lock held":      func(o *ConsumptionObservation) { o.NodeLockUnheld = false },
		"wrong gpu":           func(o *ConsumptionObservation) { o.Allocations = []Allocation{allocation("c", gpu1, 40, 40, 1)} },
		"wrong quota":         func(o *ConsumptionObservation) { o.Allocations = []Allocation{allocation("c", gpu0, 40, 39, 1)} },
		"wrong container":     func(o *ConsumptionObservation) { o.Allocations = []Allocation{allocation("other", gpu0, 40, 40, 1)} },
	} {
		t.Run(name, func(t *testing.T) {
			bad := good
			mutate(&bad)
			mustReject(t, c.RecordConsumed(ticket, bad))
			if c.Snapshot().Slots["node-0"] != "uid" {
				t.Fatal("prematurely released slot")
			}
		})
	}
	if err := c.RecordConsumed(ticket, good); err != nil {
		t.Fatal(err)
	}
	for name, mutate := range map[string]func(*ReleaseObservation){
		"old UID": func(o *ReleaseObservation) { o.PodUID = "old" }, "node": func(o *ReleaseObservation) { o.Node = "other" },
		"no observation": func(o *ReleaseObservation) { o.ObservationID = "" }, "nonterminal": func(o *ReleaseObservation) { o.PodTerminalOrAbsent = false },
		"not released": func(o *ReleaseObservation) { o.DeviceReleaseVerified = false }, "in flight": func(o *ReleaseObservation) { o.NoInFlightAllocate = false },
		"lock held": func(o *ReleaseObservation) { o.NodeLockUnheld = false },
	} {
		t.Run(name, func(t *testing.T) {
			bad := releaseProof(ticket)
			mutate(&bad)
			mustReject(t, c.Resolve(ticket, bad))
			if c.Snapshot().Used[gpu0] != (Resources{40, 40, 1}) {
				t.Fatal("premature release")
			}
		})
	}
	if err := c.Resolve(ticket, releaseProof(ticket)); err != nil {
		t.Fatal(err)
	}
	mustReject(t, c.Resolve(ticket, releaseProof(ticket)))
}

func TestCancellationAfterPossibleExternalEffectsFailsClosed(t *testing.T) {
	for _, initial := range []Stage{Publishing, Published, Bound, Consumed} {
		t.Run(string(initial), func(t *testing.T) {
			c := mustCoordinator(t, map[DeviceKey]Resources{gpu0: {100, 100, 4}}, nil)
			a := plan("a", "node-0", allocation("c", gpu0, 40, 40, 1))
			b := plan("b", "node-0", allocation("c", gpu0, 20, 20, 1))
			mustReserve(t, c, "job", a, b)
			ta := mustBegin(t, c, "job", a)
			if initial != Publishing {
				if err := c.Published(ta); err != nil {
					t.Fatal(err)
				}
			}
			if initial == Bound || initial == Consumed {
				if err := c.RecordBound(ta, BindingObservation{PodUID: "a", Node: "node-0", ObservationID: "bound"}); err != nil {
					t.Fatal(err)
				}
			}
			if initial == Consumed {
				consumed(t, c, ta, a)
			}
			state, err := c.Cancel("job", "a", "node-0")
			if err != nil || state != Uncertain {
				t.Fatalf("%v %v", state, err)
			}
			_, err = c.Begin("job", "b", "node-0")
			mustReject(t, err)
			if got := c.Snapshot(); got.Used[gpu0] != (Resources{60, 60, 2}) || !reflect.DeepEqual(got.Blocked["node-0"], []string{"a"}) {
				t.Fatal(got)
			}
			bad := releaseProof(ta)
			bad.DeviceReleaseVerified = false
			mustReject(t, c.Resolve(ta, bad))
			if err := c.Resolve(ta, releaseProof(ta)); err != nil {
				t.Fatal(err)
			}
			if got := c.Snapshot(); got.Used[gpu0] != (Resources{20, 20, 1}) {
				t.Fatal(got)
			}
			_ = mustBegin(t, c, "job", b)
		})
	}
}

func TestJobCancellationNeverPretendsBoundMemberRolledBack(t *testing.T) {
	c := mustCoordinator(t, map[DeviceKey]Resources{gpu0: {100, 100, 4}}, nil)
	a := plan("a", "node-0", allocation("c", gpu0, 40, 40, 1))
	b := plan("b", "node-0", allocation("c", gpu0, 40, 40, 1))
	mustReserve(t, c, "job", a, b)
	ta := mustBegin(t, c, "job", a)
	bind(t, c, ta)
	stages, err := c.CancelJob("job")
	if err != nil {
		t.Fatal(err)
	}
	if stages["a"] != Uncertain || stages["b"] != Cancelled || c.Snapshot().Used[gpu0] != (Resources{40, 40, 1}) {
		t.Fatalf("%v %#v", stages, c.Snapshot())
	}
	mustReject(t, c.Resolve(ta, ReleaseObservation{PodUID: "a", Node: "node-0", ObservationID: "missing actual release"}))
	if err := c.Resolve(ta, releaseProof(ta)); err != nil {
		t.Fatal(err)
	}
	if c.Snapshot().Used[gpu0] != (Resources{}) {
		t.Fatal("reservation leak")
	}
}

func TestIdentityMismatchRetriesAndOtherEpochCannotRelease(t *testing.T) {
	c := mustCoordinator(t, map[DeviceKey]Resources{gpu0: {100, 100, 4}, gpu2: {100, 100, 4}}, nil)
	a := plan("a", "node-0", allocation("c", gpu0, 40, 40, 1))
	other := plan("b", "node-1", allocation("c", gpu2, 40, 40, 1))
	mustReserve(t, c, "job", a, other)
	_, err := c.Begin("job", "old-pod-uid", "node-0")
	mustReject(t, err)
	_, err = c.Begin("job", "a", "node-1")
	mustReject(t, err)
	ta := mustBegin(t, c, "job", a)
	tb := mustBegin(t, c, "job", other)
	foreign := ta
	foreign.PodUID = "b"
	mustReject(t, c.Published(foreign))
	foreign = ta
	foreign.Node = "node-1"
	mustReject(t, c.Fail(foreign))
	mustReject(t, c.Resolve(ta, releaseProof(ta)))
	bind(t, c, ta)
	consumed(t, c, ta, a)
	if err := c.Resolve(ta, releaseProof(ta)); err != nil {
		t.Fatal(err)
	}
	mustReject(t, c.Published(ta))
	if c.Snapshot().Slots["node-1"] != "b" {
		t.Fatal("released other node")
	}
	otherProcess := mustCoordinator(t, map[DeviceKey]Resources{gpu0: {100, 100, 4}}, nil)
	mustReserve(t, otherProcess, "job", a)
	_ = mustBegin(t, otherProcess, "job", a)
	mustReject(t, otherProcess.Published(ta))
	if err := c.Fail(tb); err != nil {
		t.Fatal(err)
	}
	if c.Snapshot().Pods["b"] != Uncertain {
		t.Fatal("failure did not preserve uncertainty")
	}
}

func TestUnknownBlocksNewWorkWithoutReleasingAnotherTicket(t *testing.T) {
	c := mustCoordinator(t, map[DeviceKey]Resources{gpu0: {100, 100, 4}}, nil)
	a := plan("a", "node-0", allocation("c", gpu0, 20, 20, 1))
	b := plan("b", "node-0", allocation("c", gpu0, 20, 20, 1))
	d := plan("d", "node-0", allocation("c", gpu0, 20, 20, 1))
	mustReserve(t, c, "job", a, b, d)
	ta := mustBegin(t, c, "job", a)
	bind(t, c, ta)
	consumed(t, c, ta, a)
	tb := mustBegin(t, c, "job", b)
	if err := c.Fail(ta); err != nil {
		t.Fatal(err)
	}
	_, err := c.Begin("job", "d", "node-0")
	mustReject(t, err)
	if err := c.Resolve(ta, releaseProof(ta)); err != nil {
		t.Fatal(err)
	}
	if c.Snapshot().Slots["node-0"] != "b" {
		t.Fatal("resolution cleared another Pod's node slot")
	}
	bind(t, c, tb)
	consumed(t, c, tb, b)
	_ = mustBegin(t, c, "job", d)
}

func TestMultiGPUAllDimensionsAndOverflowAreAtomic(t *testing.T) {
	caps := map[DeviceKey]Resources{gpu0: {100, 100, 2}, gpu1: {100, 100, 2}}
	for name, second := range map[string]Resources{"memory": {101, 1, 1}, "cores": {1, 101, 1}, "instances": {1, 1, 3}} {
		t.Run(name, func(t *testing.T) {
			c := mustCoordinator(t, caps, nil)
			before := c.Snapshot()
			p := plan("pod", "node-0", allocation("c", gpu0, 20, 20, 1), Allocation{Container: "c", Device: gpu1, Resources: second})
			mustReject(t, c.Reserve(JobPlan{UID: "multi-gpu", Pods: []PodPlan{p}}))
			if !reflect.DeepEqual(before, c.Snapshot()) {
				t.Fatal("partially reserved")
			}
		})
	}
	c := mustCoordinator(t, map[DeviceKey]Resources{gpu0: {math.MaxInt64, math.MaxInt64, 3}}, nil)
	p := plan("pod", "node-0", allocation("one", gpu0, math.MaxInt64, 1, 1), allocation("two", gpu0, 1, 1, 1))
	mustReject(t, c.Reserve(JobPlan{UID: "overflow", Pods: []PodPlan{p}}))
	if c.Snapshot().Used[gpu0] != (Resources{}) {
		t.Fatal("overflow changed used")
	}
}

func TestDuplicateUIDSnapshotIsolationAndInvalidBaseline(t *testing.T) {
	_, err := New(map[DeviceKey]Resources{gpu0: {100, 100, 4}}, map[DeviceKey]Resources{gpu0: {101, 0, 0}})
	mustReject(t, err)
	_, err = New(map[DeviceKey]Resources{gpu0: {100, 100, 4}}, map[DeviceKey]Resources{gpu1: {1, 0, 0}})
	mustReject(t, err)
	caps := map[DeviceKey]Resources{gpu0: {100, 100, 4}}
	c := mustCoordinator(t, caps, nil)
	caps[gpu0] = Resources{}
	p := plan("uid", "node-0", allocation("c", gpu0, 40, 40, 1))
	mustReserve(t, c, "job", p)
	p.Allocations[0].Resources.Cores = 99
	copyState := c.Snapshot()
	copyState.Used[gpu0] = Resources{}
	if c.Snapshot().Used[gpu0] != (Resources{40, 40, 1}) {
		t.Fatal("external mutation altered state")
	}
	mustReject(t, c.Reserve(JobPlan{UID: "new-job", Pods: []PodPlan{plan("uid", "node-0", allocation("c", gpu0, 1, 1, 1))}}))
	if stage, err := c.Cancel("job", "uid", "node-0"); err != nil || stage != Cancelled {
		t.Fatalf("%v %v", stage, err)
	}
	mustReject(t, c.Reserve(JobPlan{UID: "new-job", Pods: []PodPlan{plan("uid", "node-0", allocation("c", gpu0, 1, 1, 1))}}))
}

func TestConcurrentReserveAndSingleNodeSlot(t *testing.T) {
	c := mustCoordinator(t, map[DeviceKey]Resources{gpu0: {100, 100, 10}}, nil)
	var wg sync.WaitGroup
	for i := 0; i < 40; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			uid := fmt.Sprintf("pod-%d", i)
			_ = c.Reserve(JobPlan{UID: fmt.Sprintf("job-%d", i), Pods: []PodPlan{plan(uid, "node-0", allocation("c", gpu0, 10, 10, 1))}})
		}(i)
	}
	wg.Wait()
	snapshot := c.Snapshot()
	if snapshot.Used[gpu0] != (Resources{100, 100, 10}) || len(snapshot.Pods) != 10 {
		t.Fatalf("resource oversubscription or lost capacity: %#v", snapshot)
	}
	var resultsMu sync.Mutex
	accepted := 0
	for uid := range snapshot.Pods {
		uid := uid
		wg.Add(1)
		go func() {
			defer wg.Done()
			job := c.podUIDs[uid]
			if _, err := c.Begin(job, uid, "node-0"); err == nil {
				resultsMu.Lock()
				accepted++
				resultsMu.Unlock()
			}
		}()
	}
	wg.Wait()
	if accepted != 1 || len(c.Snapshot().Slots) != 1 {
		t.Fatalf("%d nodes selected", accepted)
	}
}
