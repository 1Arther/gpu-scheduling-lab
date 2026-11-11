package preempt

import (
	"testing"

	"volcano.sh/volcano/pkg/scheduler/api"
	"volcano.sh/volcano/pkg/scheduler/framework"
)

func TestPreemptDisabledBeforeVictimSelectionWithHardVeto(t *testing.T) {
	// An incomplete Job and nil cache would panic if Execute reached the native
	// traversal or eviction path. The guarded action must return before either.
	s := &framework.Session{Jobs: map[api.JobID]*api.JobInfo{"poison": nil}}
	s.AddAllocationVetoFn("guard", nil)
	New().Execute(s)
}
