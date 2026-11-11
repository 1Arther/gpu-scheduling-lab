// Local only: frozen RF proposal + fake API durable ledger + fake plugin.
// Neither real Kubernetes binding nor a UID-aware Device Plugin is exercised.
package vgpu

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
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
	"volcano.sh/volcano/pkg/scheduler/plugins/hrrn/coordination"
)

// Explicit compare-and-swap emulates only ConfigMap UID/resourceVersion; fake
// client-go alone does not enforce either and is not a real API server.
func rfDurableFake(objects ...runtime.Object) *fake.Clientset {
	c := fake.NewSimpleClientset(objects...)
	var mu sync.Mutex
	var version int64
	gvr := schema.GroupVersionResource{Version: "v1", Resource: "configmaps"}
	c.PrependReactor("create", "configmaps", func(a clienttesting.Action) (bool, runtime.Object, error) {
		mu.Lock()
		defer mu.Unlock()
		obj := a.(clienttesting.CreateAction).GetObject().(*v1.ConfigMap).DeepCopy()
		version++
		obj.UID = types.UID("fake-rf-ledger")
		obj.ResourceVersion = strconv.FormatInt(version, 10)
		if err := c.Tracker().Create(gvr, obj, a.GetNamespace()); err != nil {
			return true, nil, err
		}
		return true, obj.DeepCopy(), nil
	})
	c.PrependReactor("update", "configmaps", func(a clienttesting.Action) (bool, runtime.Object, error) {
		mu.Lock()
		defer mu.Unlock()
		obj := a.(clienttesting.UpdateAction).GetObject().(*v1.ConfigMap).DeepCopy()
		old, err := c.Tracker().Get(gvr, a.GetNamespace(), obj.Name)
		if err != nil {
			return true, nil, err
		}
		prev := old.(*v1.ConfigMap)
		if obj.UID != prev.UID || obj.ResourceVersion != prev.ResourceVersion {
			return true, nil, apierrors.NewConflict(schema.GroupResource{Resource: "configmaps"}, obj.Name, fmt.Errorf("fake UID/RV conflict"))
		}
		version++
		obj.ResourceVersion = strconv.FormatInt(version, 10)
		if err = c.Tracker().Update(gvr, obj, a.GetNamespace()); err != nil {
			return true, nil, err
		}
		return true, obj.DeepCopy(), nil
	})
	return c
}

