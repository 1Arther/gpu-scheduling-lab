// Copyright 2026 The Volcano Authors. Apache-2.0.
package coordination

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"reflect"
	"sort"
	"strconv"

	v1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes"
)

// This is a local-testable persistence primitive, NOT a production scheduler
// integration. One ConfigMap provides an atomic ledger update for cooperating
// writers only. It cannot fence another scheduler, kubelet, a Device Plugin or
// an already-running process. External publication requires separate fencing,
// authoritative device observations and durable recovery integration.
const durableKey = "ledger.json"
const durableVersion = 1
const durableMaxBytes = 768 * 1024

var (
	ErrDurableConflict    = errors.New("durable coordination: resource version conflict")
	ErrDurableUnknown     = errors.New("durable coordination: write outcome unknown; reconcile before external effects")
	ErrDurableQuarantined = errors.New("durable coordination: recovery quarantine")
)

type durableDevice struct {
	Node     string    `json:"node"`
	GPU      string    `json:"gpu"`
	Capacity Resources `json:"capacity"`
	Baseline Resources `json:"baseline"`
}
type durablePod struct {
	Plan      PodPlan `json:"plan"`
	Stage     Stage   `json:"stage"`
	Sequence  uint64  `json:"sequence"`
	HoldsSlot bool    `json:"holdsSlot"`
}
type durableJob struct {
	UID  string       `json:"uid"`
	Pods []durablePod `json:"pods"`
}
type durableLedger struct {
	Version     int              `json:"version"`
	Epoch       string           `json:"epoch"`
	Revision    uint64           `json:"revision"`
	Sequence    uint64           `json:"sequence"`
	Quarantined bool             `json:"quarantined"`
	Devices     []durableDevice  `json:"devices"`
	Jobs        []durableJob     `json:"jobs"`
	Handoffs    []DurableHandoff `json:"handoffs,omitempty"`
}

// DurableCoordinator is a handle bound to a specific ConfigMap UID and epoch.
// Epoch is a cooperative ledger-write token; it is NOT external fencing.
type DurableCoordinator struct {
	client          kubernetes.Interface
	namespace, name string
	uid             types.UID
	epoch           string
}

func newDurableEpoch() (string, error) {
	var epoch [16]byte
	if _, err := rand.Read(epoch[:]); err != nil {
		return "", err
	}
	return hex.EncodeToString(epoch[:]), nil
}

func decodeDurableEpoch(epoch string) ([16]byte, error) {
	var value [16]byte
	if len(epoch) != 32 {
		return value, reject("invalid durable epoch")
	}
	bytes, err := hex.DecodeString(epoch)
	if err != nil || hex.EncodeToString(bytes) != epoch {
		return value, reject("invalid durable epoch")
	}
	copy(value[:], bytes)
	return value, nil
}

// DurableAttemptID identifies a Pod handoff attempt within this ledger. It
// does not force external components to reject stale operations.
func DurableAttemptID(ticket Ticket) string {
	if ticket.sequence == 0 {
		return ""
	}
	return hex.EncodeToString(ticket.epoch[:]) + ":" + strconv.FormatUint(ticket.sequence, 10)
}

func (d *DurableCoordinator) UID() types.UID { return d.uid }
func (d *DurableCoordinator) Epoch() string  { return d.epoch }

func usableDurableClient(client kubernetes.Interface) bool {
	if client == nil {
		return false
	}
	value := reflect.ValueOf(client)
	switch value.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map, reflect.Ptr, reflect.Slice:
		return !value.IsNil()
	default:
		return true
	}
}

func durableEncode(ledger durableLedger) (string, error) {
	data, err := json.Marshal(ledger)
	if err != nil {
		return "", err
	}
	if len(data) > durableMaxBytes {
		return "", reject("durable ledger exceeds bounded size")
	}
	return string(data), nil
}

