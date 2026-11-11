// Package coordination contains an isolated model and experimental persistent
// ledger for studying complete Job reservations and device handshakes. They
// are NOT wired to Volcano's allocation path or to a real device plugin. In
// particular, the ledger is not a distributed device lock, cannot fence another
// scheduler, and must not be used for live scheduling.
// Callers of this model must supply independently verified observations. A
// device-plugin bind-phase annotation on its own is not consumption evidence.
package coordination

import (
	"crypto/rand"
	"errors"
	"fmt"
	"math"
	"sort"
	"sync"
)

// Resources are logical vGPU quantities, not physical utilisation.
type Resources struct {
	Memory, Cores, Instances int64
}

type DeviceKey struct{ Node, GPU string }

// Allocation identifies one container's allocation on one GPU. Kubernetes
// Pods are constrained to one node; a Pod can require several GPUs/containers.
type Allocation struct {
	Container string
	Device    DeviceKey
	Resources Resources
}

type PodPlan struct {
	UID, Node   string
	Allocations []Allocation
}

type JobPlan struct {
	UID  string
	Pods []PodPlan
}

type Stage string

const (
	Reserved   Stage = "reserved"
	Publishing Stage = "publishing"
	Published  Stage = "published"
	Bound      Stage = "bound"
	Consumed   Stage = "consumed"
	Uncertain  Stage = "uncertain"
	Finished   Stage = "finished"
	Cancelled  Stage = "cancelled"
)

// Ticket is an opaque attempt generation. A DurableCoordinator persists its
// epoch and sequence, but the token is not enforced by external actors and is
// not distributed fencing. Recovery requires external reconciliation and
// fencing before another coordinator may schedule.
type Ticket struct {
	JobUID, PodUID, Node string
	epoch                [16]byte
	sequence             uint64
}

// The following observations are assertions from a trusted external verifier,
// not data that this package obtains or authenticates. Production adapters
// must verify identity, timestamps, evidence freshness and device state.
type BindingObservation struct {
	PodUID, Node, ObservationID string
}

type ConsumptionObservation struct {
	PodUID, Node, ObservationID string
	Allocations                 []Allocation
	BoundUIDVerified            bool
	PluginResponseVerified      bool
	KubeletCheckpointVerified   bool
	NoInFlightAllocate          bool
	NodeLockUnheld              bool
}

type ReleaseObservation struct {
	PodUID, Node, ObservationID string
	PodTerminalOrAbsent         bool
	DeviceReleaseVerified       bool
	NoInFlightAllocate          bool
	NodeLockUnheld              bool
}

var ErrRejected = errors.New("coordination: rejected")

func reject(message string) error { return fmt.Errorf("%w: %s", ErrRejected, message) }

type podState struct {
	plan   PodPlan
	stage  Stage
	ticket Ticket
}

type jobState struct{ pods map[string]*podState }

// Coordinator owns an isolated resource snapshot and models one process.
// `allocated` represents quantities already in use before this model begins;
// they stay charged until a new, externally reconciled coordinator is built.
type Coordinator struct {
	mu      sync.Mutex
	epoch   [16]byte
	seq     uint64
	limits  map[DeviceKey]Resources
	used    map[DeviceKey]Resources
	jobs    map[string]*jobState
	podUIDs map[string]string          // tombstoned: a UID is never reused
	slots   map[string]Ticket          // one candidate per node
	blocked map[string]map[string]bool // unresolved external effects by Pod UID
}

func valid(r Resources) bool { return r.Memory >= 0 && r.Cores >= 0 && r.Instances >= 0 }

func add(a, b Resources) (Resources, bool) {
	if !valid(a) || !valid(b) || a.Memory > math.MaxInt64-b.Memory || a.Cores > math.MaxInt64-b.Cores || a.Instances > math.MaxInt64-b.Instances {
		return Resources{}, false
	}
	return Resources{a.Memory + b.Memory, a.Cores + b.Cores, a.Instances + b.Instances}, true
}

