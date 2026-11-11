package uidgate

import (
	"context"
	"fmt"

	"volcano.sh/volcano/pkg/scheduler/plugins/hrrn/coordination"
)

// LedgerJournal stores the handoff in the SAME ConfigMap CAS revision as the
// complete Job reservation. Prepare/Confirm require the current Bound Pod,
// node slot, ticket and exact container allocation. This adapter does not
// authenticate the source of the checkpoint, verify Pod binding or fence
// kubelet/other schedulers. It must not be used to enable live scheduling
// without separately implemented and validated evidence and fencing.
type LedgerJournal struct {
	Ledger   *coordination.DurableCoordinator
	Resource string
}

func (j *LedgerJournal) handoff(p Prepared) (coordination.DurableHandoff, error) {
	if j == nil || j.Ledger == nil || j.Resource == "" || p.Claim.Resource != j.Resource {
		return coordination.DurableHandoff{}, fmt.Errorf("missing or mismatched durable journal resource")
	}
	c := p.Claim
	return coordination.DurableHandoff{JobUID: c.JobUID, PodUID: c.PodUID, Node: c.Node,
		Container: c.Container, Resource: c.Resource, Attempt: c.Attempt,
		Allocations: append([]coordination.Allocation(nil), c.Allocations...),
		DeviceIDs:   append([]string(nil), p.DeviceIDs...), Nonce: p.Nonce,
		ResponseSemanticSHA256: p.ResponseSemanticSHA256}, nil
}

func (j *LedgerJournal) Prepare(ctx context.Context, p Prepared) error {
	h, err := j.handoff(p)
	if err != nil {
		return err
	}
	return j.Ledger.PrepareHandoff(ctx, h)
}

func (j *LedgerJournal) Lookup(ctx context.Context, node string, ids []string) (Prepared, error) {
	if j == nil || j.Ledger == nil || j.Resource == "" {
		return Prepared{}, fmt.Errorf("missing durable journal or resource")
	}
	h, err := j.Ledger.LookupHandoff(ctx, node, j.Resource, ids)
	if err != nil {
		return Prepared{}, err
	}
	return Prepared{Claim: Claim{JobUID: h.JobUID, PodUID: h.PodUID, Node: h.Node,
		Container: h.Container, Resource: h.Resource, Attempt: h.Attempt,
		Allocations: append([]coordination.Allocation(nil), h.Allocations...)},
		DeviceIDs: append([]string(nil), h.DeviceIDs...), Nonce: h.Nonce,
		ResponseSemanticSHA256: h.ResponseSemanticSHA256}, nil
}

func (j *LedgerJournal) Confirm(ctx context.Context, p Prepared) error {
	h, err := j.handoff(p)
	if err != nil {
		return err
	}
	return j.Ledger.ConfirmHandoff(ctx, h)
}

var _ Journal = (*LedgerJournal)(nil)
