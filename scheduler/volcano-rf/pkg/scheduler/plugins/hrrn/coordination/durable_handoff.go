package coordination

import (
	"context"
	"encoding/hex"
	"fmt"
	"reflect"
	"sort"
	"strconv"
	"strings"
)

// DurableHandoff records one container's response identity in the same ledger
// as its Job reservation. It is not an authenticated kubelet observation,
// external NodeLock, device-plugin receipt, or fencing token.
type DurableHandoff struct {
	JobUID, PodUID, Node, Container, Resource, Attempt string
	Allocations                                        []Allocation
	DeviceIDs                                          []string
	Nonce, ResponseSemanticSHA256                      string
	Confirmed                                          bool
}

func validDigest(value string, bytes int) bool {
	b, err := hex.DecodeString(value)
	return err == nil && len(b) == bytes && hex.EncodeToString(b) == value
}

func handoffAttempt(value string) ([16]byte, uint64, error) {
	var empty [16]byte
	parts := strings.Split(value, ":")
	if len(parts) != 2 {
		return empty, 0, reject("invalid handoff attempt")
	}
	epoch, err := decodeDurableEpoch(parts[0])
	if err != nil {
		return empty, 0, err
	}
	seq, err := strconv.ParseUint(parts[1], 10, 64)
	if err != nil || seq == 0 || strconv.FormatUint(seq, 10) != parts[1] {
		return empty, 0, reject("invalid handoff sequence")
	}
	return epoch, seq, nil
}

func canonicalHandoff(h DurableHandoff) (DurableHandoff, error) {
	if h.JobUID == "" || h.PodUID == "" || h.Node == "" || h.Container == "" || h.Resource == "" ||
		!validDigest(h.Nonce, 32) || !validDigest(h.ResponseSemanticSHA256, 32) {
		return DurableHandoff{}, reject("incomplete handoff identity")
	}
	if _, _, err := handoffAttempt(h.Attempt); err != nil {
		return DurableHandoff{}, err
	}
	if len(h.Allocations) == 0 || len(h.DeviceIDs) != len(h.Allocations) {
		return DurableHandoff{}, reject("handoff device count mismatch")
	}
	if _, err := allocationMap(PodPlan{UID: h.PodUID, Node: h.Node, Allocations: h.Allocations}); err != nil {
		return DurableHandoff{}, err
	}
	for _, a := range h.Allocations {
		if a.Container != h.Container || a.Resources.Instances != 1 {
			return DurableHandoff{}, reject("handoff container or instance mismatch")
		}
	}
	h.Allocations = append([]Allocation(nil), h.Allocations...)
	h.DeviceIDs = append([]string(nil), h.DeviceIDs...)
	sort.Strings(h.DeviceIDs)
	for i, id := range h.DeviceIDs {
		if id == "" || (i > 0 && h.DeviceIDs[i-1] == id) {
			return DurableHandoff{}, reject("duplicate handoff device ID")
		}
	}
	return h, nil
}

func handoffPlanMatches(h DurableHandoff, p *podState) bool {
	if p == nil || p.plan.Node != h.Node {
		return false
	}
	want, err := allocationMap(p.plan)
	if err != nil {
		return false
	}
	got, err := allocationMap(PodPlan{UID: h.PodUID, Node: h.Node, Allocations: h.Allocations})
	if err != nil {
		return false
	}
	count := 0
	for k, v := range want {
		if k.container == h.Container {
			count++
			if got[k] != v {
				return false
			}
		}
	}
	return count > 0 && count == len(got)
}

// validateDurableHandoffs also runs after every ledger update and on reopen.
// It preserves historical records after release and across a quarantined
// takeover; old attempts are never thereby authorized for new work.
func validateDurableHandoffs(ledger durableLedger, c *Coordinator) error {
	seen := map[string]bool{}
	nonces := map[string]bool{}
	activeIDs := map[string]bool{}
	byPod := map[string]map[string]bool{}
	for _, original := range ledger.Handoffs {
		h, err := canonicalHandoff(original)
		if err != nil || !reflect.DeepEqual(h, original) {
			return reject("invalid or noncanonical persisted handoff")
		}
		p, err := c.pod(h.JobUID, h.PodUID, h.Node)
		if err != nil || !handoffPlanMatches(h, p) {
			return reject("persisted handoff differs from reserved plan")
		}
		epoch, seq, _ := handoffAttempt(h.Attempt)
		if seq != p.ticket.sequence || p.ticket.sequence == 0 {
			return reject("persisted handoff ticket sequence mismatch")
		}
		if epoch != c.epoch && p.stage != Uncertain && p.stage != Cancelled && p.stage != Finished {
			return reject("old handoff on active Pod")
		}
		if p.stage == Reserved || p.stage == Publishing || p.stage == Published {
			return reject("handoff before Pod is bound")
		}
		key := h.JobUID + "\x00" + h.PodUID + "\x00" + h.Container + "\x00" + h.Attempt
		if seen[key] || nonces[h.Nonce] {
			return reject("duplicate handoff identity or nonce")
		}
		seen[key], nonces[h.Nonce] = true, true
		podKey := h.JobUID + "\x00" + h.PodUID
		if byPod[podKey] == nil {
			byPod[podKey] = map[string]bool{}
		}
		byPod[podKey][h.Container] = h.Confirmed
		if p.stage != Finished && p.stage != Cancelled {
			for _, id := range h.DeviceIDs {
				key := h.Node + "\x00" + h.Resource + "\x00" + id
				if activeIDs[key] {
					return reject("overlapping active handoff device IDs")
				}
				activeIDs[key] = true
			}
		}
	}
	// Once this ledger has entered the handoff protocol for a Pod, legacy
	// RecordConsumed must not bypass incomplete or missing container records.
	for jobUID, job := range c.jobs {
		for podUID, pod := range job.pods {
			if pod.stage != Consumed && pod.stage != Finished {
				continue
			}
			records, used := byPod[jobUID+"\x00"+podUID]
			if !used {
				continue // v1 ledger / unrelated legacy tests have no records.
			}
			for _, allocation := range pod.plan.Allocations {
				if !records[allocation.Container] {
					return reject("consumed Pod has an unconfirmed container handoff")
				}
			}
		}
	}
	return nil
}

