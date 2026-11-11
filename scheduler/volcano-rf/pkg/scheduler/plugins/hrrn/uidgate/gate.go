// Package uidgate is a local protocol prototype, not a deployed Device Plugin.
// AllocateRequest and PreStartContainerRequest contain device IDs, not Pod UID.
// A trusted kubelet checkpoint written after Allocate may bind those IDs and
// this response's fresh nonce to a UID before container startup. That inference
// is conditional on the kubelet sequence and on exclusive, fenced ownership of
// the device IDs, node, journal and checkpoint. None is established here.
package uidgate

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"sort"
	"time"

	"google.golang.org/protobuf/encoding/protowire"
	devicecheckpoint "k8s.io/kubernetes/pkg/kubelet/cm/devicemanager/checkpoint"
	"volcano.sh/volcano/pkg/scheduler/plugins/hrrn/coordination"
)

const nonceEnv = "GPU_LAB_HANDOFF_NONCE"

// Claim comes from an independently authenticated, durable scheduler
// reservation. All containers are separate claims; the gate never guesses a
// Pod from timestamps or labels. Job membership and all resources must have
// been validated and exclusively reserved before this package is invoked.
type Claim struct {
	JobUID, PodUID, Node, Container, Resource, Attempt string
	Allocations                                        []coordination.Allocation
}

// Prepared records response identity before Allocate may return success. The
// journal MUST compare-and-swap against the active claim, prevent overlapping
// IDs/attempts, and conservatively block on uncertain writes. A process-local
// map is insufficient. This package provides no production journal or fencing.
type Prepared struct {
	Claim                         Claim
	DeviceIDs                     []string
	Nonce, ResponseSemanticSHA256 string
}

type Journal interface {
	Prepare(context.Context, Prepared) error
	Lookup(context.Context, string, []string) (Prepared, error)
	Confirm(context.Context, Prepared) error
}

// NodeObserver must be backed by a live authenticated node-side source with a
// trusted checkpoint, fresh authoritative Pod binding, and fencing ensuring
// no stale kubelet/PreStart RPC or competing allocator can reuse these IDs.
// A caller that reads an old disk file, sets booleans or signs its own claims
// does not meet that contract. The interface is deliberately unimplemented.
type NodeObserver interface {
	ReadAndVerify(context.Context, Claim) (rawCheckpoint []byte, observedAt time.Time, err error)
}

type Gate struct {
	Journal  Journal
	Observer NodeObserver
	Now      func() time.Time
	MaxAge   time.Duration
}

func fields(raw []byte, field protowire.Number) ([][]byte, error) {
	var out [][]byte
	for len(raw) > 0 {
		number, typ, n := protowire.ConsumeTag(raw)
		if n < 0 {
			return nil, protowire.ParseError(n)
		}
		raw = raw[n:]
		if number == field {
			if typ != protowire.BytesType {
				return nil, fmt.Errorf("unexpected wire type")
			}
			value, n := protowire.ConsumeBytes(raw)
			if n < 0 {
				return nil, protowire.ParseError(n)
			}
			out = append(out, value)
			raw = raw[n:]
		} else {
			n := protowire.ConsumeFieldValue(number, typ, raw)
			if n < 0 {
				return nil, protowire.ParseError(n)
			}
			raw = raw[n:]
		}
	}
	return out, nil
}

func env(raw []byte, key string) (string, bool, error) {
	entries, err := fields(raw, 1)
	if err != nil {
		return "", false, err
	}
	found := false
	value := ""
	for _, entry := range entries {
		k, v, err := mapEntry(entry)
		if err != nil {
			return "", false, err
		}
		if k == key {
			if found {
				return "", false, fmt.Errorf("duplicate handoff nonce")
			}
			found, value = true, v
		}
	}
	return value, found, nil
}

func idsCanonical(ids []string) ([]string, error) {
	if len(ids) == 0 {
		return nil, fmt.Errorf("empty device IDs")
	}
	out := append([]string(nil), ids...)
	sort.Strings(out)
	for i, id := range out {
		if id == "" || (i > 0 && id == out[i-1]) {
			return nil, fmt.Errorf("empty or duplicate device ID")
		}
	}
	return out, nil
}

func sameIDs(a, b []string) bool {
	x, err := idsCanonical(a)
	if err != nil {
		return false
	}
	y, err := idsCanonical(b)
	if err != nil || len(x) != len(y) {
		return false
	}
	for i := range x {
		if x[i] != y[i] {
			return false
		}
	}
	return true
}

func validClaim(c Claim) bool {
	if c.JobUID == "" || c.PodUID == "" || c.Node == "" || c.Container == "" || c.Resource == "" || c.Attempt == "" || len(c.Allocations) == 0 {
		return false
	}
	for _, a := range c.Allocations {
		if a.Container != c.Container || a.Device.Node != c.Node {
			return false
		}
	}
	return true
}

