// Copyright 2026 The Volcano Authors.
// Licensed under the Apache License, Version 2.0.

// Package hrrn adds an opt-in, within-fairness-class Job ordering rule.
package hrrn

import (
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"k8s.io/klog/v2"
	"volcano.sh/volcano/pkg/scheduler/api"
	"volcano.sh/volcano/pkg/scheduler/conf"
	"volcano.sh/volcano/pkg/scheduler/framework"
)

const (
	PluginName          = "hrrn"
	EstimateAnnotation  = "gpu-lab/estimated-duration-seconds"
	TotalPodsAnnotation = "gpu-lab/total-pods"
)

// Tracker survives Session/plugin recreation, but NOT process restart. On restart
// waiting conservatively begins at the first complete observation, never Pod birth.
type Tracker struct {
	mu    sync.Mutex
	ready map[string]readyRecord
}
type readyRecord struct {
	signature string
	since     time.Time
}

func NewTracker() *Tracker { return &Tracker{ready: map[string]readyRecord{}} }

var processTracker = NewTracker()

type Snapshot struct {
	Ready            bool
	ReadyAt          time.Time
	WaitingSeconds   float64
	EstimatedSeconds float64
	Score            float64
	Reason           string
	ExpectedPods     int
}

type Plugin struct {
	args              framework.Arguments
	clock             func() time.Time
	tracker           *Tracker
	enabled, complete bool
	coordinated       bool
	now               time.Time
	snapshots         map[api.JobID]Snapshot
}

func New(args framework.Arguments) framework.Plugin {
	return NewWithClock(args, time.Now, processTracker)
}

// NewWithClock is also used by deterministic native scheduler tests.
func NewWithClock(args framework.Arguments, clock func() time.Time, tracker *Tracker) *Plugin {
	return &Plugin{args: args, clock: clock, tracker: tracker, snapshots: map[api.JobID]Snapshot{}}
}
func (p *Plugin) Name() string                           { return PluginName }
func (p *Plugin) Snapshot(id api.JobID) (Snapshot, bool) { s, ok := p.snapshots[id]; return s, ok }
func (p *Plugin) OnSessionClose(*framework.Session)      {}

func positive(x float64) bool { return x > 0 && !math.IsNaN(x) && !math.IsInf(x, 0) }
func estimate(job *api.JobInfo, fallback float64) (float64, string) {
	if job.PodGroup == nil {
		return fallback, "missing_podgroup"
	}
	text, ok := job.PodGroup.Annotations[EstimateAnnotation]
	if !ok {
		return fallback, "estimate_missing_default"
	}
	value, err := strconv.ParseFloat(text, 64)
	if err != nil || !positive(value) {
		return fallback, "estimate_invalid_default"
	}
	return value, "podgroup_annotation"
}

// completeMembers describes submitted, ungated membership, NOT allocated GPU
// readiness and NOT container readiness. Exact declared membership is required.
func completeMembers(job *api.JobInfo) (int, string, string) {
	if job.PodGroup == nil {
		return 0, "", "missing_podgroup"
	}
	if job.PodGroup.DeletionTimestamp != nil {
		return 0, "", "job_deleting"
	}
	n, err := strconv.Atoi(job.PodGroup.Annotations[TotalPodsAnnotation])
	if err != nil || n <= 0 || n < int(job.MinAvailable) {
		return 0, "", "total_pods_missing_or_invalid"
	}
	if len(job.Tasks) != n {
		return n, "", "incomplete_membership"
	}
	ids := make([]string, 0, n)
	for _, task := range job.Tasks {
		if task.Pod == nil || task.Pod.DeletionTimestamp != nil || task.Status == api.Failed {
			return n, "", "member_deleted_or_failed"
		}
		if task.SchGated || len(task.Pod.Spec.SchedulingGates) > 0 {
			return n, "", "member_scheduling_gated"
		}
		ids = append(ids, string(task.UID))
	}
	sort.Strings(ids)
	return n, strings.Join(ids, "|"), "complete_observed"
}

// The plugin fails closed (native ordering) if it precedes or disables one of
// the existing Priority/Gang/DRF Job comparators. Queue/Allocatable hooks stay native.
func safeOrder(tiers []conf.Tier) bool {
	seen := map[string]bool{}
	for _, tier := range tiers {
		for _, op := range tier.Plugins {
			if op.Name == PluginName {
				return seen["priority"] && seen["gang"] && seen["drf"]
			}
			if op.EnabledJobOrder == nil || *op.EnabledJobOrder {
				seen[op.Name] = true
			}
		}
	}
	return false
}

// Complete planning relies on the native predicate rollback handler having run
// first, and on this plugin's JobReady hook being enabled in configuration.
func safeCompleteOrder(tiers []conf.Tier) bool {
	predicatesSeen := false
	for _, tier := range tiers {
		for _, op := range tier.Plugins {
			if op.Name == "predicates" {
				predicatesSeen = true
			}
			if op.Name == PluginName {
				return predicatesSeen && (op.EnabledJobReady == nil || *op.EnabledJobReady)
			}
		}
	}
	return false
}

