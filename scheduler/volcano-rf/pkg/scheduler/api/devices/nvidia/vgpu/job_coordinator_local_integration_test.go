// Local proof-of-concept only: real frozen RF advice, synthetic native GPU
// ledgers, an isolated coordinator, and a deliberately fake device consumer.
// No Kubernetes API, Volcano action, kubelet, actual binding or device plugin
// is used here. This test is not an acceptance test for real C-group safety.
package vgpu

import (
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"reflect"
	"testing"

	v1 "k8s.io/api/core/v1"
	"volcano.sh/volcano/pkg/scheduler/plugins/hrrn/coordination"
)

// proposalForModel is intentionally test-only: the vGPU-only proposal cannot
// prove Kubernetes predicates, bind success, or a durable reservation.
func proposalForModel(t *testing.T, job string, p *VGPUJobPlan, pods []*v1.Pod) coordination.JobPlan {
	t.Helper()
	if p == nil || len(p.Members) != len(pods) {
		t.Fatal("full RF proposal required")
	}
	byUID := make(map[string]*v1.Pod, len(pods))
	for _, pod := range pods {
		byUID[string(pod.UID)] = pod
	}
	out := coordination.JobPlan{UID: job}
	for _, member := range p.Members {
		pod := byUID[string(member.PodUID)]
		if pod == nil || member.Namespace != pod.Namespace || member.Name != pod.Name || member.Fallback != "" || member.RequestID == "" {
			t.Fatalf("RF member identity or real-advice proof missing: %+v", member)
		}
		entry := coordination.PodPlan{UID: string(member.PodUID), Node: member.Node}
		for ci, group := range member.Devices {
			if ci >= len(pod.Spec.Containers) {
				t.Fatal("RF plan refers to missing container")
			}
			for _, unit := range group {
				entry.Allocations = append(entry.Allocations, coordination.Allocation{
					Container: pod.Spec.Containers[ci].Name,
					Device:    coordination.DeviceKey{Node: member.Node, GPU: unit.UUID},
					Resources: coordination.Resources{Memory: int64(unit.Usedmem), Cores: int64(unit.Usedcores), Instances: 1},
				})
			}
		}
		out.Pods = append(out.Pods, entry)
	}
	return out
}

// mockDeviceConsumer models one visible candidate and one local lock per node.
// Its trusted-looking observations are deliberately synthetic; it does not
// execute or establish the real GetPendingPod/Allocate/NodeLock protocol.
type mockDeviceConsumer struct {
	visible   map[string]string
	locked    map[string]bool
	published []string
}

func (m *mockDeviceConsumer) publish(t *testing.T, c *coordination.Coordinator, ticket coordination.Ticket) {
	t.Helper()
	if m.visible[ticket.Node] != "" || m.locked[ticket.Node] || c.Snapshot().Slots[ticket.Node] != ticket.PodUID {
		t.Fatal("mock attempted two visible candidates on one node")
	}
	// Pretend the publication callback has performed its external side effect.
	m.visible[ticket.Node], m.locked[ticket.Node] = ticket.PodUID, true
	if err := c.Published(ticket); err != nil {
		t.Fatal(err)
	}
	m.published = append(m.published, ticket.PodUID)
}

func (m *mockDeviceConsumer) consume(t *testing.T, c *coordination.Coordinator, ticket coordination.Ticket, plan coordination.PodPlan) {
	t.Helper()
	if m.visible[ticket.Node] != ticket.PodUID || !m.locked[ticket.Node] {
		t.Fatal("mock consumer cannot identify its only UID")
	}
	if err := c.RecordBound(ticket, coordination.BindingObservation{PodUID: ticket.PodUID, Node: ticket.Node, ObservationID: "mock-binding"}); err != nil {
		t.Fatal(err)
	}
	// This is a synthetic checkpoint and AllocateResponse assertion, not a
	// device-plugin result. Merely declaring bind-phase=success is insufficient.
	bad := coordination.ConsumptionObservation{PodUID: ticket.PodUID, Node: ticket.Node, ObservationID: "mock-bind-phase-only"}
	if err := c.RecordConsumed(ticket, bad); !errors.Is(err, coordination.ErrRejected) {
		t.Fatalf("annotation-only accepted: %v", err)
	}
	observation := coordination.ConsumptionObservation{PodUID: ticket.PodUID, Node: ticket.Node, ObservationID: "mock-only-plugin-and-checkpoint",
		Allocations: plan.Allocations, BoundUIDVerified: true, PluginResponseVerified: true, KubeletCheckpointVerified: true,
		NoInFlightAllocate: true, NodeLockUnheld: true}
	// Pretend the mock plugin has returned and dropped its local-only lock.
	// The coordinator itself does not release a Kubernetes NodeLock.
	delete(m.visible, ticket.Node)
	delete(m.locked, ticket.Node)
	if err := c.RecordConsumed(ticket, observation); err != nil {
		t.Fatal(err)
	}
}

