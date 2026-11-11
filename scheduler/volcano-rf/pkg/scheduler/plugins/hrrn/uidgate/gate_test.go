package uidgate

import (
	"context"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	"google.golang.org/protobuf/encoding/protowire"
	devicecheckpoint "k8s.io/kubernetes/pkg/kubelet/cm/devicemanager/checkpoint"
	"volcano.sh/volcano/pkg/scheduler/plugins/hrrn/coordination"
)

// Synthetic, single-process fixtures only: they do not implement external
// fencing, API authentication or a real kubelet/device plugin.
type journal struct {
	prepared  *Prepared
	uncertain bool
	confirms  int
}

func (j *journal) Prepare(_ context.Context, p Prepared) error {
	if j.uncertain || j.prepared != nil {
		return fmt.Errorf("busy or uncertain")
	}
	j.prepared = &p
	return nil
}
func (j *journal) Lookup(_ context.Context, node string, ids []string) (Prepared, error) {
	if j.uncertain || j.prepared == nil || j.prepared.Claim.Node != node || !sameIDs(j.prepared.DeviceIDs, ids) {
		return Prepared{}, fmt.Errorf("not active")
	}
	return *j.prepared, nil
}
func (j *journal) Confirm(_ context.Context, p Prepared) error {
	if j.uncertain || j.prepared == nil || j.prepared.Nonce != p.Nonce || j.prepared.Claim.Attempt != p.Claim.Attempt {
		return fmt.Errorf("stale")
	}
	j.confirms++
	return nil
}

type observer struct {
	raw      []byte
	observed time.Time
	err      error
}

func (o *observer) ReadAndVerify(_ context.Context, _ Claim) ([]byte, time.Time, error) {
	return o.raw, o.observed, o.err
}

func pair(k, v string) []byte {
	var msg []byte
	msg = protowire.AppendTag(msg, 1, protowire.BytesType)
	msg = protowire.AppendString(msg, k)
	msg = protowire.AppendTag(msg, 2, protowire.BytesType)
	msg = protowire.AppendString(msg, v)
	var out []byte
	out = protowire.AppendTag(out, 1, protowire.BytesType)
	return protowire.AppendBytes(out, msg)
}
func baseResponse() []byte {
	var out []byte
	for _, kv := range [][2]string{{"NVIDIA_VISIBLE_DEVICES", "GPU-A"}, {"CUDA_DEVICE_MEMORY_LIMIT_0", "4096m"}, {"CUDA_DEVICE_SM_LIMIT", "40"}} {
		out = append(out, pair(kv[0], kv[1])...)
	}
	return out
}
func claim() Claim {
	return Claim{JobUID: "job-a", PodUID: "pod-a-uid", Node: "node", Container: "worker", Resource: "volcano.sh/vgpu-number", Attempt: "epoch:1", Allocations: []coordination.Allocation{{Container: "worker", Device: coordination.DeviceKey{Node: "node", GPU: "GPU-A"}, Resources: coordination.Resources{Memory: 4096, Cores: 40, Instances: 1}}}}
}
func entry(uid, container string, ids []string, response []byte) devicecheckpoint.PodDevicesEntry {
	return devicecheckpoint.PodDevicesEntry{PodUID: uid, ContainerName: container, ResourceName: "volcano.sh/vgpu-number", DeviceIDs: devicecheckpoint.DevicesPerNUMA{-1: ids}, AllocResp: response}
}
func checkpoint(t *testing.T, rows ...devicecheckpoint.PodDevicesEntry) []byte {
	t.Helper()
	cp := devicecheckpoint.New(rows, map[string][]string{"volcano.sh/vgpu-number": {"slot-1", "slot-2"}})
	raw, err := cp.MarshalCheckpoint()
	if err != nil {
		t.Fatal(err)
	}
	return raw
}
func fixture(t *testing.T) (*Gate, *journal, *observer, []byte) {
	t.Helper()
	now := time.Unix(1770000000, 0)
	j := &journal{}
	o := &observer{observed: now}
	g := &Gate{Journal: j, Observer: o, Now: func() time.Time { return now }, MaxAge: 10 * time.Second}
	response, err := g.PrepareAllocate(context.Background(), claim(), []string{"slot-1"}, baseResponse())
	if err != nil {
		t.Fatal(err)
	}
	o.raw = checkpoint(t, entry("pod-a-uid", "worker", []string{"slot-1"}, response))
	return g, j, o, response
}

