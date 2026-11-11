package coordination

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"reflect"
	"testing"
	"time"

	devicecheckpoint "k8s.io/kubernetes/pkg/kubelet/cm/devicemanager/checkpoint"
)

var ackNow = time.Date(2026, 10, 27, 12, 0, 0, 0, time.UTC)

func ackAnchors() map[AckKind]AckAnchor {
	return map[AckKind]AckAnchor{
		AckBinding:    {Source: "fake-kube-api", Key: bytes.Repeat([]byte{1}, 32)},
		AckPlugin:     {Source: "fake-uid-aware-plugin", Key: bytes.Repeat([]byte{2}, 32)},
		AckCheckpoint: {Source: "fake-kubelet", Key: bytes.Repeat([]byte{3}, 32)},
		AckQuiescence: {Source: "fake-node-monitor", Key: bytes.Repeat([]byte{4}, 32)},
	}
}

func newAckFixture(t *testing.T) (*Coordinator, Ticket, PodPlan, *AckTrust, AckBundle) {
	t.Helper()
	dev0 := DeviceKey{Node: "node-0", GPU: "GPU-A"}
	dev1 := DeviceKey{Node: "node-0", GPU: "GPU-B"}
	pod := PodPlan{UID: "pod-uid-1", Node: "node-0", Allocations: []Allocation{
		{Container: "train", Device: dev0, Resources: Resources{Memory: 4000, Cores: 40, Instances: 1}},
		{Container: "train", Device: dev1, Resources: Resources{Memory: 3000, Cores: 20, Instances: 1}},
		{Container: "helper", Device: dev1, Resources: Resources{Memory: 1000, Cores: 10, Instances: 1}},
	}}
	c, err := New(map[DeviceKey]Resources{dev0: {Memory: 10000, Cores: 100, Instances: 10}, dev1: {Memory: 10000, Cores: 100, Instances: 10}}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Reserve(JobPlan{UID: "job-uid-1", Pods: []PodPlan{pod}}); err != nil {
		t.Fatal(err)
	}
	ticket, err := c.Begin("job-uid-1", pod.UID, pod.Node)
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Published(ticket); err != nil {
		t.Fatal(err)
	}
	if err := c.RecordBound(ticket, BindingObservation{PodUID: pod.UID, Node: pod.Node, ObservationID: "fake-api-bound"}); err != nil {
		t.Fatal(err)
	}
	anchors := ackAnchors()
	trust, err := NewAckTrust(anchors)
	if err != nil {
		t.Fatal(err)
	}
	base := func(kind AckKind, id string, at time.Time) AckArtifact {
		return AckArtifact{Kind: kind, Source: anchors[kind].Source, EvidenceID: id, JobUID: ticket.JobUID,
			PodUID: ticket.PodUID, Node: ticket.Node, AttemptID: DurableAttemptID(ticket), ObservedAt: at.UnixNano(), ExpiresAt: ackNow.Add(time.Minute).UnixNano()}
	}
	bind := base(AckBinding, "api-1", ackNow.Add(-4*time.Second))
	bind.Bound, bind.ResourceVersion = true, "123"
	q := base(AckQuiescence, "node-1", ackNow.Add(-time.Second))
	q.NoInFlight, q.NodeLockUnheld = true, true
	bundle := AckBundle{Binding: bind, Quiescence: q}
	for _, container := range []string{"train", "helper"} {
		allocs := []Allocation{}
		for _, a := range pod.Allocations {
			if a.Container == container {
				allocs = append(allocs, a)
			}
		}
		response := []byte("simulated AllocateResponse for " + container)
		d := sha256.Sum256(response)
		plugin := base(AckPlugin, "plugin-"+container, ackNow.Add(-3*time.Second))
		plugin.Container, plugin.UIDBound, plugin.ResponseDone, plugin.DecodeVerified = container, true, true, true
		plugin.RawResponse, plugin.ResponseSHA256, plugin.Allocations = response, hex.EncodeToString(d[:]), allocs
		plugin.CandidatesOnNode, plugin.CandidatePodUID = 1, pod.UID
		checkpoint := base(AckCheckpoint, "checkpoint-"+container, ackNow.Add(-2*time.Second))
		checkpoint.Container, checkpoint.UIDBound, checkpoint.ResponseDone, checkpoint.DecodeVerified = container, true, true, true
		checkpoint.ResponseSHA256, checkpoint.Allocations = plugin.ResponseSHA256, append([]Allocation(nil), allocs...)
		bundle.Plugins, bundle.Checkpoints = append(bundle.Plugins, plugin), append(bundle.Checkpoints, checkpoint)
	}
	return c, ticket, pod, trust, signBundle(t, bundle, anchors)
}

func signBundle(t *testing.T, b AckBundle, anchors map[AckKind]AckAnchor) AckBundle {
	t.Helper()
	sign := func(p AckArtifact) AckArtifact {
		q, err := SignSyntheticAck(anchors[p.Kind].Key, p)
		if err != nil {
			t.Fatal(err)
		}
		return q
	}
	b.Binding = sign(b.Binding)
	for i := range b.Plugins {
		b.Plugins[i] = sign(b.Plugins[i])
	}
	for i := range b.Checkpoints {
		b.Checkpoints[i] = sign(b.Checkpoints[i])
	}
	b.Quiescence = sign(b.Quiescence)
	return b
}

func verifyAck(t Ticket, p PodPlan, b AckBundle, trust *AckTrust) (ConsumptionObservation, error) {
	return VerifyDeviceAcknowledgement(t, p, b, trust, ackNow, 30*time.Second)
}

func TestAuthenticatedSyntheticAckRequiresEveryContainerAndIsOneShot(t *testing.T) {
	c, ticket, pod, trust, bundle := newAckFixture(t)
	obs, err := verifyAck(ticket, pod, bundle, trust)
	if err != nil {
		t.Fatal(err)
	}
	if !obs.BoundUIDVerified || !obs.PluginResponseVerified || !obs.KubeletCheckpointVerified || !reflect.DeepEqual(obs.Allocations, pod.Allocations) {
		t.Fatal(obs)
	}
	if err := c.RecordConsumed(ticket, obs); err != nil {
		t.Fatal(err)
	}
	if c.Snapshot().Pods[pod.UID] != Consumed {
		t.Fatal("model did not enter consumed state")
	}
	if err := c.RecordConsumed(ticket, obs); !errors.Is(err, ErrRejected) {
		t.Fatalf("replay accepted: %v", err)
	}
}

func TestAckRejectsUntrustedTamperedOrForeignIdentityWithoutStateMutation(t *testing.T) {
	for _, tc := range []struct {
		name   string
		change func(*AckBundle)
	}{
		{"job", func(b *AckBundle) { b.Binding.JobUID = "other-job" }},
		{"pod", func(b *AckBundle) { b.Plugins[0].PodUID = "other-pod" }},
		{"node", func(b *AckBundle) { b.Checkpoints[0].Node = "other-node" }},
		{"attempt", func(b *AckBundle) { b.Quiescence.AttemptID = "old-epoch:1" }},
		{"source", func(b *AckBundle) { b.Plugins[0].Source = "untrusted-plugin" }},
		{"kind", func(b *AckBundle) { b.Plugins[0].Kind = AckCheckpoint }},
		{"tampered-signature", func(b *AckBundle) { b.Plugins[0].Signature[0] ^= 0x80 }},
		{"tampered-payload", func(b *AckBundle) { b.Plugins[0].RawResponse[0] ^= 1 }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c, ticket, pod, trust, bundle := newAckFixture(t)
			before := c.Snapshot()
			tc.change(&bundle)
			if _, err := verifyAck(ticket, pod, bundle, trust); !errors.Is(err, ErrRejected) {
				t.Fatalf("bad evidence accepted: %v", err)
			}
			if !reflect.DeepEqual(before, c.Snapshot()) {
				t.Fatal("verification changed coordinator state")
			}
		})
	}
	_, ticket, pod, _, bundle := newAckFixture(t)
	if _, err := verifyAck(ticket, pod, bundle, nil); !errors.Is(err, ErrRejected) {
		t.Fatalf("nil trust accepted: %v", err)
	}
}