func fits(u, limit Resources) bool {
	return valid(u) && u.Memory <= limit.Memory && u.Cores <= limit.Cores && u.Instances <= limit.Instances
}

// New creates a fresh model. Initial allocation and all capacities must come
// from a consistent external snapshot; this function cannot verify one.
func New(limits, allocated map[DeviceKey]Resources) (*Coordinator, error) {
	c := &Coordinator{limits: map[DeviceKey]Resources{}, used: map[DeviceKey]Resources{}, jobs: map[string]*jobState{},
		podUIDs: map[string]string{}, slots: map[string]Ticket{}, blocked: map[string]map[string]bool{}}
	if _, err := rand.Read(c.epoch[:]); err != nil {
		return nil, fmt.Errorf("coordination epoch: %w", err)
	}
	if len(limits) == 0 {
		return nil, reject("empty capacity snapshot")
	}
	for key, limit := range limits {
		if key.Node == "" || key.GPU == "" || !valid(limit) {
			return nil, reject("invalid device capacity")
		}
		c.limits[key] = limit
		c.used[key] = allocated[key]
		if !fits(c.used[key], limit) {
			return nil, reject("existing allocation exceeds capacity")
		}
	}
	for key := range allocated {
		if _, ok := c.limits[key]; !ok {
			return nil, reject("unknown allocated device")
		}
	}
	return c, nil
}

type allocationKey struct {
	container string
	device    DeviceKey
}

func allocationMap(plan PodPlan) (map[allocationKey]Resources, error) {
	if plan.UID == "" || plan.Node == "" || len(plan.Allocations) == 0 {
		return nil, reject("incomplete Pod identity or allocation")
	}
	out := map[allocationKey]Resources{}
	for _, a := range plan.Allocations {
		key := allocationKey{a.Container, a.Device}
		if a.Container == "" || a.Device.Node != plan.Node || a.Device.GPU == "" || !valid(a.Resources) || a.Resources.Instances <= 0 {
			return nil, reject("invalid container, device or resources")
		}
		if _, exists := out[key]; exists {
			return nil, reject("duplicate container/device allocation")
		}
		out[key] = a.Resources
	}
	return out, nil
}

// Reserve atomically validates and reserves every Pod and GPU in the plan.
// Failed validation has no effects. This is not a Kubernetes resource claim.
func (c *Coordinator) Reserve(plan JobPlan) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if plan.UID == "" || len(plan.Pods) == 0 {
		return reject("empty Job identity or plan")
	}
	if _, exists := c.jobs[plan.UID]; exists {
		return reject("Job UID already used")
	}
	seen, demand := map[string]bool{}, map[DeviceKey]Resources{}
	job := &jobState{pods: map[string]*podState{}}
	for _, pod := range plan.Pods {
		if seen[pod.UID] || c.podUIDs[pod.UID] != "" {
			return reject("Pod UID already used")
		}
		if _, err := allocationMap(pod); err != nil {
			return err
		}
		seen[pod.UID] = true
		pod.Allocations = append([]Allocation(nil), pod.Allocations...)
		for _, a := range pod.Allocations {
			if _, ok := c.limits[a.Device]; !ok {
				return reject("unknown GPU")
			}
			var ok bool
			demand[a.Device], ok = add(demand[a.Device], a.Resources)
			if !ok {
				return reject("resource arithmetic overflow")
			}
		}
		job.pods[pod.UID] = &podState{plan: pod, stage: Reserved}
	}
	for key, requested := range demand {
		total, ok := add(c.used[key], requested)
		if !ok || !fits(total, c.limits[key]) {
			return reject("incomplete Job does not fit")
		}
	}
	for key, requested := range demand {
		c.used[key], _ = add(c.used[key], requested)
	}
	for uid := range seen {
		c.podUIDs[uid] = plan.UID
	}
	c.jobs[plan.UID] = job
	return nil
}

func (c *Coordinator) pod(jobUID, podUID, node string) (*podState, error) {
	job := c.jobs[jobUID]
	if job == nil || job.pods[podUID] == nil {
		return nil, reject("unknown Job or Pod UID")
	}
	p := job.pods[podUID]
	if p.plan.Node != node {
		return nil, reject("Pod node identity mismatch")
	}
	return p, nil
}

