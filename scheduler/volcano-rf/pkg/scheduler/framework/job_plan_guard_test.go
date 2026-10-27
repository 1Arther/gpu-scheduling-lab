package framework

import (
	"testing"
	"volcano.sh/volcano/pkg/scheduler/api"
	"volcano.sh/volcano/pkg/scheduler/conf"
)

func TestJobPipelinedHardVetoPrecedesTierPermit(t *testing.T) {
	yes := true
	s := &Session{Tiers: []conf.Tier{{Plugins: []conf.PluginOption{{Name: "gang", EnabledJobPipelined: &yes}}}, {Plugins: []conf.PluginOption{{Name: "later", EnabledJobPipelined: &yes}}}}, jobPipelinedFns: map[string]api.VoteFn{"gang": func(interface{}) int { return 1 }, "later": func(interface{}) int { return -1 }}}
	if !s.JobPipelined(nil) {
		t.Fatal("native early tier Permit changed without guard")
	}
	s.AddJobPipelinedVetoFn("complete", func(interface{}) bool { return false })
	if s.JobPipelined(nil) {
		t.Fatal("early Permit bypassed complete planning veto")
	}
	s.jobPipelinedFns["gang"] = func(interface{}) int { return -1 }
	s.AddJobPipelinedVetoFn("complete", func(interface{}) bool { return true })
	if s.JobPipelined(nil) {
		t.Fatal("veto hook granted a rejected native pipeline")
	}
}
