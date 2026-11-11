package uidgate

import (
	"context"
	"testing"

	"volcano.sh/volcano/pkg/scheduler/plugins/hrrn/coordination"
)

// This uses the actual shared ConfigMap-ledger code, an explicitly versioned
// fake API, and fake checkpoint/plugin/binding. It does not prove external
// NodeLock, kubelet, API-server, or scheduler fencing.
func TestLedgerJournalOneCASReservationAndSyntheticUIDGate(t *testing.T) {
	ctx := context.Background()
	f := newDurableGateFixture(t)
	f.gate.Journal = &LedgerJournal{Ledger: f.store, Resource: "volcano.sh/vgpu-number"}
	first, c := f.begin(t, f.job.Pods[0])
	response, err := f.gate.PrepareAllocate(ctx, c, []string{"slot-1"}, baseResponse())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.store.Begin(ctx, f.job.UID, f.job.Pods[1].UID, f.key.Node); err == nil {
		t.Fatal("second same-node candidate started too early")
	}
	stored, err := f.store.LookupHandoff(ctx, f.key.Node, c.Resource, []string{"slot-1"})
	if err != nil || stored.Attempt != c.Attempt || stored.PodUID != c.PodUID || stored.Confirmed {
		t.Fatalf("missing prepared record: %+v %v", stored, err)
	}
	f.observer.raw = checkpoint(t, entry(c.PodUID, c.Container, []string{"slot-1"}, response))
	prepared, err := f.gate.CheckPreStart(ctx, f.key.Node, []string{"slot-1"})
	if err != nil || prepared.Claim.PodUID != first.PodUID {
		t.Fatalf("synthetic prestart failed: %+v %v", prepared, err)
	}
	stored, err = f.store.LookupHandoff(ctx, f.key.Node, c.Resource, []string{"slot-1"})
	if err != nil || !stored.Confirmed {
		t.Fatalf("confirmation not persistent: %+v %v", stored, err)
	}
	obs := coordination.ConsumptionObservation{PodUID: first.PodUID, Node: first.Node, ObservationID: "synthetic-ack", Allocations: c.Allocations,
		BoundUIDVerified: true, PluginResponseVerified: true, KubeletCheckpointVerified: true, NoInFlightAllocate: true, NodeLockUnheld: true}
	if err := f.store.RecordConsumedWithHandoffs(ctx, first, obs); err != nil {
		t.Fatal(err)
	}
	second, c2 := f.begin(t, f.job.Pods[1])
	response2, err := f.gate.PrepareAllocate(ctx, c2, []string{"slot-2"}, baseResponse())
	if err != nil {
		t.Fatal(err)
	}
	f.observer.raw = checkpoint(t, entry(c.PodUID, c.Container, []string{"slot-1"}, response), entry(c2.PodUID, c2.Container, []string{"slot-2"}, response2))
	if _, err = f.gate.CheckPreStart(ctx, f.key.Node, []string{"slot-2"}); err != nil {
		t.Fatal(err)
	}
	obs.PodUID, obs.Allocations = second.PodUID, c2.Allocations
	if err = f.store.RecordConsumedWithHandoffs(ctx, second, obs); err != nil {
		t.Fatal(err)
	}
	for _, ticket := range []coordination.Ticket{first, second} {
		if err = f.store.Resolve(ctx, ticket, coordination.ReleaseObservation{PodUID: ticket.PodUID, Node: ticket.Node, ObservationID: "synthetic-release", PodTerminalOrAbsent: true, DeviceReleaseVerified: true, NoInFlightAllocate: true, NodeLockUnheld: true}); err != nil {
			t.Fatal(err)
		}
	}
	state, err := f.store.Snapshot(ctx)
	if err != nil || state.Used[f.key] != (coordination.Resources{}) {
		t.Fatalf("synthetic release leaked: %+v %v", state, err)
	}
}

func TestLedgerJournalRejectsWrongPodAndTakeoverStaysQuarantined(t *testing.T) {
	ctx := context.Background()
	f := newDurableGateFixture(t)
	j := &LedgerJournal{Ledger: f.store, Resource: "volcano.sh/vgpu-number"}
	f.gate.Journal = j
	_, c := f.begin(t, f.job.Pods[0])
	response, err := f.gate.PrepareAllocate(ctx, c, []string{"slot-1"}, baseResponse())
	if err != nil {
		t.Fatal(err)
	}
	f.observer.raw = checkpoint(t, entry("other-uid", c.Container, []string{"slot-1"}, response))
	if _, err = f.gate.CheckPreStart(ctx, c.Node, []string{"slot-1"}); err == nil {
		t.Fatal("wrong UID accepted")
	}
	stored, err := f.store.LookupHandoff(ctx, c.Node, c.Resource, []string{"slot-1"})
	if err != nil || stored.Confirmed {
		t.Fatalf("wrong UID mutated ledger: %+v %v", stored, err)
	}
	newStore, err := coordination.TakeoverDurable(ctx, f.client, "test-ns", "ledger", f.store.UID(), f.store.Epoch())
	if err != nil {
		t.Fatal(err)
	}
	if _, err = j.Lookup(ctx, c.Node, []string{"slot-1"}); err == nil {
		t.Fatal("old epoch remained valid")
	}
	newJournal := &LedgerJournal{Ledger: newStore, Resource: c.Resource}
	if _, err = newJournal.Lookup(ctx, c.Node, []string{"slot-1"}); err == nil {
		t.Fatal("quarantined handoff accepted")
	}
	state, err := newStore.Snapshot(ctx)
	if err != nil || state.Used[f.key].Cores != 80 || state.Pods[c.PodUID] != coordination.Uncertain {
		t.Fatalf("takeover lost resources: %+v %v", state, err)
	}
}

func TestLedgerJournalRejectsWrongResourceAndPlan(t *testing.T) {
	ctx := context.Background()
	f := newDurableGateFixture(t)
	_, c := f.begin(t, f.job.Pods[0])
	f.gate.Journal = &LedgerJournal{Ledger: f.store, Resource: "wrong-resource"}
	if _, err := f.gate.PrepareAllocate(ctx, c, []string{"slot-1"}, baseResponse()); err == nil {
		t.Fatal("wrong resource accepted")
	}
	f.gate.Journal = &LedgerJournal{Ledger: f.store, Resource: c.Resource}
	c.Allocations[0].Resources.Cores++
	if _, err := f.gate.PrepareAllocate(ctx, c, []string{"slot-1"}, baseResponse()); err == nil {
		t.Fatal("wrong plan accepted")
	}
	state, err := f.store.Snapshot(ctx)
	if err != nil || state.Used[f.key].Cores != 80 {
		t.Fatalf("wrong plan changed reservations: %+v %v", state, err)
	}
}