func TestAckRejectsAuthenticatedButIncompleteOrWrongEvidence(t *testing.T) {
	for _, tc := range []struct {
		name   string
		change func(*AckBundle)
	}{
		{"not-bound", func(b *AckBundle) { b.Binding.Bound = false }},
		{"no-api-revision", func(b *AckBundle) { b.Binding.ResourceVersion = "" }},
		{"uid-guessed", func(b *AckBundle) { b.Plugins[0].UIDBound = false }},
		{"plugin-not-complete", func(b *AckBundle) { b.Plugins[0].ResponseDone = false }},
		{"decode-not-proven", func(b *AckBundle) { b.Plugins[0].DecodeVerified = false }},
		{"no-raw-response", func(b *AckBundle) { b.Plugins[0].RawResponse = nil }},
		{"ambiguous-candidates", func(b *AckBundle) { b.Plugins[0].CandidatesOnNode = 2 }},
		{"wrong-selected-uid", func(b *AckBundle) { b.Plugins[0].CandidatePodUID = "other" }},
		{"wrong-plugin-digest", func(b *AckBundle) { b.Plugins[0].ResponseSHA256 = "bad" }},
		{"wrong-checkpoint-digest", func(b *AckBundle) { b.Checkpoints[0].ResponseSHA256 = "bad" }},
		{"wrong-uuid", func(b *AckBundle) { b.Plugins[0].Allocations[0].Device.GPU = "GPU-OTHER" }},
		{"wrong-memory", func(b *AckBundle) { b.Plugins[0].Allocations[0].Resources.Memory++ }},
		{"wrong-cores", func(b *AckBundle) { b.Plugins[0].Allocations[0].Resources.Cores++ }},
		{"wrong-instances", func(b *AckBundle) { b.Plugins[0].Allocations[0].Resources.Instances++ }},
		{"wrong-container", func(b *AckBundle) { b.Checkpoints[0].Allocations[0].Container = "other" }},
		{"no-checkpoint", func(b *AckBundle) { b.Checkpoints = b.Checkpoints[:1] }},
		{"duplicate-container", func(b *AckBundle) { b.Plugins[1] = b.Plugins[0]; b.Plugins[1].EvidenceID = "different-id" }},
		{"duplicate-evidence-id", func(b *AckBundle) { b.Quiescence.EvidenceID = b.Binding.EvidenceID }},
		{"inflight", func(b *AckBundle) { b.Quiescence.NoInFlight = false }},
		{"lock-held", func(b *AckBundle) { b.Quiescence.NodeLockUnheld = false }},
		{"still-active-candidate", func(b *AckBundle) { b.Quiescence.CandidatesOnNode = 1 }},
		{"candidate-name-not-empty", func(b *AckBundle) { b.Quiescence.CandidatePodUID = "other" }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c, ticket, pod, trust, bundle := newAckFixture(t)
			before := c.Snapshot()
			tc.change(&bundle)
			bundle = signBundle(t, bundle, ackAnchors()) // trusted but wrong/incomplete assertions
			if _, err := verifyAck(ticket, pod, bundle, trust); !errors.Is(err, ErrRejected) {
				t.Fatalf("invalid evidence accepted: %v", err)
			}
			if !reflect.DeepEqual(before, c.Snapshot()) {
				t.Fatal("failed verification changed reservation or node slot")
			}
		})
	}
}