func durableDecode(cm *v1.ConfigMap) (durableLedger, error) {
	var ledger durableLedger
	if cm == nil || cm.UID == "" || cm.ResourceVersion == "" || cm.DeletionTimestamp != nil ||
		(cm.Immutable != nil && *cm.Immutable) || len(cm.Data) != 1 || len(cm.BinaryData) != 0 {
		return ledger, reject("invalid ConfigMap identity, version or data")
	}
	value, exists := cm.Data[durableKey]
	if !exists || value == "" || len(value) > durableMaxBytes {
		return ledger, reject("missing or oversized durable ledger")
	}
	decoder := json.NewDecoder(bytes.NewBufferString(value))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&ledger); err != nil {
		return ledger, fmt.Errorf("decode durable ledger: %w", err)
	}
	var trailing interface{}
	if err := decoder.Decode(&trailing); err != io.EOF {
		return ledger, reject("trailing durable ledger content")
	}
	if ledger.Version != durableVersion || ledger.Revision == 0 {
		return ledger, reject("unsupported ledger version or revision")
	}
	if _, err := decodeDurableEpoch(ledger.Epoch); err != nil {
		return ledger, err
	}
	// Our writer emits canonical struct JSON. Requiring an exact round trip
	// rejects omitted/null fields, duplicate or case-variant keys and edits
	// which a permissive JSON decoder would otherwise silently normalize.
	canonical, err := durableEncode(ledger)
	if err != nil || canonical != value {
		return ledger, reject("noncanonical or incomplete durable ledger")
	}
	return ledger, nil
}

// restoreDurable reconstructs rather than trusting a serialized Used total.
// Every live or uncertain reservation is charged against immutable baselines.
func restoreDurable(ledger durableLedger) (*Coordinator, error) {
	epoch, err := decodeDurableEpoch(ledger.Epoch)
	if err != nil {
		return nil, err
	}
	limits, base := map[DeviceKey]Resources{}, map[DeviceKey]Resources{}
	physicalGPUs := map[string]bool{}
	for _, entry := range ledger.Devices {
		key := DeviceKey{Node: entry.Node, GPU: entry.GPU}
		if _, exists := limits[key]; exists || physicalGPUs[entry.GPU] {
			return nil, reject("duplicate durable physical GPU identity")
		}
		physicalGPUs[entry.GPU] = true
		limits[key], base[key] = entry.Capacity, entry.Baseline
	}
	c, err := New(limits, base)
	if err != nil {
		return nil, err
	}
	c.epoch, c.seq = epoch, ledger.Sequence
	seenSeq := map[uint64]bool{}
	for _, job := range ledger.Jobs {
		if job.UID == "" || len(job.Pods) == 0 || c.jobs[job.UID] != nil {
			return nil, reject("invalid or duplicate durable Job")
		}
		state := &jobState{pods: map[string]*podState{}}
		for _, item := range job.Pods {
			pod := item.Plan
			if pod.UID == "" || c.podUIDs[pod.UID] != "" {
				return nil, reject("duplicate durable Pod UID")
			}
			if _, err := allocationMap(pod); err != nil {
				return nil, err
			}
			pod.Allocations = append([]Allocation(nil), pod.Allocations...)
			if item.Sequence > ledger.Sequence || (item.Sequence > 0 && seenSeq[item.Sequence]) {
				return nil, reject("invalid durable ticket sequence")
			}
			if item.Sequence > 0 {
				seenSeq[item.Sequence] = true
			}
			ticket := Ticket{}
			if item.Sequence > 0 {
				ticket = Ticket{JobUID: job.UID, PodUID: pod.UID, Node: pod.Node, epoch: epoch, sequence: item.Sequence}
			}
			switch item.Stage {
			case Reserved:
				if item.Sequence != 0 || item.HoldsSlot {
					return nil, reject("invalid reserved Pod state")
				}
			case Publishing, Published, Bound:
				if item.Sequence == 0 || !item.HoldsSlot {
					return nil, reject("in-flight Pod lost slot")
				}
			case Consumed, Finished:
				if item.Sequence == 0 || item.HoldsSlot {
					return nil, reject("invalid consumed or finished Pod")
				}
			case Uncertain:
				if item.Sequence == 0 {
					return nil, reject("uncertain Pod has no attempt")
				}
				if c.blocked[pod.Node] == nil {
					c.blocked[pod.Node] = map[string]bool{}
				}
				c.blocked[pod.Node][pod.UID] = true
			case Cancelled:
				if item.HoldsSlot {
					return nil, reject("cancelled Pod holds slot")
				}
			default:
				return nil, reject("unknown durable Pod stage")
			}
			if item.HoldsSlot {
				if _, exists := c.slots[pod.Node]; exists {
					return nil, reject("multiple durable slots on one node")
				}
				c.slots[pod.Node] = ticket
			}
			for _, allocation := range pod.Allocations {
				limit, exists := c.limits[allocation.Device]
				if !exists {
					return nil, reject("durable allocation refers to unknown device")
				}
				if item.Stage == Cancelled || item.Stage == Finished {
					continue
				}
				total, ok := add(c.used[allocation.Device], allocation.Resources)
				if !ok || !fits(total, limit) {
					return nil, reject("durable ledger exceeds capacity")
				}
				c.used[allocation.Device] = total
			}
			state.pods[pod.UID] = &podState{plan: pod, stage: item.Stage, ticket: ticket}
			c.podUIDs[pod.UID] = job.UID
		}
		c.jobs[job.UID] = state
	}
	if err := validateDurableHandoffs(ledger, c); err != nil {
		return nil, err
	}
	return c, nil
}

