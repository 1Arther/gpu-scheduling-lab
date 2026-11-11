package framework

import (
	"errors"
	"fmt"
	"testing"

	v1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"volcano.sh/volcano/pkg/scheduler/api"
)

func TestAllocationVetoBeforeSessionAndStatementSideEffects(t *testing.T) {
	for _, path := range []string{"predicate", "session", "statement", "recover", "session-pipeline", "statement-pipeline", "recover-pipeline"} {
		t.Run(path, func(t *testing.T) {
			s := &Session{Nodes: map[string]*api.NodeInfo{}}
			task := &api.TaskInfo{Pod: &v1.Pod{}}
			task.NodeName = "original"
			node := &api.NodeInfo{Name: "node-a"}
			s.Nodes[node.Name] = node
			s.AddAllocationVetoFn("reserved", func(*api.TaskInfo, *api.NodeInfo) error { return errors.New("deny") })
			calls := 0
			s.eventHandlers = []*EventHandler{{AllocateFunc: func(*Event) { calls++ }, DeallocateFunc: func(*Event) { calls++ }}}
			var err error
			switch path {
			case "predicate":
				err = s.PredicateFn(task, node)
			case "session":
				err = s.Allocate(task, node)
			case "statement":
				err = NewStatement(s).Allocate(task, node)
			case "session-pipeline":
				err = s.Pipeline(task, node.Name)
			case "statement-pipeline":
				err = NewStatement(s).Pipeline(task, node.Name, false)
			case "recover":
				task.NodeName = node.Name
				saved := &Statement{operations: []operation{{name: Allocate, task: task}}}
				err = NewStatement(s).RecoverOperations(saved)
			case "recover-pipeline":
				task.NodeName = node.Name
				saved := &Statement{operations: []operation{{name: Pipeline, task: task}}}
				err = NewStatement(s).RecoverOperations(saved)
			}
			if !IsAllocationVeto(err) || task.Pod.Spec.NodeName != "" || calls != 0 {
				t.Fatalf("side effects or wrong error: err=%v podnode=%q callbacks=%d", err, task.Pod.Spec.NodeName, calls)
			}
			wantNode := "original"
			if path == "recover" || path == "recover-pipeline" {
				wantNode = node.Name
			}
			if task.NodeName != wantNode {
				t.Fatalf("premature rollback altered NodeName: %q", task.NodeName)
			}
		})
	}
}

func TestRecoverOperationsVetoDiscardsPreviouslyRecoveredCPU(t *testing.T) {
	for _, first := range []Operation{Allocate, Pipeline} {
		for _, second := range []Operation{Allocate, Pipeline} {
			t.Run(fmt.Sprintf("first-%d-second-%d", first, second), func(t *testing.T) {
				makeTask := func(name string) *api.TaskInfo {
					pod := &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "ns", UID: types.UID(name)}, Status: v1.PodStatus{Phase: v1.PodPending}}
					task := api.NewTaskInfo(pod)
					task.Job = "job"
					task.NodeName = "node"
					return task
				}
				cpu, gpu := makeTask("cpu"), makeTask("gpu")
				job := api.NewJobInfo("job", cpu, gpu)
				node := api.NewNodeInfo(&v1.Node{ObjectMeta: metav1.ObjectMeta{Name: "node"}})
				s := &Session{Jobs: map[api.JobID]*api.JobInfo{"job": job}, Nodes: map[string]*api.NodeInfo{"node": node}}
				allocated, removed := 0, 0
				s.eventHandlers = []*EventHandler{{AllocateFunc: func(*Event) { allocated++ }, DeallocateFunc: func(*Event) { removed++ }}}
				s.AddAllocationVetoFn("deny-gpu", func(task *api.TaskInfo, _ *api.NodeInfo) error {
					if task.Name == "gpu" {
						return errors.New("deny")
					}
					return nil
				})
				stmt := NewStatement(s)
				saved := &Statement{operations: []operation{{name: first, task: cpu}, {name: second, task: gpu}}}
				if err := stmt.RecoverOperations(saved); !IsAllocationVeto(err) {
					t.Fatalf("expected veto, got %v", err)
				}
				if allocated != 1 || removed != 1 || len(stmt.operations) != 0 || len(node.Tasks) != 0 || cpu.Status != api.Pending || gpu.Status != api.Pending || cpu.Pod.Spec.NodeName != "" || gpu.Pod.Spec.NodeName != "" {
					t.Fatalf("recovered CPU operation leaked: allocated=%d removed=%d operations=%d nodeTasks=%d statuses=%v/%v", allocated, removed, len(stmt.operations), len(node.Tasks), cpu.Status, gpu.Status)
				}
			})
		}
	}
}

func TestAllocationVetoCompositionAndMissingInputs(t *testing.T) {
	s := &Session{}
	if err := s.CheckAllocationVeto(nil, nil); err != nil {
		t.Fatal("empty guard changed native behavior")
	}
	task := &api.TaskInfo{Pod: &v1.Pod{}}
	node := &api.NodeInfo{Name: "node"}
	s.AddAllocationVetoFn("same", func(*api.TaskInfo, *api.NodeInfo) error { return errors.New("deny") })
	s.AddAllocationVetoFn("same", func(*api.TaskInfo, *api.NodeInfo) error { return nil })
	if !IsAllocationVeto(s.CheckAllocationVeto(task, node)) {
		t.Fatal("later same-name guard replaced earlier deny")
	}
	if !IsAllocationVeto(s.CheckAllocationVeto(nil, node)) {
		t.Fatal("nil task accepted")
	}
	if !IsAllocationVeto(s.CheckAllocationVeto(task, nil)) {
		t.Fatal("nil node accepted")
	}
	s = &Session{}
	s.AddAllocationVetoFn("nil", nil)
	if !IsAllocationVeto(s.CheckAllocationVeto(task, node)) {
		t.Fatal("nil guard accepted")
	}
}