func TestPreStartBindsRecordedNonceToKubeletUID(t *testing.T) {
	g, j, _, _ := fixture(t)
	p, err := g.CheckPreStart(context.Background(), "node", []string{"slot-1"})
	if err != nil {
		t.Fatal(err)
	}
	if p.Claim.PodUID != "pod-a-uid" || p.Nonce == "" || j.confirms != 1 {
		t.Fatal("wrong confirmation")
	}
	// Repeated call represents container restart only under a trusted observer
	// and an idempotent journal; no claim is made about real in-flight fencing.
	if _, err = g.CheckPreStart(context.Background(), "node", []string{"slot-1"}); err != nil {
		t.Fatal(err)
	}
}

func TestSwappedSameShapeRequestAndUIDReuseFailClosed(t *testing.T) {
	g, j, o, response := fixture(t)
	// Kubelet request belongs to B even though plugin had published A's plan.
	o.raw = checkpoint(t, entry("pod-b-uid", "worker", []string{"slot-1"}, response))
	if _, err := g.CheckPreStart(context.Background(), "node", []string{"slot-1"}); err == nil {
		t.Fatal("swapped request accepted")
	}
	// Historical A record coexisting with B must not be used to bless B.
	o.raw = checkpoint(t, entry("pod-a-uid", "worker", []string{"slot-1"}, response), entry("pod-b-uid", "worker", []string{"slot-1"}, response))
	if _, err := g.CheckPreStart(context.Background(), "node", []string{"slot-1"}); err == nil {
		t.Fatal("overlapping historical record accepted")
	}
	if j.confirms != 0 {
		t.Fatal("incorrectly confirmed")
	}
}

func TestReusedDeviceIDOldCheckpointAndRetryFailClosed(t *testing.T) {
	g, j, o, old := fixture(t)
	// New attempt reuses the same ID. Its nonce differs; an old disk record
	// must not validate it even when the Pod UID and quotas look identical.
	j.prepared = nil
	c := claim()
	c.Attempt = "epoch:2"
	newResponse, err := g.PrepareAllocate(context.Background(), c, []string{"slot-1"}, baseResponse())
	if err != nil {
		t.Fatal(err)
	}
	if strings.EqualFold(string(old), string(newResponse)) {
		t.Fatal("nonces unexpectedly equal")
	}
	o.raw = checkpoint(t, entry(c.PodUID, c.Container, []string{"slot-1"}, old))
	if _, err = g.CheckPreStart(context.Background(), "node", []string{"slot-1"}); err == nil {
		t.Fatal("old nonce accepted")
	}
	// An ambiguous previous write must retain isolation across restart/retry.
	j.uncertain = true
	o.raw = checkpoint(t, entry(c.PodUID, c.Container, []string{"slot-1"}, newResponse))
	if _, err = g.CheckPreStart(context.Background(), "node", []string{"slot-1"}); err == nil {
		t.Fatal("uncertain journal accepted")
	}
	if _, err = g.PrepareAllocate(context.Background(), c, []string{"slot-1"}, baseResponse()); err == nil {
		t.Fatal("uncertain retry accepted")
	}
}