// PrepareHandoff checks the current Bound Pod, its slot, attempt and exact
// container plan, then records the nonce in the SAME UID/resourceVersion CAS
// as the resource ledger. An error with unknown outcome must be reconciled;
// do not return a response to kubelet or retry blindly. This only fences
// cooperating ledger writers, not old kubelet RPCs or other schedulers.
func (d *DurableCoordinator) PrepareHandoff(ctx context.Context, input DurableHandoff) error {
	h, err := canonicalHandoff(input)
	if err != nil {
		return err
	}
	if h.Confirmed {
		return reject("new handoff cannot be confirmed")
	}
	cm, ledger, c, err := d.read(ctx)
	if err != nil {
		return err
	}
	if ledger.Quarantined {
		return ErrDurableQuarantined
	}
	p, err := c.pod(h.JobUID, h.PodUID, h.Node)
	if err != nil || p.stage != Bound || c.slots[h.Node] != p.ticket || DurableAttemptID(p.ticket) != h.Attempt || !handoffPlanMatches(h, p) {
		return reject("handoff does not match current bound reservation")
	}
	ledger.Handoffs = append(ledger.Handoffs, h)
	return d.write(ctx, cm, ledger, c)
}

// LookupHandoff returns only a current, bound, unquarantined attempt. It is a
// point-in-time read, never a guarantee that external state cannot change.
func (d *DurableCoordinator) LookupHandoff(ctx context.Context, node, resource string, ids []string) (DurableHandoff, error) {
	cm, ledger, c, err := d.read(ctx)
	_ = cm
	if err != nil {
		return DurableHandoff{}, err
	}
	if ledger.Quarantined {
		return DurableHandoff{}, ErrDurableQuarantined
	}
	ids = append([]string(nil), ids...)
	sort.Strings(ids)
	var found *DurableHandoff
	for _, h := range ledger.Handoffs {
		if h.Node != node || h.Resource != resource || !reflect.DeepEqual(h.DeviceIDs, ids) {
			continue
		}
		p, err := c.pod(h.JobUID, h.PodUID, h.Node)
		if err != nil || p.stage != Bound || c.slots[h.Node] != p.ticket || DurableAttemptID(p.ticket) != h.Attempt {
			continue
		}
		if found != nil {
			return DurableHandoff{}, reject("ambiguous active handoff")
		}
		copyH := h
		copyH.Allocations = append([]Allocation(nil), h.Allocations...)
		copyH.DeviceIDs = append([]string(nil), h.DeviceIDs...)
		found = &copyH
	}
	if found == nil {
		return DurableHandoff{}, reject("active handoff not found")
	}
	return *found, nil
}

// ConfirmHandoff stores a caller-verified observation, but does not itself
// verify a node, plugin, checkpoint, binding, or current device state.
func (d *DurableCoordinator) ConfirmHandoff(ctx context.Context, input DurableHandoff) error {
	h, err := canonicalHandoff(input)
	if err != nil {
		return err
	}
	cm, ledger, c, err := d.read(ctx)
	if err != nil {
		return err
	}
	if ledger.Quarantined {
		return ErrDurableQuarantined
	}
	p, err := c.pod(h.JobUID, h.PodUID, h.Node)
	if err != nil || p.stage != Bound || c.slots[h.Node] != p.ticket || DurableAttemptID(p.ticket) != h.Attempt {
		return reject("handoff no longer bound")
	}
	for i, stored := range ledger.Handoffs {
		stored.Confirmed, h.Confirmed = false, false
		if reflect.DeepEqual(stored, h) {
			if ledger.Handoffs[i].Confirmed {
				return nil
			}
			ledger.Handoffs[i].Confirmed = true
			return d.write(ctx, cm, ledger, c)
		}
	}
	return fmt.Errorf("%w: handoff identity not found", ErrRejected)
}

// RecordConsumedWithHandoffs advances only after every GPU container has a
// confirmed record in the same ledger revision. The caller must still obtain
// an independently verified binding, live plugin/kubelet observation and
// external fencing; these booleans are not authenticated by this method.
func (d *DurableCoordinator) RecordConsumedWithHandoffs(ctx context.Context, t Ticket, o ConsumptionObservation) error {
	cm, ledger, c, err := d.read(ctx)
	if err != nil {
		return err
	}
	if ledger.Quarantined {
		return ErrDurableQuarantined
	}
	p, err := c.ticket(t)
	if err != nil {
		return err
	}
	want := map[string]bool{}
	for _, a := range p.plan.Allocations {
		want[a.Container] = true
	}
	for _, h := range ledger.Handoffs {
		if h.JobUID == t.JobUID && h.PodUID == t.PodUID && h.Attempt == DurableAttemptID(t) && h.Confirmed {
			delete(want, h.Container)
		}
	}
	if len(want) != 0 {
		return reject("not every container handoff is confirmed")
	}
	if err = c.RecordConsumed(t, o); err != nil {
		return err
	}
	return d.write(ctx, cm, ledger, c)
}
