// Copyright 2026 The Volcano Authors. Apache-2.0.
package hrrn

import (
	"context"
	"strconv"

	"volcano.sh/volcano/pkg/scheduler/api"
	"volcano.sh/volcano/pkg/scheduler/api/devices/nvidia/vgpu"
	"volcano.sh/volcano/pkg/scheduler/conf"
	"volcano.sh/volcano/pkg/scheduler/framework"
)

// coordinatedRequested treats all same-name plugin entries as one safety
// domain. Framework registration is keyed by plugin name; a later instance
// must not overwrite an earlier fail-closed guard with an unguarded callback.
// An explicitly malformed value is treated as a request for safety.
func coordinatedRequested(tiers []conf.Tier, args framework.Arguments) bool {
	requested := func(arguments framework.Arguments) bool {
		value, present := arguments["coordinated-complete-job"]
		if !present {
			return false
		}
		flag, valid := value.(bool)
		return !valid || flag
	}
	if requested(args) {
		return true
	}
	for _, tier := range tiers {
		for _, plugin := range tier.Plugins {
			if plugin.Name == PluginName && requested(plugin.Arguments) {
				return true
			}
		}
	}
	return false
}

// requiresCoordinatedHandoff is intentionally conservative. The existing
// Device Plugin protocol does not identify a Pod in AllocateRequest and the
// production coordinator is not yet available. Even a fully feasible GPU
// plan must not publish any members of a multi-Pod Job through the old path.
// Unknown declared membership also fails closed for a GPU Job: one observed
// Pod need not mean that it is the Job's only member.
func requiresCoordinatedHandoff(job *api.JobInfo, verifiedMembership func(*api.JobInfo) bool) bool {
	if job == nil || job.PodGroup == nil {
		return true
	}
	usesVGPU := false
	for _, task := range job.Tasks {
		if task == nil || task.Pod == nil {
			return true
		}
		if vgpu.HasCoordinatedVGPURequest(task.Pod) {
			usesVGPU = true
		}
	}
	expected, err := strconv.Atoi(job.PodGroup.Annotations[TotalPodsAnnotation])
	// An unobserved member might request vGPU even when every member seen so
	// far is CPU-only. This also matters when coordinated mode was explicitly
	// requested alongside invalid/disabled complete-job configuration.
	if err != nil || expected < 1 || expected != len(job.Tasks) || expected < int(job.MinAvailable) {
		return true
	}
	if usesVGPU && (expected > 1 || job.MinAvailable > 1) {
		return true
	}
	// Even apparently CPU-only members might be a prefix of a larger vGPU Job
	// with a false declared total. Verify every allowed Job against the owner
	// Job's spec; ownerless or unknown members conservatively remain pending.
	return verifiedMembership == nil || !verifiedMembership(job)
}

func coordinatedHandoffUnavailable() *api.ValidateResult {
	return &api.ValidateResult{Pass: false, Reason: "HRRNCoordinatedHandoffUnavailable",
		Message: "Job membership is unverified or multi-Pod vGPU handoff is not yet integrated"}
}

func (p *Plugin) installCoordinatedFailClosed(ssn *framework.Session) {
	ssn.AddJobValidFn(p.Name(), func(obj interface{}) *api.ValidateResult {
		job, ok := obj.(*api.JobInfo)
		if !ok || p.coordinationBlocked(ssn, job) {
			return coordinatedHandoffUnavailable()
		}
		return nil
	})
}

func (p *Plugin) coordinationBlocked(ssn *framework.Session, job *api.JobInfo) bool {
	return requiresCoordinatedHandoff(job, func(candidate *api.JobInfo) bool {
		return verifiedVolcanoMembership(context.Background(), candidate, ssn.VCClient())
	})
}