func TestRejectsWrongContainerQuotaIDsOrStaleEvidence(t *testing.T) {
	for _, which := range []string{"container", "quota", "ids", "stale", "duplicate-id", "observer"} {
		t.Run(which, func(t *testing.T) {
			g, j, o, response := fixture(t)
			ids := []string{"slot-1"}
			switch which {
			case "container":
				o.raw = checkpoint(t, entry("pod-a-uid", "other", ids, response))
			case "quota":
				j.prepared.Claim.Allocations[0].Resources.Cores = 39
			case "ids":
				ids = []string{"slot-2"}
			case "stale":
				o.observed = o.observed.Add(-time.Minute)
			case "duplicate-id":
				ids = []string{"slot-1", "slot-1"}
			case "observer":
				o.err = fmt.Errorf("observer not fenced")
			}
			if _, err := g.CheckPreStart(context.Background(), "node", ids); err == nil {
				t.Fatal("invalid evidence accepted")
			}
			if j.confirms != 0 {
				t.Fatal("invalid evidence confirmed")
			}
		})
	}
}

func TestPrepareRejectsDoublePublishAndInputNonce(t *testing.T) {
	g, _, _, _ := fixture(t)
	if _, err := g.PrepareAllocate(context.Background(), claim(), []string{"slot-1"}, baseResponse()); err == nil {
		t.Fatal("double publish accepted")
	}
	j := &journal{}
	g.Journal = j
	if _, err := g.PrepareAllocate(context.Background(), claim(), []string{"slot-1"}, append(baseResponse(), pair(nonceEnv, "forged")...)); err == nil {
		t.Fatal("caller nonce accepted")
	}
}

func TestPrepareDoesNotAliasCallerPlanOrIDs(t *testing.T) {
	j := &journal{}
	g := &Gate{Journal: j}
	c := claim()
	ids := []string{"slot-1"}
	if _, err := g.PrepareAllocate(context.Background(), c, ids, baseResponse()); err != nil {
		t.Fatal(err)
	}
	c.Allocations[0].Device.GPU = "changed"
	c.Allocations[0].Resources.Cores = 99
	ids[0] = "changed"
	if j.prepared.Claim.Allocations[0].Device.GPU != "GPU-A" || j.prepared.Claim.Allocations[0].Resources.Cores != 40 || j.prepared.DeviceIDs[0] != "slot-1" {
		t.Fatal("saved reservation aliases caller input")
	}
}

// Synthetic wire reordering tests map semantics; this does not replace an
// actual generated pluginapi/kubelet decode and re-encode compatibility test.
func TestReorderedMapBytesPreserveSemanticDigest(t *testing.T) {
	g, j, o, response := fixture(t)
	entries, err := fields(response, 1)
	if err != nil || len(entries) < 2 {
		t.Fatalf("bad response fixture: %v", err)
	}
	var reordered []byte
	for i := len(entries) - 1; i >= 0; i-- {
		reordered = protowire.AppendTag(reordered, 1, protowire.BytesType)
		reordered = protowire.AppendBytes(reordered, entries[i])
	}
	o.raw = checkpoint(t, entry("pod-a-uid", "worker", []string{"slot-1"}, reordered))
	if _, err = g.CheckPreStart(context.Background(), "node", []string{"slot-1"}); err != nil {
		t.Fatalf("same map semantics were rejected: %v", err)
	}
	if j.confirms != 1 {
		t.Fatal("reordered response was not confirmed")
	}
}

func TestResponseAdditionalFieldsAndUnknownFieldsAreRejected(t *testing.T) {
	for _, field := range []protowire.Number{2, 3, 4, 5, 31} {
		t.Run(fmt.Sprint(field), func(t *testing.T) {
			g, j, o, response := fixture(t)
			var nested []byte
			nested = protowire.AppendTag(nested, 1, protowire.BytesType)
			nested = protowire.AppendString(nested, "unexpected")
			if field == 4 {
				nested = protowire.AppendTag(nested, 2, protowire.BytesType)
				nested = protowire.AppendString(nested, "value")
			}
			changed := append([]byte(nil), response...)
			changed = protowire.AppendTag(changed, field, protowire.BytesType)
			changed = protowire.AppendBytes(changed, nested)
			o.raw = checkpoint(t, entry("pod-a-uid", "worker", []string{"slot-1"}, changed))
			if _, err := g.CheckPreStart(context.Background(), "node", []string{"slot-1"}); err == nil {
				t.Fatal("changed or unknown response field accepted")
			}
			if j.confirms != 0 {
				t.Fatal("changed or unknown response confirmed")
			}
		})
	}
}