func simulatedRelease(ticket coordination.Ticket) coordination.ReleaseObservation {
	return coordination.ReleaseObservation{PodUID: ticket.PodUID, Node: ticket.Node, ObservationID: "mock-only-release",
		PodTerminalOrAbsent: true, DeviceReleaseVerified: true, NoInFlightAllocate: true, NodeLockUnheld: true}
}

func TestVGPUPlannerAndCoordinatorFrozenRFWithSimulatedDevicePlugin(t *testing.T) {
	endpoint := os.Getenv("RF_LIVE_ENDPOINT")
	if endpoint == "" {
		t.Skip("RF_LIVE_ENDPOINT is required; no mock RF or claimed integration")
	}
	rfTestConfig(t)
	NodeLockEnable = true // planner cannot invoke real NodeLock.
	RFEndpoint = endpoint
	trace := &rfTraceTransport{base: rfHTTP.Transport}
	rfHTTP = &http.Client{Transport: trace, CheckRedirect: rfHTTP.CheckRedirect}

	node := rfTestNode("coordinator-node", "NVIDIA-L40", 1, 46068)
	bg := plannerBackground(node, "coordinator-background", 4096, 30)
	nodes := map[string]*GPUDevices{node.Name: node}
	large := []*v1.Pod{rfTestPod("coordinator-large-0", "training", 4096, 40), rfTestPod("coordinator-large-1", "training", 4096, 40)}
	small := rfTestPod("coordinator-small", "inference", 4096, 60)
	key := coordination.DeviceKey{Node: node.Name, GPU: node.Device[0].UUID}
	capacity := coordination.Resources{Memory: 46068, Cores: 100, Instances: 10}
	initial := coordination.Resources{Memory: 4096, Cores: 30, Instances: 1}
	c, err := coordination.New(map[coordination.DeviceKey]coordination.Resources{key: capacity}, map[coordination.DeviceKey]coordination.Resources{key: initial})
	if err != nil {
		t.Fatal(err)
	}
	mock := &mockDeviceConsumer{visible: map[string]string{}, locked: map[string]bool{}}
	before := c.Snapshot()
	beforeToken := vgpuJobSourceToken(node)
	if proposal, err := PlanCompleteVGPUJob(nodes, large); proposal != nil || !errors.Is(err, ErrVGPUPlanNoCandidate) {
		t.Fatalf("30 + 2x40 must not publish a partial candidate: %+v, %v", proposal, err)
	}
	if !reflect.DeepEqual(before, c.Snapshot()) || len(mock.published) != 0 || vgpuJobSourceToken(node) != beforeToken {
		t.Fatal("failed complete proposal mutated state or published a Pod")
	}
	// A separate, feasible small proposal bypasses the rejected large plan.
	smallRF, err := PlanCompleteVGPUJob(nodes, []*v1.Pod{small})
	if err != nil || ValidateVGPUJobPlanSnapshot(nodes, smallRF) != nil {
		t.Fatalf("small RF proposal invalid: %v", err)
	}
	smallPlan := proposalForModel(t, "small-job", smallRF, []*v1.Pod{small})
	if err := c.Reserve(smallPlan); err != nil {
		t.Fatal(err)
	}
	if got := c.Snapshot().Used[key]; got != (coordination.Resources{Memory: 8192, Cores: 90, Instances: 2}) {
		t.Fatalf("30+60 reservation: %+v", got)
	}
	smallTicket, err := c.Begin(smallPlan.UID, smallPlan.Pods[0].UID, smallPlan.Pods[0].Node)
	if err != nil {
		t.Fatal(err)
	}
	mock.publish(t, c, smallTicket)
	mock.consume(t, c, smallTicket, smallPlan.Pods[0])
	if err := c.Resolve(smallTicket, simulatedRelease(smallTicket)); err != nil {
		t.Fatal(err)
	}
	if got := c.Snapshot().Used[key]; got != initial {
		t.Fatalf("small release leaked: %+v", got)
	}

	// An externally observed background release would require a new reconciled
	// snapshot. Model it by the real native in-memory SubResource and a fresh
	// in-memory coordinator. This is not a durable handoff or cluster restart.
	node.SubResource(bg)
	if node.Device[0].UsedMem != 0 || node.Device[0].UsedCore != 0 || node.Device[0].UsedNum != 0 {
		t.Fatal("native synthetic background not released")
	}
	completeRF, err := PlanCompleteVGPUJob(nodes, large)
	if err != nil || ValidateVGPUJobPlanSnapshot(nodes, completeRF) != nil {
		t.Fatalf("complete RF proposal invalid: %v", err)
	}
	completePlan := proposalForModel(t, "complete-job", completeRF, large)
	post, err := coordination.New(map[coordination.DeviceKey]coordination.Resources{key: capacity}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := post.Reserve(completePlan); err != nil {
		t.Fatal(err)
	}
	if got := post.Snapshot().Used[key]; got != (coordination.Resources{Memory: 8192, Cores: 80, Instances: 2}) {
		t.Fatalf("full plan not reserved: %+v", got)
	}
	first, err := post.Begin(completePlan.UID, completePlan.Pods[0].UID, completePlan.Pods[0].Node)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := post.Begin(completePlan.UID, completePlan.Pods[1].UID, completePlan.Pods[1].Node); !errors.Is(err, coordination.ErrRejected) {
		t.Fatalf("second became visible before first: %v", err)
	}
	mock.publish(t, post, first)
	mock.consume(t, post, first, completePlan.Pods[0])
	second, err := post.Begin(completePlan.UID, completePlan.Pods[1].UID, completePlan.Pods[1].Node)
	if err != nil {
		t.Fatal(err)
	}
	mock.publish(t, post, second)
	mock.consume(t, post, second, completePlan.Pods[1])
	if err := post.Resolve(first, simulatedRelease(first)); err != nil {
		t.Fatal(err)
	}
	if err := post.Resolve(second, simulatedRelease(second)); err != nil {
		t.Fatal(err)
	}
	if got := post.Snapshot().Used[key]; got != (coordination.Resources{}) {
		t.Fatalf("full job reservations leaked: %+v", got)
	}

	// An unknown result after a mock publish cannot silently clear the fake lock
	// or free resources. Independent work can still use a different node.
	other := rfTestNode("other-node", "NVIDIA-L40", 1, 46068)
	otherKey := coordination.DeviceKey{Node: other.Name, GPU: other.Device[0].UUID}
	failure, err := coordination.New(map[coordination.DeviceKey]coordination.Resources{key: capacity, otherKey: capacity}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := failure.Reserve(completePlan); err != nil {
		t.Fatal(err)
	}
	first, err = failure.Begin(completePlan.UID, completePlan.Pods[0].UID, completePlan.Pods[0].Node)
	if err != nil {
		t.Fatal(err)
	}
	mock.publish(t, failure, first)
	if err := failure.Fail(first); err != nil {
		t.Fatal(err)
	}
	if _, err := failure.Begin(completePlan.UID, completePlan.Pods[1].UID, completePlan.Pods[1].Node); !errors.Is(err, coordination.ErrRejected) {
		t.Fatalf("uncertain node did not block: %v", err)
	}
	if err := failure.Resolve(first, coordination.ReleaseObservation{PodUID: first.PodUID, Node: first.Node, ObservationID: "incomplete mock"}); !errors.Is(err, coordination.ErrRejected) {
		t.Fatalf("incomplete release accepted: %v", err)
	}
	if got := failure.Snapshot(); got.Used[key].Cores != 80 || got.Slots[node.Name] != first.PodUID || !mock.locked[node.Name] || mock.visible[node.Name] != first.PodUID {
		t.Fatalf("uncertain published work was silently released: %+v", got)
	}
	otherRF, err := PlanCompleteVGPUJob(map[string]*GPUDevices{other.Name: other}, []*v1.Pod{small})
	if err != nil {
		t.Fatal(err)
	}
	otherPlan := proposalForModel(t, "other-job", otherRF, []*v1.Pod{small})
	if err := failure.Reserve(otherPlan); err != nil {
		t.Fatal(err)
	}
	otherTicket, err := failure.Begin(otherPlan.UID, otherPlan.Pods[0].UID, otherPlan.Pods[0].Node)
	if err != nil {
		t.Fatal(err)
	}
	mock.publish(t, failure, otherTicket)
	mock.consume(t, failure, otherTicket, otherPlan.Pods[0])
	if err := failure.Resolve(otherTicket, simulatedRelease(otherTicket)); err != nil {
		t.Fatal(err)
	}
	if got := failure.Snapshot(); got.Used[key].Cores != 80 || got.Slots[node.Name] != first.PodUID || !mock.locked[node.Name] {
		t.Fatal("other-node work affected uncertainty")
	}

	trace.mu.Lock()
	defer trace.mu.Unlock()
	if len(trace.rows) < 4 {
		t.Fatalf("expected real frozen RF requests, got %d", len(trace.rows))
	}
	for _, row := range trace.rows {
		var response rfResponse
		if err := json.Unmarshal(row["response"].(json.RawMessage), &response); err != nil || row["http_status"] != http.StatusOK ||
			response.ModelVersion != RFModelVersion || !response.ModelReady || response.PredictionEnabled == nil || !*response.PredictionEnabled || response.Status != "ranked" {
			t.Fatalf("response is not from a ready frozen RF model: %+v, %v", row, err)
		}
	}
	t.Logf("frozen RF adviser calls=%d; candidate plan and synthetic coordinator handoff verified; no real API/binder/plugin", len(trace.rows))
}
