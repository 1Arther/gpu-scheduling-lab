// Copyright 2026 The Volcano Authors. Apache-2.0.
package coordination

// These fixtures exercise only an in-process fake Kubernetes API and a fake
// device consumer. They do not establish real Volcano binding or kubelet / GPU
// Device Plugin behavior. No live cluster, actual NodeLock or GPU is touched.

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"reflect"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	v1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes/fake"
	clienttesting "k8s.io/client-go/testing"
)

// client-go's default fake object tracker does not enforce Kubernetes
// resourceVersion conflicts. Explicit reactors make each ConfigMap update a
// single atomic compare-and-swap for this local test, including concurrent
// contenders. They do not claim to model all API-server behavior.
func flowCASClient(t *testing.T, objects ...runtime.Object) *fake.Clientset {
	t.Helper()
	client := fake.NewSimpleClientset(objects...)
	var mu sync.Mutex
	var sequence int64
	gvr := schema.GroupVersionResource{Version: "v1", Resource: "configmaps"}
	client.PrependReactor("create", "configmaps", func(action clienttesting.Action) (bool, runtime.Object, error) {
		mu.Lock()
		defer mu.Unlock()
		obj := action.(clienttesting.CreateAction).GetObject().(*v1.ConfigMap).DeepCopy()
		sequence++
		obj.UID = types.UID(fmt.Sprintf("local-cm-uid-%d", sequence))
		obj.ResourceVersion = strconv.FormatInt(sequence, 10)
		if err := client.Tracker().Create(gvr, obj, action.GetNamespace()); err != nil {
			return true, nil, err
		}
		return true, obj.DeepCopy(), nil
	})
	client.PrependReactor("update", "configmaps", func(action clienttesting.Action) (bool, runtime.Object, error) {
		mu.Lock()
		defer mu.Unlock()
		obj := action.(clienttesting.UpdateAction).GetObject().(*v1.ConfigMap).DeepCopy()
		current, err := client.Tracker().Get(gvr, action.GetNamespace(), obj.Name)
		if err != nil {
			return true, nil, err
		}
		old := current.(*v1.ConfigMap)
		if obj.UID != old.UID || obj.ResourceVersion != old.ResourceVersion {
			return true, nil, apierrors.NewConflict(schema.GroupResource{Resource: "configmaps"}, obj.Name, fmt.Errorf("simulated UID/resourceVersion conflict"))
		}
		sequence++
		obj.ResourceVersion = strconv.FormatInt(sequence, 10)
		if err := client.Tracker().Update(gvr, obj, action.GetNamespace()); err != nil {
			return true, nil, err
		}
		return true, obj.DeepCopy(), nil
	})
	return client
}

const flowNamespace = "local-coordination-test"
const flowLockAnnotation = "test.gpu-lab/node-lock-owner"
const flowPendingAnnotation = "test.gpu-lab/visible-candidate"

// simulatedDeviceFlow deliberately exposes the synthetic nature of these
// callbacks. It can assert one visible candidate per node and fake a Pod bind,
// but it cannot correlate a real kubelet AllocateRequest to a Pod UID.
type simulatedDeviceFlow struct {
	client *fake.Clientset
	mu     sync.Mutex
}