func storeDurable(ledger *durableLedger, c *Coordinator) error {
	if ledger.Revision == ^uint64(0) {
		return reject("durable revision exhausted")
	}
	ledger.Revision++
	ledger.Epoch, ledger.Sequence = hex.EncodeToString(c.epoch[:]), c.seq
	ledger.Jobs = nil
	for jobUID, job := range c.jobs {
		entry := durableJob{UID: jobUID}
		for _, pod := range job.pods {
			entry.Pods = append(entry.Pods, durablePod{Plan: pod.plan, Stage: pod.stage,
				Sequence: pod.ticket.sequence, HoldsSlot: c.slots[pod.plan.Node] == pod.ticket && pod.ticket.sequence != 0})
		}
		sort.Slice(entry.Pods, func(i, j int) bool { return entry.Pods[i].Plan.UID < entry.Pods[j].Plan.UID })
		ledger.Jobs = append(ledger.Jobs, entry)
	}
	sort.Slice(ledger.Jobs, func(i, j int) bool { return ledger.Jobs[i].UID < ledger.Jobs[j].UID })
	_, err := restoreDurable(*ledger)
	return err
}

// CreateDurable creates a new ledger only; AlreadyExists is not adopted. The
// baseline must be a complete externally reconciled snapshot and must exclude
// any future reservations tracked by this object.
func CreateDurable(ctx context.Context, client kubernetes.Interface, namespace, name string, capacities, baseline map[DeviceKey]Resources) (*DurableCoordinator, error) {
	if ctx == nil || !usableDurableClient(client) || namespace == "" || name == "" {
		return nil, reject("missing durable creation identity")
	}
	if _, err := New(capacities, baseline); err != nil {
		return nil, err
	}
	epoch, err := newDurableEpoch()
	if err != nil {
		return nil, err
	}
	ledger := durableLedger{Version: durableVersion, Epoch: epoch, Revision: 1}
	for key, capacity := range capacities {
		ledger.Devices = append(ledger.Devices, durableDevice{Node: key.Node, GPU: key.GPU, Capacity: capacity, Baseline: baseline[key]})
	}
	sort.Slice(ledger.Devices, func(i, j int) bool {
		if ledger.Devices[i].Node == ledger.Devices[j].Node {
			return ledger.Devices[i].GPU < ledger.Devices[j].GPU
		}
		return ledger.Devices[i].Node < ledger.Devices[j].Node
	})
	if _, err := restoreDurable(ledger); err != nil {
		return nil, err // reject invalid snapshots before the first API write
	}
	encoded, err := durableEncode(ledger)
	if err != nil {
		return nil, err
	}
	cm, err := client.CoreV1().ConfigMaps(namespace).Create(ctx, &v1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: namespace}, Data: map[string]string{durableKey: encoded}}, metav1.CreateOptions{})
	if apierrors.IsAlreadyExists(err) {
		return nil, fmt.Errorf("%w: %v", ErrDurableConflict, err)
	}
	if err != nil {
		return nil, fmt.Errorf("%w: create: %v", ErrDurableUnknown, err)
	}
	decoded, err := durableDecode(cm)
	if err != nil || cm.Namespace != namespace || cm.Name != name || cm.Data[durableKey] != encoded || decoded.Epoch != epoch {
		return nil, fmt.Errorf("%w: invalid creation response: %v", ErrDurableUnknown, err)
	}
	if _, err := restoreDurable(decoded); err != nil {
		return nil, fmt.Errorf("%w: %v", ErrDurableUnknown, err)
	}
	return &DurableCoordinator{client: client, namespace: namespace, name: name, uid: cm.UID, epoch: epoch}, nil
}