func TestAckRejectsStaleFutureExpiredOutOfOrderAndOldTicket(t *testing.T) {
	for _, tc := range []struct {
		name   string
		change func(*AckBundle)
	}{
		{"old", func(b *AckBundle) { b.Binding.ObservedAt = ackNow.Add(-31 * time.Second).UnixNano() }},
		{"future", func(b *AckBundle) { b.Plugins[0].ObservedAt = ackNow.Add(time.Second).UnixNano() }},
		{"expired", func(b *AckBundle) { b.Quiescence.ExpiresAt = ackNow.Add(-time.Nanosecond).UnixNano() }},
		{"before-binding", func(b *AckBundle) { b.Plugins[0].ObservedAt = ackNow.Add(-5 * time.Second).UnixNano() }},
		{"checkpoint-before-plugin", func(b *AckBundle) { b.Checkpoints[0].ObservedAt = ackNow.Add(-4 * time.Second).UnixNano() }},
		{"node-before-checkpoint", func(b *AckBundle) { b.Quiescence.ObservedAt = ackNow.Add(-3 * time.Second).UnixNano() }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, ticket, pod, trust, bundle := newAckFixture(t)
			tc.change(&bundle)
			bundle = signBundle(t, bundle, ackAnchors())
			if _, err := verifyAck(ticket, pod, bundle, trust); !errors.Is(err, ErrRejected) {
				t.Fatalf("bad timing accepted: %v", err)
			}
		})
	}
	_, ticket, pod, trust, bundle := newAckFixture(t)
	ticket.epoch[0] ^= 1
	if _, err := verifyAck(ticket, pod, bundle, trust); !errors.Is(err, ErrRejected) {
		t.Fatalf("old epoch evidence accepted: %v", err)
	}
}

