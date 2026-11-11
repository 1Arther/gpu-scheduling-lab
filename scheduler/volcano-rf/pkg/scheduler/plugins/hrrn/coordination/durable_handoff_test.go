package coordination

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
)

func boundHandoffFixture(t *testing.T) (*DurableCoordinator, Ticket, DurableHandoff, *durableCASFixture) {
	t.Helper()
	ctx := context.Background()
	d, _, fixture := durableFixture(t, Resources{})
	p := plan("pod", "node-0", allocation("worker", gpu0, 40, 40, 1))
	if err := d.Reserve(ctx, JobPlan{UID: "job", Pods: []PodPlan{p}}); err != nil {
		t.Fatal(err)
	}
	ticket, err := d.Begin(ctx, "job", p.UID, p.Node)
	if err != nil {
		t.Fatal(err)
	}
	if err := d.Published(ctx, ticket); err != nil {
		t.Fatal(err)
	}
	if err := d.RecordBound(ctx, ticket, BindingObservation{PodUID: p.UID, Node: p.Node, ObservationID: "fake-bind"}); err != nil {
		t.Fatal(err)
	}
	h := DurableHandoff{JobUID: "job", PodUID: p.UID, Node: p.Node, Container: "worker", Resource: "volcano.sh/vgpu-number",
		Attempt: DurableAttemptID(ticket), Allocations: p.Allocations, DeviceIDs: []string{"slot-1"}, Nonce: strings.Repeat("a", 64), ResponseSemanticSHA256: strings.Repeat("b", 64)}
	return d, ticket, h, fixture
}

func TestDurableHandoffConcurrentPreparationsCannotBothSucceed(t *testing.T) {
	ctx := context.Background()
	d, _, h, _ := boundHandoffFixture(t)
	other := h
	other.Nonce = strings.Repeat("c", 64)
	var wg sync.WaitGroup
	ready := make(chan struct{})
	results := make(chan error, 2)
	for _, candidate := range []DurableHandoff{h, other} {
		wg.Add(1)
		go func(x DurableHandoff) { defer wg.Done(); <-ready; results <- d.PrepareHandoff(ctx, x) }(candidate)
	}
	close(ready)
	wg.Wait()
	close(results)
	success := 0
	for err := range results {
		if err == nil {
			success++
		}
	}
	if success != 1 {
		t.Fatalf("wanted one committed preparation, got %d", success)
	}
}

func TestDurableHandoffDoesNotReuseDeviceIDBeforeRelease(t *testing.T) {
	ctx := context.Background()
	d, _, _ := durableFixture(t, Resources{})
	a := plan("a", "node-0", allocation("worker", gpu0, 40, 40, 1))
	b := plan("b", "node-0", allocation("worker", gpu0, 40, 40, 1))
	if err := d.Reserve(ctx, JobPlan{UID: "job", Pods: []PodPlan{a, b}}); err != nil {
		t.Fatal(err)
	}
	makeBound := func(p PodPlan) Ticket {
		ticket, err := d.Begin(ctx, "job", p.UID, p.Node)
		if err != nil {
			t.Fatal(err)
		}
		if err := d.Published(ctx, ticket); err != nil {
			t.Fatal(err)
		}
		if err := d.RecordBound(ctx, ticket, BindingObservation{PodUID: p.UID, Node: p.Node, ObservationID: "synthetic"}); err != nil {
			t.Fatal(err)
		}
		return ticket
	}
	ta := makeBound(a)
	h := DurableHandoff{JobUID: "job", PodUID: a.UID, Node: a.Node, Container: "worker", Resource: "volcano.sh/vgpu-number", Attempt: DurableAttemptID(ta), Allocations: a.Allocations, DeviceIDs: []string{"slot-1"}, Nonce: strings.Repeat("a", 64), ResponseSemanticSHA256: strings.Repeat("b", 64)}
	if err := d.PrepareHandoff(ctx, h); err != nil {
		t.Fatal(err)
	}
	if err := d.ConfirmHandoff(ctx, h); err != nil {
		t.Fatal(err)
	}
	obs := ConsumptionObservation{PodUID: a.UID, Node: a.Node, ObservationID: "synthetic", Allocations: a.Allocations, BoundUIDVerified: true, PluginResponseVerified: true, KubeletCheckpointVerified: true, NoInFlightAllocate: true, NodeLockUnheld: true}
	if err := d.RecordConsumedWithHandoffs(ctx, ta, obs); err != nil {
		t.Fatal(err)
	}
	tb := makeBound(b)
	h.PodUID, h.Attempt, h.Allocations, h.Nonce = b.UID, DurableAttemptID(tb), b.Allocations, strings.Repeat("c", 64)
	if err := d.PrepareHandoff(ctx, h); err == nil {
		t.Fatal("device ID reused while earlier Pod remains active")
	}
	h.DeviceIDs = []string{"slot-2"}
	if err := d.PrepareHandoff(ctx, h); err != nil {
		t.Fatal(err)
	}
}