// OpenDurable opens a same-epoch cooperative handle and refuses unexpected
// UID/epoch, corrupted data and deletion. It does NOT prove another holder of
// that epoch has stopped. Do not treat it as restart recovery: recovery must
// use TakeoverDurable and independently establish real external fencing. All
// resource users must cooperate with this ledger for its capacities to matter.
func OpenDurable(ctx context.Context, client kubernetes.Interface, namespace, name string, uid types.UID, epoch string) (*DurableCoordinator, error) {
	if ctx == nil || !usableDurableClient(client) || namespace == "" || name == "" || uid == "" {
		return nil, reject("missing durable identity")
	}
	d := &DurableCoordinator{client: client, namespace: namespace, name: name, uid: uid, epoch: epoch}
	_, _, _, err := d.read(ctx)
	if err != nil {
		return nil, err
	}
	return d, nil
}

func (d *DurableCoordinator) read(ctx context.Context) (*v1.ConfigMap, durableLedger, *Coordinator, error) {
	var zero durableLedger
	if ctx == nil || d == nil || !usableDurableClient(d.client) || d.uid == "" {
		return nil, zero, nil, reject("missing durable handle")
	}
	cm, err := d.client.CoreV1().ConfigMaps(d.namespace).Get(ctx, d.name, metav1.GetOptions{})
	if err != nil {
		return nil, zero, nil, err
	}
	if cm.UID != d.uid || cm.Namespace != d.namespace || cm.Name != d.name {
		return nil, zero, nil, reject("ConfigMap UID changed")
	}
	ledger, err := durableDecode(cm)
	if err != nil {
		return nil, zero, nil, err
	}
	if ledger.Epoch != d.epoch {
		return nil, zero, nil, reject("stale durable epoch")
	}
	c, err := restoreDurable(ledger)
	return cm, ledger, c, err
}

func (d *DurableCoordinator) write(ctx context.Context, cm *v1.ConfigMap, ledger durableLedger, c *Coordinator) error {
	if err := storeDurable(&ledger, c); err != nil {
		return err
	}
	encoded, err := durableEncode(ledger)
	if err != nil {
		return err
	}
	updated := cm.DeepCopy() // retains UID + resourceVersion as update preconditions.
	updated.Data = map[string]string{durableKey: encoded}
	response, err := d.client.CoreV1().ConfigMaps(d.namespace).Update(ctx, updated, metav1.UpdateOptions{})
	if apierrors.IsConflict(err) {
		return fmt.Errorf("%w: %v", ErrDurableConflict, err)
	}
	if err != nil {
		return fmt.Errorf("%w: update: %v", ErrDurableUnknown, err)
	}
	if response == nil || response.UID != d.uid || response.Namespace != d.namespace || response.Name != d.name ||
		response.ResourceVersion == "" || response.ResourceVersion == cm.ResourceVersion || response.Data[durableKey] != encoded {
		return fmt.Errorf("%w: incomplete or changed update response", ErrDurableUnknown)
	}
	if _, err := durableDecode(response); err != nil {
		return fmt.Errorf("%w: invalid update response: %v", ErrDurableUnknown, err)
	}
	return nil
}