func TestAckTrustRejectsMissingWeakAndUnexpectedAnchors(t *testing.T) {
	missing := ackAnchors()
	delete(missing, AckBinding)
	if _, err := NewAckTrust(missing); !errors.Is(err, ErrRejected) {
		t.Fatal(err)
	}
	weak := ackAnchors()
	weak[AckPlugin] = AckAnchor{Source: "weak", Key: []byte("weak")}
	if _, err := NewAckTrust(weak); !errors.Is(err, ErrRejected) {
		t.Fatal(err)
	}
	extra := ackAnchors()
	extra["extra"] = AckAnchor{Source: "extra", Key: bytes.Repeat([]byte{9}, 32)}
	if _, err := NewAckTrust(extra); !errors.Is(err, ErrRejected) {
		t.Fatal(err)
	}
}

func TestAckTrustRejectsSharedSourcesAndEffectiveKeys(t *testing.T) {
	tests := []struct {
		name string
		edit func(map[AckKind]AckAnchor)
	}{
		{"same-source", func(a map[AckKind]AckAnchor) {
			x := a[AckPlugin]
			x.Source = a[AckBinding].Source
			a[AckPlugin] = x
		}},
		{"same-key", func(a map[AckKind]AckAnchor) {
			x := a[AckPlugin]
			x.Key = append([]byte(nil), a[AckBinding].Key...)
			a[AckPlugin] = x
		}},
		{"zero-padded-key", func(a map[AckKind]AckAnchor) {
			x := a[AckPlugin]
			x.Key = append(append([]byte(nil), a[AckBinding].Key...), 0)
			a[AckPlugin] = x
		}},
		{"long-hashed-key", func(a map[AckKind]AckAnchor) {
			long := bytes.Repeat([]byte{42}, sha256.BlockSize+1)
			digest := sha256.Sum256(long)
			x := a[AckBinding]
			x.Key = long
			a[AckBinding] = x
			x = a[AckPlugin]
			x.Key = digest[:]
			a[AckPlugin] = x
		}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			anchors := ackAnchors()
			tt.edit(anchors)
			if _, err := NewAckTrust(anchors); !errors.Is(err, ErrRejected) {
				t.Fatalf("shared trust anchor accepted: %v", err)
			}
		})
	}
}

