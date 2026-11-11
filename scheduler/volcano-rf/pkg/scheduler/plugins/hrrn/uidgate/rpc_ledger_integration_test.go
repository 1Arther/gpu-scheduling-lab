package uidgate

import (
	"context"
	"testing"

	"github.com/gogo/protobuf/proto"
	pluginapi "k8s.io/kubelet/pkg/apis/deviceplugin/v1beta1"
	"volcano.sh/volcano/pkg/scheduler/plugins/hrrn/coordination"
)

// Actual generated pluginapi RPC serialization and the real shared ledger
// code, but fake Kubernetes API, binder, kubelet checkpoint, source observer
// and claim source. No kubelet, deployed HAMi or real fencing is exercised.
func TestRPCLedgerTwoPodHandoffWithSyntheticKubelet(t *testing.T) {
	ctx := context.Background()
	f := newDurableGateFixture(t)
	f.gate.Journal = &LedgerJournal{Ledger: f.store, Resource: "volcano.sh/vgpu-number"}
	adapter, source, _, _, _ := rpcFixture()
	adapter.Gate = f.gate
	client := rpcServer(t, adapter)
	first, c1 := f.begin(t, f.job.Pods[0])
	source.value = c1
	allocate := func(id string) []byte {
		t.Helper()
		response, err := client.Allocate(ctx, &pluginapi.AllocateRequest{ContainerRequests: []*pluginapi.ContainerAllocateRequest{{DevicesIDs: []string{id}}}})
		if err != nil || len(response.ContainerResponses) != 1 {
			t.Fatalf("RPC Allocate: %+v %v", response, err)
		}
		data, err := proto.Marshal(response.ContainerResponses[0])
		if err != nil {
			t.Fatal(err)
		}
		return data
	}
	prestart := func(id string) error {
		_, err := client.PreStartContainer(ctx, &pluginapi.PreStartContainerRequest{DevicesIDs: []string{id}})
		return err
	}
	raw1 := allocate("slot-1")
	if _, err := f.store.Begin(ctx, f.job.UID, f.job.Pods[1].UID, f.key.Node); err == nil {
		t.Fatal("second Pod accepted before first handoff")
	}
	f.observer.raw = checkpoint(t, entry("wrong-pod", c1.Container, []string{"slot-1"}, raw1))
	if err := prestart("slot-1"); err == nil {
		t.Fatal("wrong Pod passed prestart")
	}
	handoff, err := f.store.LookupHandoff(ctx, c1.Node, c1.Resource, []string{"slot-1"})
	if err != nil || handoff.Confirmed {
		t.Fatalf("wrong Pod confirmed or destroyed handoff: %+v %v", handoff, err)
	}
	f.observer.raw = checkpoint(t, entry(c1.PodUID, c1.Container, []string{"slot-1"}, raw1))
	if err := prestart("slot-1"); err != nil {
		t.Fatal(err)
	}
	handoff, err = f.store.LookupHandoff(ctx, c1.Node, c1.Resource, []string{"slot-1"})
	if err != nil || !handoff.Confirmed {
		t.Fatalf("prestart confirmation not persisted: %+v %v", handoff, err)
	}
	obs := coordination.ConsumptionObservation{PodUID: first.PodUID, Node: first.Node, ObservationID: "synthetic-node-evidence", Allocations: c1.Allocations,
		BoundUIDVerified: true, PluginResponseVerified: true, KubeletCheckpointVerified: true, NoInFlightAllocate: true, NodeLockUnheld: true}
	if err := f.store.RecordConsumedWithHandoffs(ctx, first, obs); err != nil {
		t.Fatal(err)
	}
	second, c2 := f.begin(t, f.job.Pods[1])
	source.value = c2
	raw2 := allocate("slot-2")
	f.observer.raw = checkpoint(t, entry(c1.PodUID, c1.Container, []string{"slot-1"}, raw1), entry(c2.PodUID, c2.Container, []string{"slot-2"}, raw2))
	if err := prestart("slot-2"); err != nil {
		t.Fatal(err)
	}
	obs.PodUID, obs.Allocations = second.PodUID, c2.Allocations
	if err := f.store.RecordConsumedWithHandoffs(ctx, second, obs); err != nil {
		t.Fatal(err)
	}
	for _, ticket := range []coordination.Ticket{first, second} {
		err := f.store.Resolve(ctx, ticket, coordination.ReleaseObservation{PodUID: ticket.PodUID, Node: ticket.Node,
			ObservationID: "synthetic-release", PodTerminalOrAbsent: true, DeviceReleaseVerified: true, NoInFlightAllocate: true, NodeLockUnheld: true})
		if err != nil {
			t.Fatal(err)
		}
	}
	state, err := f.store.Snapshot(ctx)
	if err != nil || state.Used[f.key] != (coordination.Resources{}) {
		t.Fatalf("resources not released: %+v %v", state, err)
	}
}

func TestRPCLedgerTakeoverRetainsCapacityAndRejectsOldAttempt(t *testing.T) {
	ctx := context.Background()
	f := newDurableGateFixture(t)
	f.gate.Journal = &LedgerJournal{Ledger: f.store, Resource: "volcano.sh/vgpu-number"}
	adapter, source, _, _, _ := rpcFixture()
	adapter.Gate = f.gate
	client := rpcServer(t, adapter)
	_, c := f.begin(t, f.job.Pods[0])
	source.value = c
	response, err := client.Allocate(ctx, &pluginapi.AllocateRequest{ContainerRequests: []*pluginapi.ContainerAllocateRequest{{DevicesIDs: []string{"slot-1"}}}})
	if err != nil {
		t.Fatal(err)
	}
	raw, err := proto.Marshal(response.ContainerResponses[0])
	if err != nil {
		t.Fatal(err)
	}
	f.observer.raw = checkpoint(t, entry(c.PodUID, c.Container, []string{"slot-1"}, raw))
	old := f.store
	newStore, err := coordination.TakeoverDurable(ctx, f.client, "test-ns", "ledger", old.UID(), old.Epoch())
	if err != nil {
		t.Fatal(err)
	}
	prestart := &pluginapi.PreStartContainerRequest{DevicesIDs: []string{"slot-1"}}
	if _, err := client.PreStartContainer(ctx, prestart); err == nil {
		t.Fatal("old epoch confirmed after takeover")
	}
	adapter.Gate.Journal = &LedgerJournal{Ledger: newStore, Resource: c.Resource}
	if _, err := client.PreStartContainer(ctx, prestart); err == nil {
		t.Fatal("quarantined new epoch confirmed without recovery")
	}
	state, err := newStore.Snapshot(ctx)
	if err != nil || state.Used[f.key].Cores != 80 || state.Pods[c.PodUID] != coordination.Uncertain {
		t.Fatalf("takeover lost unknown resources: %+v %v", state, err)
	}
}