func TestDurableHandoffRequiresEveryContainerConfirmation(t *testing.T) {
	ctx := context.Background()
	d, _, _ := durableFixture(t, Resources{})
	p := plan("pod", "node-0", allocation("first", gpu0, 20, 20, 1), allocation("second", gpu0, 20, 20, 1))
	if err := d.Reserve(ctx, JobPlan{UID: "job", Pods: []PodPlan{p}}); err != nil {
		t.Fatal(err)
	}
	ticket, err := d.Begin(ctx, "job", p.UID, p.Node)
	if err != nil {
		t.Fatal(err)
	}
	if err := d.Published(ctx, ticket); err != nil {
		t.Fatal(err)
	}
	if err := d.RecordBound(ctx, ticket, BindingObservation{PodUID: p.UID, Node: p.Node, ObservationID: "synthetic"}); err != nil {
		t.Fatal(err)
	}
	h := DurableHandoff{JobUID: "job", PodUID: p.UID, Node: p.Node, Container: "first", Resource: "volcano.sh/vgpu-number", Attempt: DurableAttemptID(ticket), Allocations: p.Allocations[:1], DeviceIDs: []string{"slot-1"}, Nonce: strings.Repeat("a", 64), ResponseSemanticSHA256: strings.Repeat("b", 64)}
	if err := d.PrepareHandoff(ctx, h); err != nil {
		t.Fatal(err)
	}
	if err := d.ConfirmHandoff(ctx, h); err != nil {
		t.Fatal(err)
	}
	obs := ConsumptionObservation{PodUID: p.UID, Node: p.Node, ObservationID: "synthetic", Allocations: p.Allocations, BoundUIDVerified: true, PluginResponseVerified: true, KubeletCheckpointVerified: true, NoInFlightAllocate: true, NodeLockUnheld: true}
	if err := d.RecordConsumedWithHandoffs(ctx, ticket, obs); err == nil {
		t.Fatal("missing second container accepted")
	}
	if err := d.RecordConsumed(ctx, ticket, obs); err == nil {
		t.Fatal("legacy method bypassed missing second container")
	}
	h.Container, h.Allocations, h.DeviceIDs, h.Nonce = "second", p.Allocations[1:], []string{"slot-2"}, strings.Repeat("c", 64)
	if err := d.PrepareHandoff(ctx, h); err != nil {
		t.Fatal(err)
	}
	if err := d.ConfirmHandoff(ctx, h); err != nil {
		t.Fatal(err)
	}
	if err := d.RecordConsumedWithHandoffs(ctx, ticket, obs); err != nil {
		t.Fatal(err)
	}
}

