package coordination

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"sync"
	"testing"

	v1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes/fake"
	clienttesting "k8s.io/client-go/testing"
)

// client-go's fake client does not enforce API server UID/resourceVersion
// semantics. This explicit reactor does; it is still not a real API server.
type durableCASFixture struct {
	mu               sync.Mutex
	cm               *v1.ConfigMap
	rev              int
	conflictNext     bool
	lostResponseNext bool
}

func newDurableCASFake() (*fake.Clientset, *durableCASFixture) {
	client := fake.NewSimpleClientset()
	f := &durableCASFixture{}
	client.PrependReactor("*", "configmaps", func(action clienttesting.Action) (bool, runtime.Object, error) {
		f.mu.Lock()
		defer f.mu.Unlock()
		resource := schema.GroupResource{Resource: "configmaps"}
		switch action.GetVerb() {
		case "create":
			object := action.(clienttesting.CreateAction).GetObject().(*v1.ConfigMap)
			if f.cm != nil {
				return true, nil, apierrors.NewAlreadyExists(resource, object.Name)
			}
			f.rev = 1
			f.cm = object.DeepCopy()
			f.cm.UID = types.UID("ledger-uid-1")
			f.cm.ResourceVersion = "1"
			return true, f.cm.DeepCopy(), nil
		case "get":
			name := action.(clienttesting.GetAction).GetName()
			if f.cm == nil || f.cm.Name != name || f.cm.Namespace != action.GetNamespace() {
				return true, nil, apierrors.NewNotFound(resource, name)
			}
			return true, f.cm.DeepCopy(), nil
		case "update":
			object := action.(clienttesting.UpdateAction).GetObject().(*v1.ConfigMap)
			if f.cm == nil {
				return true, nil, apierrors.NewNotFound(resource, object.Name)
			}
			if f.conflictNext {
				f.conflictNext = false
				return true, nil, apierrors.NewConflict(resource, object.Name, fmt.Errorf("injected conflict"))
			}
			if object.Name != f.cm.Name || object.Namespace != f.cm.Namespace || object.UID != f.cm.UID || object.ResourceVersion != f.cm.ResourceVersion {
				return true, nil, apierrors.NewConflict(resource, object.Name, fmt.Errorf("UID or resourceVersion precondition"))
			}
			f.rev++
			f.cm = object.DeepCopy()
			f.cm.ResourceVersion = fmt.Sprint(f.rev)
			if f.lostResponseNext {
				f.lostResponseNext = false
				return true, nil, apierrors.NewInternalError(fmt.Errorf("response lost after commit"))
			}
			return true, f.cm.DeepCopy(), nil
		default:
			return true, nil, fmt.Errorf("unexpected ConfigMap operation %s", action.GetVerb())
		}
	})
	return client, f
}

func durableFixture(t *testing.T, background Resources) (*DurableCoordinator, *fake.Clientset, *durableCASFixture) {
	t.Helper()
	client, store := newDurableCASFake()
	ledger, err := CreateDurable(context.Background(), client, "test-ns", "ledger", map[DeviceKey]Resources{gpu0: {100, 100, 10}}, map[DeviceKey]Resources{gpu0: background})
	if err != nil {
		t.Fatal(err)
	}
	return ledger, client, store
}

func baselineProof() BaselineEvidence {
	return BaselineEvidence{ObservationID: "independent-baseline", FullInventoryVerified: true, AllActivePodsAndClaimsAccounted: true, ReservationsExcluded: true, NoUnknownInFlightWork: true}
}
func recoveryProof() RecoveryEvidence {
	return RecoveryEvidence{ObservationID: "independent-recovery", PreviousOwnerFenced: true, AllActiveAllocationsAccounted: true, NoUnknownInFlightWork: true}
}