func (d *DurableCoordinator) mutate(ctx context.Context, allowQuarantine bool, f func(*Coordinator) error) error {
	cm, ledger, c, err := d.read(ctx)
	if err != nil {
		return err
	}
	if ledger.Quarantined && !allowQuarantine {
		return ErrDurableQuarantined
	}
	if err := f(c); err != nil {
		return err
	}
	return d.write(ctx, cm, ledger, c)
}

func (d *DurableCoordinator) Reserve(ctx context.Context, plan JobPlan) error {
	return d.mutate(ctx, false, func(c *Coordinator) error { return c.Reserve(plan) })
}
func (d *DurableCoordinator) Begin(ctx context.Context, jobUID, podUID, node string) (Ticket, error) {
	var ticket Ticket
	err := d.mutate(ctx, false, func(c *Coordinator) error { var e error; ticket, e = c.Begin(jobUID, podUID, node); return e })
	if err != nil {
		return Ticket{}, err
	}
	return ticket, nil
}
func (d *DurableCoordinator) Published(ctx context.Context, ticket Ticket) error {
	return d.mutate(ctx, false, func(c *Coordinator) error { return c.Published(ticket) })
}
func (d *DurableCoordinator) RecordBound(ctx context.Context, ticket Ticket, obs BindingObservation) error {
	return d.mutate(ctx, false, func(c *Coordinator) error { return c.RecordBound(ticket, obs) })
}
func (d *DurableCoordinator) RecordConsumed(ctx context.Context, ticket Ticket, obs ConsumptionObservation) error {
	return d.mutate(ctx, false, func(c *Coordinator) error { return c.RecordConsumed(ticket, obs) })
}
func (d *DurableCoordinator) Cancel(ctx context.Context, jobUID, podUID, node string) (Stage, error) {
	var stage Stage
	err := d.mutate(ctx, true, func(c *Coordinator) error { var e error; stage, e = c.Cancel(jobUID, podUID, node); return e })
	if err != nil {
		return "", err
	}
	return stage, nil
}
func (d *DurableCoordinator) Fail(ctx context.Context, ticket Ticket) error {
	return d.mutate(ctx, true, func(c *Coordinator) error { return c.Fail(ticket) })
}
func (d *DurableCoordinator) Resolve(ctx context.Context, ticket Ticket, obs ReleaseObservation) error {
	return d.mutate(ctx, true, func(c *Coordinator) error { return c.Resolve(ticket, obs) })
}
func (d *DurableCoordinator) Snapshot(ctx context.Context) (State, error) {
	_, _, c, err := d.read(ctx)
	if err != nil {
		return State{}, err
	}
	return c.Snapshot(), nil
}

// RecoveryTicket returns the new epoch's ticket for an uncertain Pod after
// takeover; it does not assert a previous plugin result or authorize release.
func (d *DurableCoordinator) RecoveryTicket(ctx context.Context, jobUID, podUID, node string) (Ticket, error) {
	_, _, c, err := d.read(ctx)
	if err != nil {
		return Ticket{}, err
	}
	p, err := c.pod(jobUID, podUID, node)
	if err != nil || p.stage != Uncertain || p.ticket.sequence == 0 {
		return Ticket{}, reject("Pod is not an uncertain prior attempt")
	}
	return p.ticket, nil
}