func (p *Plugin) OnSessionOpen(ssn *framework.Session) {
	if reservationBarrierRequested(ssn.Tiers, p.args) {
		p.installReservationBarrier(ssn)
	}
	// This opt-in is deliberately fail-closed until a persistent device-plugin
	// handoff is integrated. It cannot be used to enable a speculative multi-Pod
	// binding path, including when the remaining HRRN configuration is invalid.
	p.coordinated = coordinatedRequested(ssn.Tiers, p.args)
	p.args.GetBool(&p.enabled, "enabled") // Default false, even if plugin is listed.
	if !p.enabled {
		if p.coordinated {
			p.installCoordinatedFailClosed(ssn)
		}
		return
	}
	if !safeOrder(ssn.Tiers) {
		klog.ErrorS(nil, "HRRN disabled: priority, gang and drf JobOrder must precede hrrn")
		p.enabled = false
		if p.coordinated {
			p.installCoordinatedFailClosed(ssn)
		}
		return
	}
	p.args.GetBool(&p.complete, "complete-job")
	if p.complete && !safeCompleteOrder(ssn.Tiers) {
		klog.ErrorS(nil, "HRRN disabled: complete-job requires predicates before hrrn and enableJobReady=true")
		p.enabled = false
		if p.coordinated {
			p.installCoordinatedFailClosed(ssn)
		}
		return
	}
	tau, fallback := 1.0, 60.0
	p.args.GetFloat64(&tau, "tau-seconds")
	p.args.GetFloat64(&fallback, "default-duration-seconds")
	if !positive(tau) {
		tau = 1
		klog.Warning("HRRN invalid tau; using 1 second")
	}
	if !positive(fallback) {
		fallback = 60
		klog.Warning("HRRN invalid default service; using 60 seconds")
	}
	p.now = p.clock() // Exactly one time sample for this entire Session.
	p.tracker.mu.Lock()
	live := map[string]bool{}
	for id, job := range ssn.Jobs {
		key := string(id)
		if job.PodGroup != nil {
			key += "/" + string(job.PodGroup.UID)
		}
		live[key] = true
		n, sig, reason := completeMembers(job)
		s := Snapshot{Ready: reason == "complete_observed", ReadyAt: p.now, ExpectedPods: n, Reason: reason, Score: 1}
		if s.Ready {
			old, ok := p.tracker.ready[key]
			if !ok || old.signature != sig {
				old = readyRecord{signature: sig, since: p.now}
				p.tracker.ready[key] = old
			}
			s.ReadyAt = old.since
			s.WaitingSeconds = math.Max(0, p.now.Sub(old.since).Seconds())
		} else {
			delete(p.tracker.ready, key)
		}
		var why string
		s.EstimatedSeconds, why = estimate(job, fallback)
		s.Reason += ";" + why
		s.Score = 1 + s.WaitingSeconds/math.Max(s.EstimatedSeconds, tau)
		p.snapshots[id] = s
		klog.InfoS("HRRN session snapshot", "session", ssn.UID, "job", id, "ready", s.Ready, "readyAt", s.ReadyAt.UTC().Format(time.RFC3339Nano), "waitingSeconds", s.WaitingSeconds, "estimatedSeconds", s.EstimatedSeconds, "score", s.Score, "reason", s.Reason, "completeJob", p.complete)
	}
	for key := range p.tracker.ready {
		if !live[key] {
			delete(p.tracker.ready, key)
		}
	}
	p.tracker.mu.Unlock()
	ssn.AddJobOrderFn(p.Name(), p.compare)
	if !p.complete {
		if p.coordinated {
			p.installCoordinatedFailClosed(ssn)
		}
		return
	}
	// Adapt the real allocate Statement: do not commit at a partial minAvailable.
	// Failed complete planning uses native Discard and the original queue bypass.
	ssn.AddJobValidFn(p.Name(), func(obj interface{}) *api.ValidateResult {
		job := obj.(*api.JobInfo)
		if p.coordinated && p.coordinationBlocked(ssn, job) {
			return coordinatedHandoffUnavailable()
		}
		s := p.snapshots[job.UID]
		if !s.Ready {
			return &api.ValidateResult{Pass: false, Reason: "HRRNCompleteMembership", Message: s.Reason}
		}
		for _, task := range job.Tasks {
			if task.Status == api.Pending && task.Resreq.IsEmpty() {
				return &api.ValidateResult{Pass: false, Reason: "HRRNBestEffortUnsupported", Message: "complete-job requires non-BestEffort resource requests; backfill must not partially start the Job"}
			}
		}
		return nil
	})
	ssn.AddJobReadyFn(p.Name(), func(obj interface{}) bool {
		job := obj.(*api.JobInfo)
		s := p.snapshots[job.UID]
		return s.Ready && int(job.ReadyTaskNum()) == s.ExpectedPods
	})
	// Do not retain a partial Statement through future-resource pipelining.
	ssn.AddJobPipelinedVetoFn(p.Name(), func(interface{}) bool { return false })
	p.installRollbackCleanup(ssn)
}

func (p *Plugin) compare(l, r interface{}) int {
	a, b := l.(*api.JobInfo), r.(*api.JobInfo)
	if a.Queue != b.Queue || a.Priority != b.Priority {
		return 0
	}
	x, xok := p.snapshots[a.UID]
	y, yok := p.snapshots[b.UID]
	if !xok || !yok {
		return 0
	}
	if x.Score > y.Score {
		return -1
	}
	if x.Score < y.Score {
		return 1
	}
	if x.ReadyAt.Before(y.ReadyAt) {
		return -1
	}
	if x.ReadyAt.After(y.ReadyAt) {
		return 1
	}
	if a.UID < b.UID {
		return -1
	}
	if a.UID > b.UID {
		return 1
	}
	return 0
}

func (s Snapshot) String() string {
	return fmt.Sprintf("ready=%v score=%.6f reason=%s", s.Ready, s.Score, s.Reason)
}
