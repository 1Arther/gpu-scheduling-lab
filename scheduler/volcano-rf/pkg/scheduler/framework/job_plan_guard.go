// Copyright 2026 The Volcano Authors. Apache-2.0.
package framework

import "volcano.sh/volcano/pkg/scheduler/api"

// AddJobPipelinedVetoFn installs a hard, deny-only pipelining guard. It cannot
// grant resources or bypass any native queue/gang/predicate constraint.
// Unlike tier votes, it cannot be skipped by an earlier tier's Permit.
func (ssn *Session) AddJobPipelinedVetoFn(name string, fn api.ValidateFn) {
	if ssn.jobPipelinedVetoFns == nil {
		ssn.jobPipelinedVetoFns = map[string]api.ValidateFn{}
	}
	ssn.jobPipelinedVetoFns[name] = fn
}