func TestUncheckedCheckpointArtifactDoesNotCreateTrustedAck(t *testing.T) {
	device := DeviceKey{Node: "node", GPU: "GPU-123"}
	pod := PodPlan{UID: "pod-uid", Node: "node", Allocations: []Allocation{{Container: "sleep", Device: device, Resources: Resources{Memory: 4096, Cores: 40, Instances: 1}}}}
	c, err := New(map[DeviceKey]Resources{device: {Memory: 8192, Cores: 100, Instances: 10}}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Reserve(JobPlan{UID: "job-uid", Pods: []PodPlan{pod}}); err != nil {
		t.Fatal(err)
	}
	ticket, err := c.Begin("job-uid", pod.UID, pod.Node)
	if err != nil {
		t.Fatal(err)
	}
	response := receiptTestResponse("GPU-123")
	raw := receiptCheckpoint(t, []devicecheckpoint.PodDevicesEntry{receiptEntry(pod.UID, response)}, []string{"virtual-slot-1"})
	before := c.Snapshot()
	artifact, err := CheckpointArtifactFromUncheckedSource(ticket, pod, raw, "sleep", "volcano.sh/vgpu-number", "fake-kubelet", "synthetic-id", ackNow.Add(-time.Second), ackNow.Add(time.Minute))
	if err != nil {
		t.Fatal(err)
	}
	if artifact.ResponseSHA256 != hashBytes(response) || len(artifact.Signature) != 0 || artifact.PodUID != pod.UID {
		t.Fatalf("incorrect unchecked artifact: %+v", artifact)
	}
	trust, err := NewAckTrust(ackAnchors())
	if err != nil {
		t.Fatal(err)
	}
	if err := trust.verify(artifact, AckCheckpoint, ticket, DurableAttemptID(ticket), ackNow, time.Minute); !errors.Is(err, ErrRejected) {
		t.Fatalf("unsigned record was treated as authenticated: %v", err)
	}
	if !reflect.DeepEqual(before, c.Snapshot()) {
		t.Fatal("parsing checkpoint changed reservation")
	}
	wrong := pod
	wrong.Allocations = append([]Allocation(nil), pod.Allocations...)
	wrong.Allocations[0].Resources.Cores++
	if _, err := CheckpointArtifactFromUncheckedSource(ticket, wrong, raw, "sleep", "volcano.sh/vgpu-number", "fake-kubelet", "id", ackNow.Add(-time.Second), ackNow.Add(time.Minute)); err == nil {
		t.Fatal("mismatched plan accepted")
	}
	if _, err := CheckpointArtifactFromUncheckedSource(ticket, pod, raw, "unknown", "volcano.sh/vgpu-number", "fake-kubelet", "id", ackNow.Add(-time.Second), ackNow.Add(time.Minute)); err == nil {
		t.Fatal("unknown container accepted")
	}
}

// Historical data is an optional read-only fixture. The deliberately old
// artificial time is only for a stale-evidence test, not its collection time.
func TestUncheckedCheckpointArtifactPriorHistoricalFixture(t *testing.T) {
	path := os.Getenv("HRRN_KUBELET_CHECKPOINT_FIXTURE")
	if path == "" {
		t.Skip("historical checkpoint fixture not provided")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	device := DeviceKey{Node: "t3dgq", GPU: "GPU-fb6aaeaf-9ca4-3c9a-7b3b-f56714c1f7cd"}
	pod := PodPlan{UID: "3e58592a-6846-497a-9c48-8211517a49b2", Node: "t3dgq", Allocations: []Allocation{{Container: "sleep", Device: device, Resources: Resources{Memory: 4096, Cores: 100, Instances: 1}}}}
	c, err := New(map[DeviceKey]Resources{device: {Memory: 24564, Cores: 100, Instances: 10}}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Reserve(JobPlan{UID: "synthetic-job", Pods: []PodPlan{pod}}); err != nil {
		t.Fatal(err)
	}
	ticket, err := c.Begin("synthetic-job", pod.UID, pod.Node)
	if err != nil {
		t.Fatal(err)
	}
	old := time.Unix(1, 0)
	artifact, err := CheckpointArtifactFromUncheckedSource(ticket, pod, raw, "sleep", "volcano.sh/vgpu-number", "fake-kubelet", "historical-only", old, old.Add(time.Second))
	if err != nil {
		t.Fatal(err)
	}
	if len(artifact.Signature) != 0 {
		t.Fatal("historical disk data became authenticated")
	}
	artifact, err = SignSyntheticAck(ackAnchors()[AckCheckpoint].Key, artifact)
	if err != nil {
		t.Fatal(err)
	}
	trust, err := NewAckTrust(ackAnchors())
	if err != nil {
		t.Fatal(err)
	}
	if err := trust.verify(artifact, AckCheckpoint, ticket, DurableAttemptID(ticket), ackNow, time.Minute); !errors.Is(err, ErrRejected) {
		t.Fatalf("stale historical checkpoint became live proof: %v", err)
	}
}