func TestSamePodDifferentContainersAreCheckedSeparately(t *testing.T) {
	g, j, o, first := fixture(t)
	if _, err := g.CheckPreStart(context.Background(), "node", []string{"slot-1"}); err != nil {
		t.Fatal(err)
	}
	// A real journal may release the next slot only after verified quiescence;
	// this fixture models that boundary rather than proving external fencing.
	j.prepared = nil
	secondClaim := claim()
	secondClaim.Container = "helper"
	secondClaim.Attempt = "epoch:2"
	secondClaim.Allocations[0].Container = "helper"
	second, err := g.PrepareAllocate(context.Background(), secondClaim, []string{"slot-2"}, baseResponse())
	if err != nil {
		t.Fatal(err)
	}
	o.raw = checkpoint(t, entry(secondClaim.PodUID, "worker", []string{"slot-1"}, first), entry(secondClaim.PodUID, "helper", []string{"slot-2"}, second))
	if _, err = g.CheckPreStart(context.Background(), "node", []string{"slot-2"}); err != nil {
		t.Fatal(err)
	}
}

// This inspects a previously captured kubelet checkpoint response as input,
// then artificially reorders map entries. It is not a real pluginapi
// decode/re-encode and says nothing about current node state.
func TestPriorCheckpointResponseSemanticDigest(t *testing.T) {
	path := os.Getenv("HRRN_KUBELET_CHECKPOINT_FIXTURE")
	if path == "" {
		t.Skip("saved kubelet checkpoint fixture is not configured")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	cp := devicecheckpoint.New(nil, nil)
	if err = cp.UnmarshalCheckpoint(raw); err != nil {
		t.Fatal(err)
	}
	if err = cp.VerifyChecksum(); err != nil {
		t.Fatal(err)
	}
	entries, _ := cp.GetData()
	var response []byte
	for _, row := range entries {
		if row.PodUID == "3e58592a-6846-497a-9c48-8211517a49b2" && row.ContainerName == "sleep" && row.ResourceName == "volcano.sh/vgpu-number" {
			if response != nil {
				t.Fatal("duplicate recorded response")
			}
			response = row.AllocResp
		}
	}
	if len(response) == 0 {
		t.Fatal("saved Pod response not found")
	}
	digest, err := responseSemanticDigest(response)
	if err != nil {
		t.Fatal(err)
	}
	responseFields, err := decodeFields(response, map[protowire.Number]protowire.Type{1: protowire.BytesType, 2: protowire.BytesType, 3: protowire.BytesType, 4: protowire.BytesType, 5: protowire.BytesType})
	if err != nil {
		t.Fatal(err)
	}
	var reordered []byte
	var envs [][]byte
	for _, field := range responseFields {
		if field.number == 1 {
			envs = append(envs, field.data)
			continue
		}
		reordered = protowire.AppendTag(reordered, field.number, protowire.BytesType)
		reordered = protowire.AppendBytes(reordered, field.data)
	}
	if len(envs) < 2 {
		t.Fatal("saved response lacks multiple environment values")
	}
	for i := len(envs) - 1; i >= 0; i-- {
		reordered = protowire.AppendTag(reordered, 1, protowire.BytesType)
		reordered = protowire.AppendBytes(reordered, envs[i])
	}
	got, err := responseSemanticDigest(reordered)
	if err != nil || got != digest {
		t.Fatalf("saved response map reorder changed semantic digest: %v", err)
	}
}