func (c *Coordinator) ticket(t Ticket) (*podState, error) {
	p, err := c.pod(t.JobUID, t.PodUID, t.Node)
	if err != nil {
		return nil, err
	}
	if t.epoch != c.epoch || t.sequence == 0 || p.ticket != t {
		return nil, reject("stale or foreign ticket")
	}
	return p, nil
}

// Begin makes external publication uncertain immediately. A slot is retained
// until a verified handshake or explicit resolution of an uncertain outcome.
func (c *Coordinator) Begin(jobUID, podUID, node string) (Ticket, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	p, err := c.pod(jobUID, podUID, node)
	if err != nil {
		return Ticket{}, err
	}
	if p.stage != Reserved {
		return Ticket{}, reject("Pod is not reserved")
	}
	if _, busy := c.slots[node]; busy || len(c.blocked[node]) != 0 {
		return Ticket{}, reject("node has a pending or uncertain candidate")
	}
	if c.seq == math.MaxUint64 {
		return Ticket{}, reject("ticket sequence exhausted")
	}
	c.seq++
	t := Ticket{JobUID: jobUID, PodUID: podUID, Node: node, epoch: c.epoch, sequence: c.seq}
	p.ticket, p.stage, c.slots[node] = t, Publishing, t
	return t, nil
}

func (c *Coordinator) Published(t Ticket) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	p, err := c.ticket(t)
	if err != nil {
		return err
	}
	if p.stage != Publishing || c.slots[t.Node] != t {
		return reject("publication is not pending for this ticket")
	}
	p.stage = Published
	return nil
}

func (c *Coordinator) RecordBound(t Ticket, o BindingObservation) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	p, err := c.ticket(t)
	if err != nil {
		return err
	}
	if p.stage != Published || c.slots[t.Node] != t || o.PodUID != t.PodUID || o.Node != t.Node || o.ObservationID == "" {
		return reject("binding identity or stage not proven")
	}
	p.stage = Bound
	return nil
}

// RecordConsumed accepts only a complete observation of the exact Pod plan.
// This model cannot verify whether the caller's assertions are truthful.
// Neither bind-phase=success nor a scheduler-side plan is a sufficient source.
func (c *Coordinator) RecordConsumed(t Ticket, o ConsumptionObservation) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	p, err := c.ticket(t)
	if err != nil {
		return err
	}
	if p.stage != Bound || c.slots[t.Node] != t || o.PodUID != t.PodUID || o.Node != t.Node || o.ObservationID == "" ||
		!o.BoundUIDVerified || !o.PluginResponseVerified || !o.KubeletCheckpointVerified || !o.NoInFlightAllocate || !o.NodeLockUnheld {
		return reject("complete bound Pod/plugin/kubelet handshake not proven")
	}
	want, _ := allocationMap(p.plan)
	got, err := allocationMap(PodPlan{UID: o.PodUID, Node: o.Node, Allocations: o.Allocations})
	if err != nil || len(want) != len(got) {
		return reject("consumed allocations differ from plan")
	}
	for key, value := range want {
		if got[key] != value {
			return reject("consumed UUID or resource quota mismatch")
		}
	}
	p.stage = Consumed
	delete(c.slots, t.Node) // this only releases the in-memory slot, never a real NodeLock
	return nil
}

func (c *Coordinator) release(p *podState) {
	for _, a := range p.plan.Allocations {
		v := c.used[a.Device]
		c.used[a.Device] = Resources{v.Memory - a.Resources.Memory, v.Cores - a.Resources.Cores, v.Instances - a.Resources.Instances}
	}
}

// Cancel has no external side effects. A never-published reservation can be
// released; any possibly published/bound/consumed Pod becomes uncertain and
// keeps its resources until Resolve is given a separately verified release.
func (c *Coordinator) Cancel(jobUID, podUID, node string) (Stage, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	p, err := c.pod(jobUID, podUID, node)
	if err != nil {
		return "", err
	}
	return c.cancel(p)
}