func durableBindAndConsume(t *testing.T, d *DurableCoordinator, ticket Ticket, pod PodPlan) {
	t.Helper()
	ctx := context.Background()
	if err := d.Published(ctx, ticket); err != nil {
		t.Fatal(err)
	}
	if err := d.RecordBound(ctx, ticket, BindingObservation{PodUID: pod.UID, Node: pod.Node, ObservationID: "fake-bound"}); err != nil {
		t.Fatal(err)
	}
	obs := ConsumptionObservation{PodUID: pod.UID, Node: pod.Node, ObservationID: "fake-plugin-and-kubelet", Allocations: pod.Allocations,
		BoundUIDVerified: true, PluginResponseVerified: true, KubeletCheckpointVerified: true, NoInFlightAllocate: true, NodeLockUnheld: true}
	if err := d.RecordConsumed(ctx, ticket, obs); err != nil {
		t.Fatal(err)
	}
}

func TestDurableCompleteReservationBypassAndSequentialRelease(t *testing.T) {
	ctx := context.Background()
	d, client, _ := durableFixture(t, Resources{30, 30, 1})
	a := plan("large-0", "node-0", allocation("c0", gpu0, 40, 40, 1))
	b := plan("large-1", "node-0", allocation("c1", gpu0, 40, 40, 1))
	small := plan("small", "node-0", allocation("c", gpu0, 60, 60, 1))
	before, err := d.Snapshot(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if err := d.Reserve(ctx, JobPlan{UID: "large", Pods: []PodPlan{a, b}}); !errors.Is(err, ErrRejected) {
		t.Fatalf("incomplete capacity accepted: %v", err)
	}
	after, err := d.Snapshot(ctx)
	if err != nil || !reflect.DeepEqual(before, after) {
		t.Fatalf("failed reservation changed ledger: %+v %v", after, err)
	}
	if err := d.Reserve(ctx, JobPlan{UID: "small-job", Pods: []PodPlan{small}}); err != nil {
		t.Fatal(err)
	}
	ticket, err := d.Begin(ctx, "small-job", small.UID, small.Node)
	if err != nil {
		t.Fatal(err)
	}
	if DurableAttemptID(ticket) == "" {
		t.Fatal("attempt identifier missing")
	}
	durableBindAndConsume(t, d, ticket, small)
	if err := d.Resolve(ctx, ticket, releaseProof(ticket)); err != nil {
		t.Fatal(err)
	}
	opened, err := OpenDurable(ctx, client, "test-ns", "ledger", d.UID(), d.Epoch())
	if err != nil {
		t.Fatal(err)
	}
	if got, err := opened.Snapshot(ctx); err != nil || got.Used[gpu0] != (Resources{30, 30, 1}) {
		t.Fatalf("baseline after small release: %+v %v", got, err)
	}
	if err := opened.ReconcileBaseline(ctx, map[DeviceKey]Resources{gpu0: {}}, baselineProof()); err != nil {
		t.Fatal(err)
	}
	if err := opened.Reserve(ctx, JobPlan{UID: "large", Pods: []PodPlan{a, b}}); err != nil {
		t.Fatal(err)
	}
	if got, err := opened.Snapshot(ctx); err != nil || got.Used[gpu0] != (Resources{80, 80, 2}) {
		t.Fatalf("complete durable reservation: %+v %v", got, err)
	}
	ta, err := opened.Begin(ctx, "large", a.UID, a.Node)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := opened.Begin(ctx, "large", b.UID, b.Node); !errors.Is(err, ErrRejected) {
		t.Fatalf("two in-flight Pods on one node: %v", err)
	}
	durableBindAndConsume(t, opened, ta, a)
	tb, err := opened.Begin(ctx, "large", b.UID, b.Node)
	if err != nil {
		t.Fatal(err)
	}
	durableBindAndConsume(t, opened, tb, b)
	if err := opened.Resolve(ctx, ta, releaseProof(ta)); err != nil {
		t.Fatal(err)
	}
	if err := opened.Resolve(ctx, tb, releaseProof(tb)); err != nil {
		t.Fatal(err)
	}
	last, err := OpenDurable(ctx, client, "test-ns", "ledger", d.UID(), d.Epoch())
	if err != nil {
		t.Fatal(err)
	}
	if got, err := last.Snapshot(ctx); err != nil || got.Used[gpu0] != (Resources{}) {
		t.Fatalf("release leaked reservation: %+v %v", got, err)
	}
}

func TestDurableTakeoverQuarantinesAndFencesCooperativeWrites(t *testing.T) {
	ctx := context.Background()
	d, client, _ := durableFixture(t, Resources{})
	a := plan("a", "node-0", allocation("c", gpu0, 40, 40, 1))
	b := plan("b", "node-0", allocation("c", gpu0, 40, 40, 1))
	if err := d.Reserve(ctx, JobPlan{UID: "job", Pods: []PodPlan{a, b}}); err != nil {
		t.Fatal(err)
	}
	old, err := d.Begin(ctx, "job", "a", "node-0")
	if err != nil {
		t.Fatal(err)
	}
	if err := d.Published(ctx, old); err != nil {
		t.Fatal(err)
	}
	next, err := TakeoverDurable(ctx, client, "test-ns", "ledger", d.UID(), d.Epoch())
	if err != nil {
		t.Fatal(err)
	}
	if next.Epoch() == d.Epoch() {
		t.Fatal("epoch was not rotated")
	}
	if err := d.RecordBound(ctx, old, BindingObservation{PodUID: "a", Node: "node-0", ObservationID: "late"}); !errors.Is(err, ErrRejected) {
		t.Fatalf("old cooperative writer accepted: %v", err)
	}
	state, err := next.Snapshot(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if state.Pods["a"] != Uncertain || state.Pods["b"] != Reserved || state.Used[gpu0] != (Resources{80, 80, 2}) || state.Slots["node-0"] != "a" {
		t.Fatalf("takeover lost uncertainty: %+v", state)
	}
	if _, err := next.Begin(ctx, "job", "b", "node-0"); !errors.Is(err, ErrDurableQuarantined) {
		t.Fatalf("recovery quarantine missing: %v", err)
	}
	recovery, err := next.RecoveryTicket(ctx, "job", "a", "node-0")
	if err != nil {
		t.Fatal(err)
	}
	if DurableAttemptID(recovery) == DurableAttemptID(old) {
		t.Fatal("old attempt identity survived takeover")
	}
	if err := next.AcknowledgeRecovery(ctx, RecoveryEvidence{ObservationID: "unsupported"}); !errors.Is(err, ErrRejected) {
		t.Fatalf("unverified recovery accepted: %v", err)
	}
	if err := next.AcknowledgeRecovery(ctx, recoveryProof()); err != nil {
		t.Fatal(err)
	}
	if _, err := next.Begin(ctx, "job", "b", "node-0"); !errors.Is(err, ErrRejected) {
		t.Fatalf("uncertain node slot released: %v", err)
	}
	if err := next.Resolve(ctx, recovery, ReleaseObservation{PodUID: "a", Node: "node-0", ObservationID: "partial"}); !errors.Is(err, ErrRejected) {
		t.Fatalf("incomplete cleanup accepted: %v", err)
	}
	if err := next.Resolve(ctx, recovery, releaseProof(recovery)); err != nil {
		t.Fatal(err)
	}
	if _, err := next.Begin(ctx, "job", "b", "node-0"); err != nil {
		t.Fatal(err)
	}
}

func TestDurableCASConflictAndLostResponseFailClosed(t *testing.T) {
	ctx := context.Background()
	d, client, f := durableFixture(t, Resources{})
	p := plan("p", "node-0", allocation("c", gpu0, 40, 40, 1))
	f.mu.Lock()
	f.conflictNext = true
	f.mu.Unlock()
	if err := d.Reserve(ctx, JobPlan{UID: "job", Pods: []PodPlan{p}}); !errors.Is(err, ErrDurableConflict) {
		t.Fatalf("conflict not surfaced: %v", err)
	}
	if got, err := d.Snapshot(ctx); err != nil || got.Used[gpu0] != (Resources{}) {
		t.Fatalf("conflict leaked: %+v %v", got, err)
	}
	if err := d.Reserve(ctx, JobPlan{UID: "job", Pods: []PodPlan{p}}); err != nil {
		t.Fatal(err)
	}
	f.mu.Lock()
	f.lostResponseNext = true
	f.mu.Unlock()
	ticket, err := d.Begin(ctx, "job", "p", "node-0")
	if !errors.Is(err, ErrDurableUnknown) || DurableAttemptID(ticket) != "" {
		t.Fatalf("unknown update returned usable ticket: %+v %v", ticket, err)
	}
	opened, err := OpenDurable(ctx, client, "test-ns", "ledger", d.UID(), d.Epoch())
	if err != nil {
		t.Fatal(err)
	}
	if got, err := opened.Snapshot(ctx); err != nil || got.Pods["p"] != Publishing || got.Slots["node-0"] != "p" {
		t.Fatalf("committed but lost response not retained: %+v %v", got, err)
	}
	// Reconciliation requires takeover; the failed call never supplies a token
	// permitting an external effect.
	next, err := TakeoverDurable(ctx, client, "test-ns", "ledger", d.UID(), d.Epoch())
	if err != nil {
		t.Fatal(err)
	}
	if got, err := next.Snapshot(ctx); err != nil || got.Pods["p"] != Uncertain || got.Used[gpu0].Cores != 40 {
		t.Fatalf("lost response not quarantined: %+v %v", got, err)
	}
}

func TestDurableCorruptionIdentityAndBaselineRejected(t *testing.T) {
	ctx := context.Background()
	d, client, f := durableFixture(t, Resources{30, 30, 1})
	if err := d.ReconcileBaseline(ctx, map[DeviceKey]Resources{gpu0: {}}, BaselineEvidence{ObservationID: "weak"}); !errors.Is(err, ErrRejected) {
		t.Fatalf("weak baseline accepted: %v", err)
	}
	if err := d.ReconcileBaseline(ctx, map[DeviceKey]Resources{{Node: "other", GPU: "unknown"}: {}}, baselineProof()); !errors.Is(err, ErrRejected) {
		t.Fatalf("wrong baseline devices accepted: %v", err)
	}
	f.mu.Lock()
	original := f.cm.DeepCopy()
	f.cm.UID = "replacement"
	f.mu.Unlock()
	if _, err := d.Snapshot(ctx); !errors.Is(err, ErrRejected) {
		t.Fatalf("recreated ConfigMap adopted: %v", err)
	}
	f.mu.Lock()
	f.cm = original.DeepCopy()
	value := f.cm.Data[durableKey]
	f.cm.Data[durableKey] = strings.Replace(value, `"version":1`, `"version":1,"version":1`, 1)
	f.mu.Unlock()
	if _, err := OpenDurable(ctx, client, "test-ns", "ledger", d.UID(), d.Epoch()); err == nil {
		t.Fatal("duplicate JSON accepted")
	}
	f.mu.Lock()
	f.cm = original.DeepCopy()
	f.cm.Data[durableKey] = strings.Replace(value, `"version":1`, `"version":2`, 1)
	f.mu.Unlock()
	if _, err := OpenDurable(ctx, client, "test-ns", "ledger", d.UID(), d.Epoch()); err == nil {
		t.Fatal("unknown version accepted")
	}
	f.mu.Lock()
	f.cm = original.DeepCopy()
	f.cm.Data[durableKey] = strings.Replace(value, `"revision":1`, `"revision":1,"unknown":true`, 1)
	f.mu.Unlock()
	if _, err := OpenDurable(ctx, client, "test-ns", "ledger", d.UID(), d.Epoch()); err == nil {
		t.Fatal("unknown field accepted")
	}
}

func TestDurableConcurrentStaleReadCannotOverwriteReservation(t *testing.T) {
	ctx := context.Background()
	d, _, _ := durableFixture(t, Resources{})
	cm1, ledger1, c1, err := d.read(ctx)
	if err != nil {
		t.Fatal(err)
	}
	cm2, ledger2, c2, err := d.read(ctx)
	if err != nil {
		t.Fatal(err)
	}
	a := plan("a", "node-0", allocation("c", gpu0, 60, 60, 1))
	b := plan("b", "node-0", allocation("c", gpu0, 60, 60, 1))
	if err := c1.Reserve(JobPlan{UID: "first", Pods: []PodPlan{a}}); err != nil {
		t.Fatal(err)
	}
	if err := c2.Reserve(JobPlan{UID: "second", Pods: []PodPlan{b}}); err != nil {
		t.Fatal(err)
	}
	if err := d.write(ctx, cm1, ledger1, c1); err != nil {
		t.Fatal(err)
	}
	if err := d.write(ctx, cm2, ledger2, c2); !errors.Is(err, ErrDurableConflict) {
		t.Fatalf("stale concurrent write accepted: %v", err)
	}
	state, err := d.Snapshot(ctx)
	if err != nil || state.Used[gpu0] != (Resources{60, 60, 1}) || state.Pods["a"] != Reserved || state.Pods["b"] != "" {
		t.Fatalf("stale concurrent write corrupted ledger: %+v %v", state, err)
	}
}

func TestDurableTakeoverOfConsumedAndPublishedKeepsAllResources(t *testing.T) {
	ctx := context.Background()
	d, client, _ := durableFixture(t, Resources{})
	a := plan("a", "node-0", allocation("c", gpu0, 40, 40, 1))
	b := plan("b", "node-0", allocation("c", gpu0, 40, 40, 1))
	if err := d.Reserve(ctx, JobPlan{UID: "job", Pods: []PodPlan{a, b}}); err != nil {
		t.Fatal(err)
	}
	ta, err := d.Begin(ctx, "job", "a", "node-0")
	if err != nil {
		t.Fatal(err)
	}
	durableBindAndConsume(t, d, ta, a)
	tb, err := d.Begin(ctx, "job", "b", "node-0")
	if err != nil {
		t.Fatal(err)
	}
	if err := d.Published(ctx, tb); err != nil {
		t.Fatal(err)
	}
	next, err := TakeoverDurable(ctx, client, "test-ns", "ledger", d.UID(), d.Epoch())
	if err != nil {
		t.Fatal(err)
	}
	state, err := next.Snapshot(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if state.Pods["a"] != Uncertain || state.Pods["b"] != Uncertain || state.Used[gpu0] != (Resources{80, 80, 2}) || state.Slots["node-0"] != "b" || len(state.Blocked["node-0"]) != 2 {
		t.Fatalf("partial job not retained: %+v", state)
	}
	if err := next.AcknowledgeRecovery(ctx, recoveryProof()); err != nil {
		t.Fatal(err)
	}
	newB, err := next.RecoveryTicket(ctx, "job", "b", "node-0")
	if err != nil {
		t.Fatal(err)
	}
	if err := next.Resolve(ctx, newB, releaseProof(newB)); err != nil {
		t.Fatal(err)
	}
	state, err = next.Snapshot(ctx)
	if err != nil || state.Used[gpu0] != (Resources{40, 40, 1}) || len(state.Blocked["node-0"]) != 1 {
		t.Fatalf("resolving b incorrectly released a: %+v %v", state, err)
	}
	newA, err := next.RecoveryTicket(ctx, "job", "a", "node-0")
	if err != nil {
		t.Fatal(err)
	}
	if err := next.Resolve(ctx, newA, releaseProof(newA)); err != nil {
		t.Fatal(err)
	}
	if state, err = next.Snapshot(ctx); err != nil || state.Used[gpu0] != (Resources{}) || len(state.Blocked["node-0"]) != 0 {
		t.Fatalf("resolved resources leaked: %+v %v", state, err)
	}
}

func TestDurableCorruptStatesAndOversubscriptionRejected(t *testing.T) {
	ctx := context.Background()
	d, _, f := durableFixture(t, Resources{})
	p := plan("p", "node-0", allocation("c", gpu0, 40, 40, 1))
	if err := d.Reserve(ctx, JobPlan{UID: "job", Pods: []PodPlan{p}}); err != nil {
		t.Fatal(err)
	}
	f.mu.Lock()
	original := f.cm.DeepCopy()
	f.mu.Unlock()
	var ledger durableLedger
	if err := json.Unmarshal([]byte(original.Data[durableKey]), &ledger); err != nil {
		t.Fatal(err)
	}
	for name, mutate := range map[string]func(*durableLedger){
		"missing-device":     func(l *durableLedger) { l.Devices = nil },
		"duplicate-device":   func(l *durableLedger) { l.Devices = append(l.Devices, l.Devices[0]) },
		"negative-baseline":  func(l *durableLedger) { l.Devices[0].Baseline.Cores = -1 },
		"over-capacity":      func(l *durableLedger) { l.Devices[0].Baseline.Cores = 70 },
		"unknown-stage":      func(l *durableLedger) { l.Jobs[0].Pods[0].Stage = "mystery" },
		"reserved-with-slot": func(l *durableLedger) { l.Jobs[0].Pods[0].HoldsSlot = true },
		"missing-pod-uid":    func(l *durableLedger) { l.Jobs[0].Pods[0].Plan.UID = "" },
	} {
		t.Run(name, func(t *testing.T) {
			var copyLedger durableLedger
			raw, _ := json.Marshal(ledger)
			if err := json.Unmarshal(raw, &copyLedger); err != nil {
				t.Fatal(err)
			}
			mutate(&copyLedger)
			encoded, err := json.Marshal(copyLedger)
			if err != nil {
				t.Fatal(err)
			}
			f.mu.Lock()
			f.cm = original.DeepCopy()
			f.cm.Data[durableKey] = string(encoded)
			f.mu.Unlock()
			if _, err := d.Snapshot(ctx); err == nil {
				t.Fatal("corrupt ledger was accepted")
			}
		})
	}
	f.mu.Lock()
	f.cm = original.DeepCopy()
	f.mu.Unlock()
}

func TestDurableRejectsDuplicatePhysicalGPUAndUnsafeBaseline(t *testing.T) {
	ctx := context.Background()
	client, fixture := newDurableCASFake()
	duplicate := DeviceKey{Node: "another-node", GPU: gpu0.GPU}
	if _, err := CreateDurable(ctx, client, "test-ns", "ledger", map[DeviceKey]Resources{gpu0: {100, 100, 10}, duplicate: {100, 100, 10}}, nil); !errors.Is(err, ErrRejected) {
		t.Fatalf("duplicate global GPU identity accepted: %v", err)
	}
	fixture.mu.Lock()
	created := fixture.cm != nil
	fixture.mu.Unlock()
	if created {
		t.Fatal("invalid snapshot was written before validation")
	}
	d, err := CreateDurable(ctx, client, "test-ns", "ledger", map[DeviceKey]Resources{gpu0: {100, 100, 10}}, map[DeviceKey]Resources{gpu0: {}})
	if err != nil {
		t.Fatal(err)
	}
	a := plan("a", "node-0", allocation("c", gpu0, 40, 40, 1))
	b := plan("b", "node-0", allocation("c", gpu0, 40, 40, 1))
	if err := d.Reserve(ctx, JobPlan{UID: "job", Pods: []PodPlan{a, b}}); err != nil {
		t.Fatal(err)
	}
	before, err := d.Snapshot(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if err := d.ReconcileBaseline(ctx, map[DeviceKey]Resources{gpu0: {30, 30, 1}}, baselineProof()); !errors.Is(err, ErrRejected) {
		t.Fatalf("overfull external baseline accepted: %v", err)
	}
	after, err := d.Snapshot(ctx)
	if err != nil || !reflect.DeepEqual(before, after) {
		t.Fatalf("failed baseline reconciliation changed reservations: %+v %v", after, err)
	}
}

func TestDurableConcurrentWritersCannotOverreserve(t *testing.T) {
	ctx := context.Background()
	d, client, _ := durableFixture(t, Resources{})
	const count = 32
	var wg sync.WaitGroup
	errorsOut := make(chan error, count)
	for i := 0; i < count; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			// Separate handles represent independent cooperating processes that
			// share the object UID and epoch, not a shared in-memory mutex.
			handle, err := OpenDurable(ctx, client, "test-ns", "ledger", d.UID(), d.Epoch())
			if err == nil {
				uid := fmt.Sprintf("pod-%02d", i)
				job := fmt.Sprintf("job-%02d", i)
				pod := plan(uid, "node-0", allocation("c", gpu0, 6, 6, 1))
				err = handle.Reserve(ctx, JobPlan{UID: job, Pods: []PodPlan{pod}})
			}
			errorsOut <- err
		}(i)
	}
	wg.Wait()
	close(errorsOut)
	success := 0
	for err := range errorsOut {
		if err == nil {
			success++
			continue
		}
		if !errors.Is(err, ErrDurableConflict) && !errors.Is(err, ErrRejected) {
			t.Fatalf("unexpected concurrent failure: %v", err)
		}
	}
	state, err := d.Snapshot(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if success < 1 || success > 10 || len(state.Pods) != success ||
		state.Used[gpu0] != (Resources{Memory: int64(success * 6), Cores: int64(success * 6), Instances: int64(success)}) {
		t.Fatalf("concurrent ledger violated capacity or lost successes: successes=%d state=%+v", success, state)
	}
}

func TestDurableConcurrentTakeoverHasOneWinningEpoch(t *testing.T) {
	ctx := context.Background()
	d, client, _ := durableFixture(t, Resources{})
	const attempts = 24
	type outcome struct {
		handle *DurableCoordinator
		err    error
	}
	outcomes := make(chan outcome, attempts)
	start := make(chan struct{})
	var wg sync.WaitGroup
	for i := 0; i < attempts; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			handle, err := TakeoverDurable(ctx, client, "test-ns", "ledger", d.UID(), d.Epoch())
			outcomes <- outcome{handle, err}
		}()
	}
	close(start)
	wg.Wait()
	close(outcomes)
	var winner *DurableCoordinator
	for result := range outcomes {
		if result.err == nil {
			if winner != nil {
				t.Fatal("multiple takeover epochs won")
			}
			winner = result.handle
			continue
		}
		if !errors.Is(result.err, ErrDurableConflict) && !errors.Is(result.err, ErrRejected) {
			t.Fatalf("unexpected takeover failure: %v", result.err)
		}
	}
	if winner == nil || winner.Epoch() == d.Epoch() {
		t.Fatal("missing or unchanged takeover epoch")
	}
	if _, err := d.Snapshot(ctx); !errors.Is(err, ErrRejected) {
		t.Fatalf("old handle accepted: %v", err)
	}
	if err := winner.Reserve(ctx, JobPlan{UID: "new", Pods: []PodPlan{plan("p", "node-0", allocation("c", gpu0, 1, 1, 1))}}); !errors.Is(err, ErrDurableQuarantined) {
		t.Fatalf("winning epoch skipped recovery quarantine: %v", err)
	}
}