func (s *simulatedDeviceFlow) publish(t *testing.T, node, podName string, podUID types.UID, attempt string) {
	t.Helper()
	s.mu.Lock()
	defer s.mu.Unlock()
	ctx := context.Background()
	n, err := s.client.CoreV1().Nodes().Get(ctx, node, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if n.Annotations[flowLockAnnotation] != "" || n.Annotations[flowPendingAnnotation] != "" {
		t.Fatal("simulated node already has an outstanding candidate")
	}
	p, err := s.client.CoreV1().Pods(flowNamespace).Get(ctx, podName, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if p.UID != podUID || p.Spec.NodeName != "" || attempt == "" {
		t.Fatal("stale or already bound simulated Pod")
	}
	if n.Annotations == nil {
		n.Annotations = map[string]string{}
	}
	n.Annotations[flowLockAnnotation], n.Annotations[flowPendingAnnotation] = attempt, string(podUID)
	if _, err = s.client.CoreV1().Nodes().Update(ctx, n, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	if p.Annotations == nil {
		p.Annotations = map[string]string{}
	}
	p.Annotations[flowPendingAnnotation] = attempt
	if _, err = s.client.CoreV1().Pods(flowNamespace).Update(ctx, p, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
}

func (s *simulatedDeviceFlow) bind(t *testing.T, node, podName string, podUID types.UID, attempt string) {
	t.Helper()
	s.mu.Lock()
	defer s.mu.Unlock()
	ctx := context.Background()
	n, err := s.client.CoreV1().Nodes().Get(ctx, node, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	p, err := s.client.CoreV1().Pods(flowNamespace).Get(ctx, podName, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if n.Annotations[flowLockAnnotation] != attempt || n.Annotations[flowPendingAnnotation] != string(podUID) ||
		p.UID != podUID || p.Annotations[flowPendingAnnotation] != attempt || p.Spec.NodeName != "" {
		t.Fatal("fake bind identity mismatch")
	}
	p.Spec.NodeName = node
	if _, err = s.client.CoreV1().Pods(flowNamespace).Update(ctx, p, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
}

func (s *simulatedDeviceFlow) releaseNode(t *testing.T, node string, podUID types.UID, attempt string) {
	t.Helper()
	s.mu.Lock()
	defer s.mu.Unlock()
	ctx := context.Background()
	n, err := s.client.CoreV1().Nodes().Get(ctx, node, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if n.Annotations[flowLockAnnotation] != attempt || n.Annotations[flowPendingAnnotation] != string(podUID) {
		t.Fatal("fake plugin cannot release another candidate's lock")
	}
	delete(n.Annotations, flowLockAnnotation)
	delete(n.Annotations, flowPendingAnnotation)
	if _, err = s.client.CoreV1().Nodes().Update(ctx, n, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
}

func flowPlan(job, uid string, key DeviceKey, cores int64) JobPlan {
	return JobPlan{UID: job, Pods: []PodPlan{{UID: uid, Node: key.Node, Allocations: []Allocation{{Container: "worker", Device: key, Resources: Resources{Memory: 1000, Cores: cores, Instances: 1}}}}}}
}

func flowAssertUsed(t *testing.T, d *DurableCoordinator, key DeviceKey, cores int64) State {
	t.Helper()
	state, err := d.Snapshot(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if state.Used[key].Cores != cores {
		t.Fatalf("used=%+v; want %d cores", state.Used[key], cores)
	}
	return state
}

// Simulated actors sign local claims, not real kubelet/plugin attestations.
func flowConsume(t *testing.T, d *DurableCoordinator, s *simulatedDeviceFlow, plan JobPlan, podName string) Ticket {
	t.Helper()
	ctx := context.Background()
	p := plan.Pods[0]
	ticket, err := d.Begin(ctx, plan.UID, p.UID, p.Node)
	if err != nil {
		t.Fatal(err)
	}
	attempt := DurableAttemptID(ticket)
	s.publish(t, p.Node, podName, types.UID(p.UID), attempt)
	if err := d.Published(ctx, ticket); err != nil {
		t.Fatal(err)
	}
	s.bind(t, p.Node, podName, types.UID(p.UID), attempt)
	if err := d.RecordBound(ctx, ticket, BindingObservation{PodUID: p.UID, Node: p.Node, ObservationID: "synthetic-bound-" + p.UID}); err != nil {
		t.Fatal(err)
	}
	node, err := s.client.CoreV1().Nodes().Get(ctx, p.Node, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	pod, err := s.client.CoreV1().Pods(flowNamespace).Get(ctx, podName, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if node.Annotations[flowPendingAnnotation] != p.UID || node.Annotations[flowLockAnnotation] != attempt || string(pod.UID) != p.UID || pod.Spec.NodeName != p.Node {
		t.Fatal("simulated candidate identity did not match")
	}
	s.releaseNode(t, p.Node, types.UID(p.UID), attempt)
	now := time.Unix(1750000000, 0)
	anchors := map[AckKind]AckAnchor{}
	for _, kind := range []AckKind{AckBinding, AckPlugin, AckCheckpoint, AckQuiescence} {
		anchors[kind] = AckAnchor{Source: string(kind), Key: []byte(strings.Repeat(string(kind)+"-", 32))}
	}
	trust, err := NewAckTrust(anchors)
	if err != nil {
		t.Fatal(err)
	}
	base := func(kind AckKind, offset int) AckArtifact {
		return AckArtifact{Kind: kind, Source: string(kind), EvidenceID: string(kind) + "-" + p.UID, JobUID: plan.UID, PodUID: p.UID, Node: p.Node, AttemptID: attempt, ObservedAt: now.Add(time.Duration(offset) * time.Second).UnixNano(), ExpiresAt: now.Add(time.Minute).UnixNano()}
	}
	sign := func(a AckArtifact) AckArtifact {
		signed, err := SignSyntheticAck(anchors[a.Kind].Key, a)
		if err != nil {
			t.Fatal(err)
		}
		return signed
	}
	binding := base(AckBinding, -4)
	binding.Bound = true
	binding.ResourceVersion = "fake-rv"
	raw := []byte("synthetic AllocateResponse " + p.UID + " " + p.Allocations[0].Device.GPU)
	hash := sha256.Sum256(raw)
	plugin := base(AckPlugin, -3)
	plugin.Container = "worker"
	plugin.UIDBound = true
	plugin.ResponseDone = true
	plugin.DecodeVerified = true
	plugin.RawResponse = raw
	plugin.ResponseSHA256 = hex.EncodeToString(hash[:])
	plugin.Allocations = p.Allocations
	plugin.CandidatesOnNode = 1
	plugin.CandidatePodUID = p.UID
	checkpoint := base(AckCheckpoint, -2)
	checkpoint.Container = "worker"
	checkpoint.UIDBound = true
	checkpoint.ResponseDone = true
	checkpoint.DecodeVerified = true
	checkpoint.ResponseSHA256 = plugin.ResponseSHA256
	checkpoint.Allocations = p.Allocations
	quiet := base(AckQuiescence, -1)
	quiet.NoInFlight = true
	quiet.NodeLockUnheld = true
	bundle := AckBundle{Binding: sign(binding), Plugins: []AckArtifact{sign(plugin)}, Checkpoints: []AckArtifact{sign(checkpoint)}, Quiescence: sign(quiet)}
	obs, err := VerifyDeviceAcknowledgement(ticket, p, bundle, trust, now, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if err := d.RecordConsumed(ctx, ticket, obs); err != nil {
		t.Fatal(err)
	}
	return ticket
}

func flowFinish(t *testing.T, d *DurableCoordinator, s *simulatedDeviceFlow, ticket Ticket) {
	t.Helper()
	ctx := context.Background()
	pods, err := s.client.CoreV1().Pods(flowNamespace).List(ctx, metav1.ListOptions{})
	if err != nil {
		t.Fatal(err)
	}
	var pod *v1.Pod
	for i := range pods.Items {
		if string(pods.Items[i].UID) == ticket.PodUID {
			pod = pods.Items[i].DeepCopy()
		}
	}
	if pod == nil || pod.Spec.NodeName != ticket.Node {
		t.Fatal("synthetic terminal Pod identity mismatch")
	}
	pod.Status.Phase = v1.PodSucceeded
	delete(pod.Annotations, flowPendingAnnotation)
	if _, err := s.client.CoreV1().Pods(flowNamespace).Update(ctx, pod, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	node, err := s.client.CoreV1().Nodes().Get(ctx, ticket.Node, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if node.Annotations[flowLockAnnotation] != "" || node.Annotations[flowPendingAnnotation] != "" {
		t.Fatal("simulated device has not released node")
	}
	// This is a synthetic reconciler assertion, not a real device release.
	err = d.Resolve(context.Background(), ticket, ReleaseObservation{PodUID: ticket.PodUID, Node: ticket.Node, ObservationID: "synthetic-release-" + ticket.PodUID, PodTerminalOrAbsent: true, DeviceReleaseVerified: true, NoInFlightAllocate: true, NodeLockUnheld: true})
	if err != nil {
		t.Fatal(err)
	}
}

func TestDurableFlowBypassRestartAndSyntheticDeviceAcknowledgements(t *testing.T) {
	ctx := context.Background()
	key := DeviceKey{Node: "fake-node", GPU: "fake-GPU-uuid"}
	objects := []runtime.Object{&v1.Node{ObjectMeta: metav1.ObjectMeta{Name: key.Node}}}
	for _, name := range []string{"small", "large-0", "large-1"} {
		objects = append(objects, &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: flowNamespace, UID: types.UID(name + "-uid")}})
	}
	client := flowCASClient(t, objects...)
	flow := &simulatedDeviceFlow{client: client}
	d, err := CreateDurable(ctx, client, flowNamespace, "ledger", map[DeviceKey]Resources{key: {Memory: 10000, Cores: 100, Instances: 10}}, map[DeviceKey]Resources{key: {Memory: 1000, Cores: 30, Instances: 1}})
	if err != nil {
		t.Fatal(err)
	}
	large := flowPlan("large-job", "large-0-uid", key, 40)
	large.Pods = append(large.Pods, flowPlan("large-job", "large-1-uid", key, 40).Pods[0])
	before := flowAssertUsed(t, d, key, 30)
	if err := d.Reserve(ctx, large); err == nil {
		t.Fatal("incomplete 2x40 plan was accepted against 30 background cores")
	}
	if after := flowAssertUsed(t, d, key, 30); !reflect.DeepEqual(before, after) {
		t.Fatalf("failed large reservation changed ledger: %+v", after)
	}
	n, err := client.CoreV1().Nodes().Get(ctx, key.Node, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if n.Annotations[flowLockAnnotation] != "" {
		t.Fatal("failed reservation published simulated NodeLock")
	}
	small := flowPlan("small-job", "small-uid", key, 60)
	if err := d.Reserve(ctx, small); err != nil {
		t.Fatalf("small job failed to bypass: %v", err)
	}
	flowAssertUsed(t, d, key, 90)
	d, err = OpenDurable(ctx, client, flowNamespace, "ledger", d.UID(), d.Epoch())
	if err != nil {
		t.Fatal(err)
	}
	smallTicket := flowConsume(t, d, flow, small, "small")
	flowFinish(t, d, flow, smallTicket)
	flowAssertUsed(t, d, key, 30)
	// The fixture now asserts the pre-existing background Pod has finished.
	err = d.ReconcileBaseline(ctx, map[DeviceKey]Resources{key: {}}, BaselineEvidence{ObservationID: "synthetic-background-release", FullInventoryVerified: true, AllActivePodsAndClaimsAccounted: true, ReservationsExcluded: true, NoUnknownInFlightWork: true})
	if err != nil {
		t.Fatal(err)
	}
	if err := d.Reserve(ctx, large); err != nil {
		t.Fatal(err)
	}
	state := flowAssertUsed(t, d, key, 80)
	if state.Pods["large-0-uid"] != Reserved || state.Pods["large-1-uid"] != Reserved {
		t.Fatal("full multi-Pod reservation missing")
	}
	d, err = OpenDurable(ctx, client, flowNamespace, "ledger", d.UID(), d.Epoch())
	if err != nil {
		t.Fatal(err)
	}
	first := flowConsume(t, d, flow, JobPlan{UID: large.UID, Pods: large.Pods[:1]}, "large-0")
	// An ordinary process reopen preserves both reservations and the first ACK.
	d, err = OpenDurable(ctx, client, flowNamespace, "ledger", d.UID(), d.Epoch())
	if err != nil {
		t.Fatal(err)
	}
	state = flowAssertUsed(t, d, key, 80)
	if state.Pods["large-0-uid"] != Consumed || state.Pods["large-1-uid"] != Reserved {
		t.Fatal("reopen lost partial progress")
	}
	second := flowConsume(t, d, flow, JobPlan{UID: large.UID, Pods: large.Pods[1:]}, "large-1")
	state = flowAssertUsed(t, d, key, 80)
	if state.Pods["large-0-uid"] != Consumed || state.Pods["large-1-uid"] != Consumed {
		t.Fatal("both simulated members must be acknowledged")
	}
	flowFinish(t, d, flow, first)
	flowFinish(t, d, flow, second)
	state = flowAssertUsed(t, d, key, 0)
	if state.Pods["large-0-uid"] != Finished || state.Pods["large-1-uid"] != Finished || len(state.Slots) != 0 {
		t.Fatal("synthetic completion left reservation or lock")
	}
}

func TestDurableFlowCrashQuarantinesAndRejectsOldTickets(t *testing.T) {
	ctx := context.Background()
	key := DeviceKey{Node: "fake-node", GPU: "fake-GPU-uuid"}
	client := flowCASClient(t, &v1.Node{ObjectMeta: metav1.ObjectMeta{Name: key.Node}}, &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "first", Namespace: flowNamespace, UID: "first-uid"}})
	flow := &simulatedDeviceFlow{client: client}
	d, err := CreateDurable(ctx, client, flowNamespace, "crash-ledger", map[DeviceKey]Resources{key: {Memory: 10000, Cores: 100, Instances: 10}}, map[DeviceKey]Resources{key: {}})
	if err != nil {
		t.Fatal(err)
	}
	job := flowPlan("large-job", "first-uid", key, 40)
	job.Pods = append(job.Pods, flowPlan("large-job", "second-uid", key, 40).Pods[0])
	if err = d.Reserve(ctx, job); err != nil {
		t.Fatal(err)
	}
	ticket, err := d.Begin(ctx, job.UID, "first-uid", key.Node)
	if err != nil {
		t.Fatal(err)
	}
	attempt := DurableAttemptID(ticket)
	flow.publish(t, key.Node, "first", "first-uid", attempt)
	if err = d.Published(ctx, ticket); err != nil {
		t.Fatal(err)
	}
	flow.bind(t, key.Node, "first", "first-uid", attempt)
	if err = d.RecordBound(ctx, ticket, BindingObservation{PodUID: "first-uid", Node: key.Node, ObservationID: "synthetic-bound"}); err != nil {
		t.Fatal(err)
	}
	if _, err = d.Begin(ctx, job.UID, "second-uid", key.Node); err == nil {
		t.Fatal("second Pod published while first device result unresolved")
	}
	old := d
	d, err = TakeoverDurable(ctx, client, flowNamespace, "crash-ledger", old.UID(), old.Epoch())
	if err != nil {
		t.Fatal(err)
	}
	if err = old.Published(ctx, ticket); err == nil {
		t.Fatal("old owner accepted after takeover")
	}
	if err = d.RecordConsumed(ctx, ticket, ConsumptionObservation{}); err == nil {
		t.Fatal("old ticket accepted after takeover")
	}
	state := flowAssertUsed(t, d, key, 80)
	if state.Pods["first-uid"] != Uncertain || state.Pods["second-uid"] != Reserved {
		t.Fatal("bound Pod was falsely rolled back")
	}
	if _, err = d.Begin(ctx, job.UID, "second-uid", key.Node); err == nil {
		t.Fatal("quarantine did not block new work")
	}
	if err = d.AcknowledgeRecovery(ctx, RecoveryEvidence{ObservationID: "incomplete"}); err == nil {
		t.Fatal("incomplete recovery evidence accepted")
	}
	n, err := client.CoreV1().Nodes().Get(ctx, key.Node, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if n.Annotations[flowLockAnnotation] != attempt {
		t.Fatal("unknown simulated lock was removed")
	}
	recovery, err := d.RecoveryTicket(ctx, job.UID, "first-uid", key.Node)
	if err != nil {
		t.Fatal(err)
	}
	if err = d.Resolve(ctx, recovery, ReleaseObservation{PodUID: "first-uid", Node: key.Node, ObservationID: "not-proven"}); err == nil {
		t.Fatal("unknown device allocation was released")
	}
	flowAssertUsed(t, d, key, 80)
	if err = d.AcknowledgeRecovery(ctx, RecoveryEvidence{ObservationID: "synthetic-fence-and-inventory", PreviousOwnerFenced: true, AllActiveAllocationsAccounted: true, NoUnknownInFlightWork: true}); err != nil {
		t.Fatal(err)
	}
	if _, err = d.Begin(ctx, job.UID, "second-uid", key.Node); err == nil {
		t.Fatal("uncertain Pod failed to block same node after global recovery")
	}
	// Local fixture now asserts external release and clears its simulated lock.
	flow.releaseNode(t, key.Node, "first-uid", attempt)
	flowFinish(t, d, flow, recovery)
	flowAssertUsed(t, d, key, 40)
	if _, err = d.Begin(ctx, job.UID, "second-uid", key.Node); err != nil {
		t.Fatalf("verified local recovery could not resume: %v", err)
	}
}