func TestDurableHandoffRequiresExactBoundReservationAndPersists(t *testing.T) {
	ctx := context.Background()
	d, ticket, h, _ := boundHandoffFixture(t)
	for name, edit := range map[string]func(*DurableHandoff){
		"wrong-job":     func(x *DurableHandoff) { x.JobUID = "other" },
		"wrong-pod":     func(x *DurableHandoff) { x.PodUID = "other" },
		"wrong-attempt": func(x *DurableHandoff) { x.Attempt = strings.Repeat("c", 32) + ":1" },
		"wrong-quota":   func(x *DurableHandoff) { x.Allocations[0].Resources.Cores++ },
		"wrong-device":  func(x *DurableHandoff) { x.Allocations[0].Device.GPU = "other" },
		"duplicate-id":  func(x *DurableHandoff) { x.DeviceIDs = []string{"slot-1", "slot-1"} },
	} {
		t.Run(name, func(t *testing.T) {
			x := h
			x.Allocations = append([]Allocation(nil), h.Allocations...)
			edit(&x)
			if err := d.PrepareHandoff(ctx, x); err == nil {
				t.Fatal("invalid handoff was accepted")
			}
		})
	}
	if err := d.PrepareHandoff(ctx, h); err != nil {
		t.Fatal(err)
	}
	if err := d.PrepareHandoff(ctx, h); err == nil {
		t.Fatal("duplicate attempt accepted")
	}
	opened, err := OpenDurable(ctx, d.client, d.namespace, d.name, d.UID(), d.Epoch())
	if err != nil {
		t.Fatal(err)
	}
	got, err := opened.LookupHandoff(ctx, h.Node, h.Resource, h.DeviceIDs)
	if err != nil || got.Nonce != h.Nonce || got.Confirmed {
		t.Fatalf("prepared record did not persist: %+v %v", got, err)
	}
	obs := ConsumptionObservation{PodUID: h.PodUID, Node: h.Node, ObservationID: "synthetic", Allocations: h.Allocations,
		BoundUIDVerified: true, PluginResponseVerified: true, KubeletCheckpointVerified: true, NoInFlightAllocate: true, NodeLockUnheld: true}
	if err := opened.RecordConsumedWithHandoffs(ctx, ticket, obs); err == nil {
		t.Fatal("unconfirmed record allowed consumption")
	}
	if err := opened.RecordConsumed(ctx, ticket, obs); err == nil {
		t.Fatal("legacy consumption bypassed an unconfirmed handoff")
	}
	if err := opened.ConfirmHandoff(ctx, h); err != nil {
		t.Fatal(err)
	}
	if err := opened.ConfirmHandoff(ctx, h); err != nil {
		t.Fatalf("identical confirmation not idempotent: %v", err)
	}
	if err := opened.RecordConsumedWithHandoffs(ctx, ticket, obs); err != nil {
		t.Fatal(err)
	}
	if _, err := opened.LookupHandoff(ctx, h.Node, h.Resource, h.DeviceIDs); err == nil {
		t.Fatal("consumed handoff was replayable")
	}
	if err := opened.Resolve(ctx, ticket, releaseProof(ticket)); err != nil {
		t.Fatal(err)
	}
	view, err := opened.Snapshot(ctx)
	if err != nil || view.Used[gpu0] != (Resources{}) {
		t.Fatalf("release failed: %+v %v", view, err)
	}
}

func TestDurableHandoffCancellationTakeoverAndAmbiguousWriteFailClosed(t *testing.T) {
	ctx := context.Background()
	t.Run("cancel-before-prepare", func(t *testing.T) {
		d, _, h, _ := boundHandoffFixture(t)
		if _, err := d.Cancel(ctx, h.JobUID, h.PodUID, h.Node); err != nil {
			t.Fatal(err)
		}
		if err := d.PrepareHandoff(ctx, h); err == nil {
			t.Fatal("uncertain Pod accepted")
		}
	})
	t.Run("takeover", func(t *testing.T) {
		d, _, h, _ := boundHandoffFixture(t)
		if err := d.PrepareHandoff(ctx, h); err != nil {
			t.Fatal(err)
		}
		newer, err := TakeoverDurable(ctx, d.client, d.namespace, d.name, d.UID(), d.Epoch())
		if err != nil {
			t.Fatal(err)
		}
		if _, err = newer.LookupHandoff(ctx, h.Node, h.Resource, h.DeviceIDs); !errors.Is(err, ErrDurableQuarantined) {
			t.Fatalf("old handoff not quarantined: %v", err)
		}
		if err = d.ConfirmHandoff(ctx, h); err == nil {
			t.Fatal("stale epoch confirmed")
		}
	})
	t.Run("lost-response", func(t *testing.T) {
		d, _, h, state := boundHandoffFixture(t)
		state.lostResponseNext = true // The fake reactor commits and then loses the response.
		if err := d.PrepareHandoff(ctx, h); !errors.Is(err, ErrDurableUnknown) {
			t.Fatalf("unknown write not surfaced: %v", err)
		}
		if err := d.PrepareHandoff(ctx, h); err == nil {
			t.Fatal("blind retry accepted")
		}
		stored, err := d.LookupHandoff(ctx, h.Node, h.Resource, h.DeviceIDs)
		if err != nil || stored.Nonce != h.Nonce {
			t.Fatalf("could not inspect committed result: %+v %v", stored, err)
		}
	})
	t.Run("cas-conflict", func(t *testing.T) {
		d, _, h, state := boundHandoffFixture(t)
		state.conflictNext = true
		if err := d.PrepareHandoff(ctx, h); !errors.Is(err, ErrDurableConflict) {
			t.Fatalf("write conflict was not preserved: %v", err)
		}
		if _, err := d.LookupHandoff(ctx, h.Node, h.Resource, h.DeviceIDs); err == nil {
			t.Fatal("failed CAS left a handoff record")
		}
	})
}