// PrepareAllocate takes one container's actual plugin response protobuf bytes
// (not the outer AllocateResponse), adds a random nonce, and persists the exact
// returned response identity BEFORE returning it to kubelet. The base response
// builder must already be constrained to the selected durable claim; this
// helper does not call the legacy HAMi GetPendingPod or alter Kubernetes.
// The caller must call Prepare exactly once for a fenced attempt; retries,
// ambiguous journal writes and plugin restarts fail closed/reconcile externally.
func (g *Gate) PrepareAllocate(ctx context.Context, claim Claim, ids []string, base []byte) ([]byte, error) {
	if ctx == nil || g == nil || g.Journal == nil || !validClaim(claim) || len(base) == 0 {
		return nil, fmt.Errorf("invalid prepare")
	}
	ids, err := idsCanonical(ids)
	if err != nil {
		return nil, err
	}
	if len(ids) != len(claim.Allocations) {
		return nil, fmt.Errorf("device count differs from claim")
	}
	_, exists, err := env(base, nonceEnv)
	if err != nil || exists {
		return nil, fmt.Errorf("invalid base response nonce")
	}
	var random [32]byte
	if _, err := rand.Read(random[:]); err != nil {
		return nil, err
	}
	nonce := hex.EncodeToString(random[:])
	var entry []byte
	entry = protowire.AppendTag(entry, 1, protowire.BytesType)
	entry = protowire.AppendString(entry, nonceEnv)
	entry = protowire.AppendTag(entry, 2, protowire.BytesType)
	entry = protowire.AppendString(entry, nonce)
	response := append([]byte(nil), base...)
	response = protowire.AppendTag(response, 1, protowire.BytesType)
	response = protowire.AppendBytes(response, entry)
	hash, err := responseSemanticDigest(response)
	if err != nil {
		return nil, fmt.Errorf("unsupported response: %w", err)
	}
	// Never let a caller mutate the reserved plan after the journal accepts it.
	// idsCanonical already copied the device-ID slice above.
	claim.Allocations = append([]coordination.Allocation(nil), claim.Allocations...)
	p := Prepared{Claim: claim, DeviceIDs: ids, Nonce: nonce, ResponseSemanticSHA256: hash}
	if err = g.Journal.Prepare(ctx, p); err != nil {
		return nil, fmt.Errorf("persist preparation: %w", err)
	}
	return response, nil
}

// CheckPreStart is called with the exact IDs from the kubelet PreStart request.
// It returns no acknowledgement until fresh authoritative Pod/checkpoint
// observations match the persisted attempt, exact response, UID and quota.
// Duplicate/restarted calls are safe only if the Journal's Confirm is
// idempotent and Observer excludes old RPCs and stale/reused device IDs.
func (g *Gate) CheckPreStart(ctx context.Context, node string, ids []string) (Prepared, error) {
	var zero Prepared
	if ctx == nil || g == nil || g.Journal == nil || g.Observer == nil || g.Now == nil || g.MaxAge <= 0 || g.MaxAge > time.Minute || node == "" {
		return zero, fmt.Errorf("invalid prestart configuration")
	}
	ids, err := idsCanonical(ids)
	if err != nil {
		return zero, err
	}
	p, err := g.Journal.Lookup(ctx, node, ids)
	if err != nil {
		return zero, err
	}
	if !validClaim(p.Claim) || p.Claim.Node != node || !sameIDs(p.DeviceIDs, ids) || len(p.Nonce) != 64 || len(p.ResponseSemanticSHA256) != 64 {
		return zero, fmt.Errorf("journal identity mismatch")
	}
	raw, at, err := g.Observer.ReadAndVerify(ctx, p.Claim)
	if err != nil {
		return zero, err
	}
	now := g.Now()
	if at.IsZero() || at.After(now) || now.Sub(at) > g.MaxAge || len(raw) == 0 || len(raw) > 32<<20 {
		return zero, fmt.Errorf("stale or invalid checkpoint observation")
	}
	cp := devicecheckpoint.New(nil, nil)
	if err = cp.UnmarshalCheckpoint(raw); err != nil {
		return zero, err
	}
	if err = cp.VerifyChecksum(); err != nil {
		return zero, err
	}
	entries, _ := cp.GetData()
	matched := 0
	for _, entry := range entries {
		if entry.ResourceName != p.Claim.Resource {
			continue
		}
		var entryIDs []string
		for _, group := range entry.DeviceIDs {
			entryIDs = append(entryIDs, group...)
		}
		overlaps := false
		for _, x := range entryIDs {
			for _, y := range ids {
				if x == y {
					overlaps = true
				}
			}
		}
		if !overlaps {
			continue
		}
		if entry.PodUID != p.Claim.PodUID || entry.ContainerName != p.Claim.Container || !sameIDs(entryIDs, ids) {
			return zero, fmt.Errorf("device IDs overlap another UID/container")
		}
		matched++
		v, ok, e := env(entry.AllocResp, nonceEnv)
		sum, digestErr := responseSemanticDigest(entry.AllocResp)
		if e != nil || digestErr != nil || !ok || v != p.Nonce || sum != p.ResponseSemanticSHA256 {
			return zero, fmt.Errorf("response nonce or digest mismatch")
		}
	}
	if matched != 1 {
		return zero, fmt.Errorf("checkpoint does not identify exactly one current UID/container")
	}
	receipt, err := coordination.InspectKubeletCheckpoint(raw, p.Claim.PodUID, p.Claim.Container, p.Claim.Resource)
	if err != nil {
		return zero, err
	}
	if !sameIDs(receipt.RegisteredSlotIDs, ids) {
		return zero, fmt.Errorf("checkpoint device IDs mismatch")
	}
	if err = receipt.Matches(p.Claim.Allocations); err != nil {
		return zero, err
	}
	if err = g.Journal.Confirm(ctx, p); err != nil {
		return zero, err
	}
	return p, nil
}