func (c *Coordinator) cancel(p *podState) (Stage, error) {
	node, podUID := p.plan.Node, p.plan.UID
	switch p.stage {
	case Reserved:
		c.release(p)
		p.stage = Cancelled
	case Publishing, Published, Bound, Consumed:
		p.stage = Uncertain
		if c.blocked[node] == nil {
			c.blocked[node] = map[string]bool{}
		}
		c.blocked[node][podUID] = true
	case Uncertain:
		// Idempotent; the node and reservation stay blocked.
	default:
		return "", reject("Pod already terminal")
	}
	return p.stage, nil
}

// CancelJob drops only never-published reservations. Each possibly published
// member stays uncertain; this is not atomic rollback of an externally bound Job.
func (c *Coordinator) CancelJob(jobUID string) (map[string]Stage, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	job := c.jobs[jobUID]
	if job == nil {
		return nil, reject("unknown Job UID")
	}
	out := map[string]Stage{}
	for uid, pod := range job.pods {
		if pod.stage == Finished || pod.stage == Cancelled {
			out[uid] = pod.stage
			continue
		}
		stage, err := c.cancel(pod)
		if err != nil {
			return nil, err
		} // only internal invalid state can reach this point
		out[uid] = stage
	}
	return out, nil
}

// Fail marks a potentially side-effecting operation uncertain. It never
// releases or deletes a NodeLock and never treats a partial bind as rolled back.
func (c *Coordinator) Fail(t Ticket) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	p, err := c.ticket(t)
	if err != nil {
		return err
	}
	switch p.stage {
	case Publishing, Published, Bound, Consumed:
		p.stage = Uncertain
		if c.blocked[t.Node] == nil {
			c.blocked[t.Node] = map[string]bool{}
		}
		c.blocked[t.Node][t.PodUID] = true
		return nil
	case Uncertain:
		return nil
	default:
		return reject("failure cannot alter a terminal Pod")
	}
}

// Resolve models release after external verification. An uncertain or consumed
// Pod is never released based only on an annotation or disappearance of a plan.
// Other Pods' resources and tickets are untouched.
func (c *Coordinator) Resolve(t Ticket, o ReleaseObservation) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	p, err := c.ticket(t)
	if err != nil {
		return err
	}
	if p.stage != Consumed && p.stage != Uncertain {
		return reject("Pod has not reached a releasable state")
	}
	if o.PodUID != t.PodUID || o.Node != t.Node || o.ObservationID == "" || !o.PodTerminalOrAbsent ||
		!o.DeviceReleaseVerified || !o.NoInFlightAllocate || !o.NodeLockUnheld {
		return reject("termination, release and node quiescence not proven")
	}
	c.release(p)
	if c.slots[t.Node] == t {
		delete(c.slots, t.Node)
	}
	delete(c.blocked[t.Node], t.PodUID)
	if p.stage == Uncertain {
		p.stage = Cancelled
	} else {
		p.stage = Finished
	}
	return nil
}

// State is a copy suitable for assertions. Used includes pre-existing usage and
// all unreleased reservations, avoiding double charging within this model.
type State struct {
	Used    map[DeviceKey]Resources
	Pods    map[string]Stage
	Slots   map[string]string
	Blocked map[string][]string
}

func (c *Coordinator) Snapshot() State {
	c.mu.Lock()
	defer c.mu.Unlock()
	s := State{Used: map[DeviceKey]Resources{}, Pods: map[string]Stage{}, Slots: map[string]string{}, Blocked: map[string][]string{}}
	for k, v := range c.used {
		s.Used[k] = v
	}
	for _, job := range c.jobs {
		for uid, p := range job.pods {
			s.Pods[uid] = p.stage
		}
	}
	for node, ticket := range c.slots {
		s.Slots[node] = ticket.PodUID
	}
	for node, pods := range c.blocked {
		for uid := range pods {
			s.Blocked[node] = append(s.Blocked[node], uid)
		}
		sort.Strings(s.Blocked[node])
	}
	return s
}