// All four sources and their signatures are synthetic; this proves the local
// verifier contract, not that a real device plugin emits UID-bound receipts.
func rfSyntheticAck(t *testing.T, ticket coordination.Ticket, plan coordination.PodPlan) coordination.ConsumptionObservation {
	t.Helper()
	now := time.Unix(1750000000, 0)
	attempt := coordination.DurableAttemptID(ticket)
	anchors := map[coordination.AckKind]coordination.AckAnchor{}
	for _, k := range []coordination.AckKind{coordination.AckBinding, coordination.AckPlugin, coordination.AckCheckpoint, coordination.AckQuiescence} {
		anchors[k] = coordination.AckAnchor{Source: string(k), Key: []byte(strings.Repeat(string(k)+"-", 32))}
	}
	trust, err := coordination.NewAckTrust(anchors)
	if err != nil {
		t.Fatal(err)
	}
	proof := func(k coordination.AckKind, off int) coordination.AckArtifact {
		return coordination.AckArtifact{Kind: k, Source: string(k), EvidenceID: string(k) + "-" + plan.UID, JobUID: ticket.JobUID, PodUID: plan.UID, Node: plan.Node, AttemptID: attempt, ObservedAt: now.Add(time.Duration(off) * time.Second).UnixNano(), ExpiresAt: now.Add(time.Minute).UnixNano()}
	}
	sign := func(a coordination.AckArtifact) coordination.AckArtifact {
		x, e := coordination.SignSyntheticAck(anchors[a.Kind].Key, a)
		if e != nil {
			t.Fatal(e)
		}
		return x
	}
	b := proof(coordination.AckBinding, -4)
	b.Bound = true
	b.ResourceVersion = "fake-rv"
	raw := []byte("synthetic response for " + plan.UID)
	hash := sha256.Sum256(raw)
	p := proof(coordination.AckPlugin, -3)
	p.Container = plan.Allocations[0].Container
	p.UIDBound = true
	p.ResponseDone = true
	p.DecodeVerified = true
	p.RawResponse = raw
	p.ResponseSHA256 = hex.EncodeToString(hash[:])
	p.Allocations = plan.Allocations
	p.CandidatesOnNode = 1
	p.CandidatePodUID = plan.UID
	cp := proof(coordination.AckCheckpoint, -2)
	cp.Container = p.Container
	cp.UIDBound = true
	cp.ResponseDone = true
	cp.DecodeVerified = true
	cp.ResponseSHA256 = p.ResponseSHA256
	cp.Allocations = plan.Allocations
	q := proof(coordination.AckQuiescence, -1)
	q.NoInFlight = true
	q.NodeLockUnheld = true
	bundle := coordination.AckBundle{Binding: sign(b), Plugins: []coordination.AckArtifact{sign(p)}, Checkpoints: []coordination.AckArtifact{sign(cp)}, Quiescence: sign(q)}
	obs, err := coordination.VerifyDeviceAcknowledgement(ticket, plan, bundle, trust, now, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	return obs
}

func TestVGPUFrozenRFDurableReservationFakePlugin(t *testing.T) {
	endpoint := os.Getenv("RF_LIVE_ENDPOINT")
	if endpoint == "" {
		t.Skip("RF_LIVE_ENDPOINT required for frozen RF; never substitute a mock model")
	}
	ctx := context.Background()
	rfTestConfig(t)
	NodeLockEnable = true
	RFEndpoint = endpoint
	trace := &rfTraceTransport{base: rfHTTP.Transport}
	rfHTTP = &http.Client{Transport: trace, CheckRedirect: rfHTTP.CheckRedirect}
	node := rfTestNode("durable-node", "NVIDIA-L40", 1, 46068)
	bg := plannerBackground(node, "durable-bg", 4096, 30)
	nodes := map[string]*GPUDevices{node.Name: node}
	large := []*v1.Pod{rfTestPod("durable-large-0", "training", 4096, 40), rfTestPod("durable-large-1", "training", 4096, 40)}
	small := rfTestPod("durable-small", "inference", 4096, 60)
	objects := []runtime.Object{}
	for _, p := range append(append([]*v1.Pod{}, large...), small) {
		objects = append(objects, p.DeepCopy())
	}
	client := rfDurableFake(objects...)
	key := coordination.DeviceKey{Node: node.Name, GPU: node.Device[0].UUID}
	cap := coordination.Resources{Memory: 46068, Cores: 100, Instances: 10}
	baseline := coordination.Resources{Memory: 4096, Cores: 30, Instances: 1}
	d, err := coordination.CreateDurable(ctx, client, "default", "rf-ledger", map[coordination.DeviceKey]coordination.Resources{key: cap}, map[coordination.DeviceKey]coordination.Resources{key: baseline})
	if err != nil {
		t.Fatal(err)
	}
	before, err := d.Snapshot(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if p, err := PlanCompleteVGPUJob(nodes, large); p != nil || !errors.Is(err, ErrVGPUPlanNoCandidate) {
		t.Fatalf("temporary capacity should reject whole large plan: %v", err)
	}
	after, err := d.Snapshot(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if after.Used[key] != before.Used[key] || len(after.Pods) != 0 {
		t.Fatal("rejected full plan changed durable ledger")
	}
	sp, err := PlanCompleteVGPUJob(nodes, []*v1.Pod{small})
	if err != nil {
		t.Fatal(err)
	}
	if err = ValidateVGPUJobPlanSnapshot(nodes, sp); err != nil {
		t.Fatal(err)
	}
	sjob := proposalForModel(t, "small-job", sp, []*v1.Pod{small})
	if err = d.Reserve(ctx, sjob); err != nil {
		t.Fatal(err)
	}
	snapshot, err := d.Snapshot(ctx)
	if err != nil || snapshot.Used[key].Cores != 90 {
		t.Fatalf("small bypass was not reserved: %+v %v", snapshot, err)
	}
	consume := func(job coordination.JobPlan, member coordination.PodPlan, pod *v1.Pod) coordination.Ticket {
		ticket, e := d.Begin(ctx, job.UID, member.UID, member.Node)
		if e != nil {
			t.Fatal(e)
		}
		if e = d.Published(ctx, ticket); e != nil {
			t.Fatal(e)
		}
		// Fake object update stands in for bind; not an actual Binding API call.
		p, e := client.CoreV1().Pods(pod.Namespace).Get(ctx, pod.Name, metav1.GetOptions{})
		if e != nil {
			t.Fatal(e)
		}
		if string(p.UID) != member.UID || p.Spec.NodeName != "" {
			t.Fatal("fake Pod identity mismatch")
		}
		p.Spec.NodeName = member.Node
		if _, e = client.CoreV1().Pods(p.Namespace).Update(ctx, p, metav1.UpdateOptions{}); e != nil {
			t.Fatal(e)
		}
		if e = d.RecordBound(ctx, ticket, coordination.BindingObservation{PodUID: member.UID, Node: member.Node, ObservationID: "synthetic-fake-bind"}); e != nil {
			t.Fatal(e)
		}
		obs := rfSyntheticAck(t, ticket, member)
		if e = d.RecordConsumed(ctx, ticket, obs); e != nil {
			t.Fatal(e)
		}
		return ticket
	}
	finish := func(ticket coordination.Ticket, pod *v1.Pod) {
		p, e := client.CoreV1().Pods(pod.Namespace).Get(ctx, pod.Name, metav1.GetOptions{})
		if e != nil {
			t.Fatal(e)
		}
		if string(p.UID) != ticket.PodUID {
			t.Fatal("synthetic terminal UID changed")
		}
		p.Status.Phase = v1.PodSucceeded
		if _, e = client.CoreV1().Pods(p.Namespace).Update(ctx, p, metav1.UpdateOptions{}); e != nil {
			t.Fatal(e)
		}
		if e = d.Resolve(ctx, ticket, simulatedRelease(ticket)); e != nil {
			t.Fatal(e)
		}
	}
	st := consume(sjob, sjob.Pods[0], small)
	finish(st, small)
	node.SubResource(bg)
	if err = d.ReconcileBaseline(ctx, map[coordination.DeviceKey]coordination.Resources{key: {}}, coordination.BaselineEvidence{ObservationID: "synthetic-background-release", FullInventoryVerified: true, AllActivePodsAndClaimsAccounted: true, ReservationsExcluded: true, NoUnknownInFlightWork: true}); err != nil {
		t.Fatal(err)
	}
	lp, err := PlanCompleteVGPUJob(nodes, large)
	if err != nil {
		t.Fatal(err)
	}
	if err = ValidateVGPUJobPlanSnapshot(nodes, lp); err != nil {
		t.Fatal(err)
	}
	job := proposalForModel(t, "large-job", lp, large)
	if err = d.Reserve(ctx, job); err != nil {
		t.Fatal(err)
	}
	snapshot, err = d.Snapshot(ctx)
	if err != nil || snapshot.Used[key].Cores != 80 || len(snapshot.Pods) != 3 {
		t.Fatalf("full reservation missing %+v %v", snapshot, err)
	}
	// Reopening the same cooperative epoch demonstrates storage persistence,
	// not a fenced process restart. Takeover recovery has separate local tests.
	d, err = coordination.OpenDurable(ctx, client, "default", "rf-ledger", d.UID(), d.Epoch())
	if err != nil {
		t.Fatal(err)
	}
	first := consume(job, job.Pods[0], large[0])
	second := consume(job, job.Pods[1], large[1])
	finish(first, large[0])
	finish(second, large[1])
	snapshot, err = d.Snapshot(ctx)
	if err != nil || snapshot.Used[key] != (coordination.Resources{}) {
		t.Fatalf("durable release leak %+v %v", snapshot, err)
	}
	if len(trace.rows) == 0 {
		t.Fatal("no RF service responses observed")
	}
	for _, row := range trace.rows {
		var response rfResponse
		if err := json.Unmarshal(row["response"].(json.RawMessage), &response); err != nil ||
			row["http_status"] != http.StatusOK || response.ModelVersion != RFModelVersion ||
			!response.ModelReady || response.PredictionEnabled == nil || !*response.PredictionEnabled || response.Status != "ranked" {
			t.Fatalf("unexpected frozen RF response: %+v, %v", row, err)
		}
	}
	t.Logf("verified %d frozen RF responses; fake API binding and simulated per-Pod device ACK", len(trace.rows))
}