// TakeoverDurable rotates the cooperative epoch via the old UID + resource
// version. Every potentially published Pod is made uncertain and remains
// charged; a global quarantine prevents new starts pending external fencing.
// This CAS does not itself stop the old process or fence outside side effects.
func TakeoverDurable(ctx context.Context, client kubernetes.Interface, namespace, name string, uid types.UID, oldEpoch string) (*DurableCoordinator, error) {
	old, err := OpenDurable(ctx, client, namespace, name, uid, oldEpoch)
	if err != nil {
		return nil, err
	}
	cm, ledger, c, err := old.read(ctx)
	if err != nil {
		return nil, err
	}
	newEpoch, err := newDurableEpoch()
	if err != nil {
		return nil, err
	}
	if newEpoch == oldEpoch {
		return nil, reject("takeover epoch did not change")
	}
	epoch, _ := decodeDurableEpoch(newEpoch)
	c.epoch = epoch
	for _, job := range c.jobs {
		for _, p := range job.pods {
			oldTicket := p.ticket
			if oldTicket.sequence > 0 {
				p.ticket.epoch = epoch
			}
			if c.slots[p.plan.Node] == oldTicket && oldTicket.sequence != 0 {
				c.slots[p.plan.Node] = p.ticket
			}
			switch p.stage {
			case Publishing, Published, Bound, Consumed:
				p.stage = Uncertain
				if c.blocked[p.plan.Node] == nil {
					c.blocked[p.plan.Node] = map[string]bool{}
				}
				c.blocked[p.plan.Node][p.plan.UID] = true
			}
		}
	}
	ledger.Quarantined = true
	if err := old.write(ctx, cm, ledger, c); err != nil {
		return nil, err
	}
	return &DurableCoordinator{client: client, namespace: namespace, name: name, uid: uid, epoch: newEpoch}, nil
}

// RecoveryEvidence is supplied by a trusted external reconciler. This package
// cannot authenticate or independently obtain those observations.
type RecoveryEvidence struct {
	ObservationID                 string
	PreviousOwnerFenced           bool
	AllActiveAllocationsAccounted bool
	NoUnknownInFlightWork         bool
}

// AcknowledgeRecovery only removes the global quarantine. Uncertain Pods keep
// their per-node blocks and resources until a separate verified Resolve.
func (d *DurableCoordinator) AcknowledgeRecovery(ctx context.Context, evidence RecoveryEvidence) error {
	if evidence.ObservationID == "" || !evidence.PreviousOwnerFenced || !evidence.AllActiveAllocationsAccounted || !evidence.NoUnknownInFlightWork {
		return reject("external fencing and reconciliation evidence missing")
	}
	cm, ledger, c, err := d.read(ctx)
	if err != nil {
		return err
	}
	if !ledger.Quarantined {
		return reject("ledger is not in recovery quarantine")
	}
	ledger.Quarantined = false
	return d.write(ctx, cm, ledger, c)
}

// BaselineEvidence describes a fresh observation supplied by an external
// verifier. Reservations tracked here must be excluded from the baseline to
// avoid charging the same Pod twice. Booleans do not authenticate evidence.
type BaselineEvidence struct {
	ObservationID                   string
	FullInventoryVerified           bool
	AllActivePodsAndClaimsAccounted bool
	ReservationsExcluded            bool
	NoUnknownInFlightWork           bool
}

// ReconcileBaseline updates only the external baseline via the same CAS.
// Each remaining reservation is recharged against the new baseline before
// writing, so a partial/overfull observation is rejected. No Kubernetes Pod,
// plugin or NodeLock is read or modified by this method.
func (d *DurableCoordinator) ReconcileBaseline(ctx context.Context, baseline map[DeviceKey]Resources, evidence BaselineEvidence) error {
	if evidence.ObservationID == "" || !evidence.FullInventoryVerified || !evidence.AllActivePodsAndClaimsAccounted ||
		!evidence.ReservationsExcluded || !evidence.NoUnknownInFlightWork {
		return reject("complete externally verified baseline evidence required")
	}
	cm, ledger, _, err := d.read(ctx)
	if err != nil {
		return err
	}
	if ledger.Quarantined {
		return ErrDurableQuarantined
	}
	if len(baseline) != len(ledger.Devices) {
		return reject("baseline must cover every GPU")
	}
	for i := range ledger.Devices {
		key := DeviceKey{Node: ledger.Devices[i].Node, GPU: ledger.Devices[i].GPU}
		value, found := baseline[key]
		if !found || !valid(value) {
			return reject("missing or invalid baseline device")
		}
		ledger.Devices[i].Baseline = value
	}
	c, err := restoreDurable(ledger)
	if err != nil {
		return err
	}
	return d.write(ctx, cm, ledger, c)
}
