// Copyright 2026 The Volcano Authors. Apache-2.0.
package framework

import (
	"errors"
	"fmt"

	"volcano.sh/volcano/pkg/scheduler/api"
)

// ErrAllocationVeto means no allocation state was changed: callers must not
// run UnAllocate for this specific error. It grants no right to allocate.
var ErrAllocationVeto = errors.New("allocation hard veto")

type namedAllocationVeto struct {
	name string
	fn   api.PredicateFn
}

// AddAllocationVetoFn appends a deny-only guard, checked at both public native
// allocation entry points before task, device ledger, NodeLock or Pod changes.
// Same-name registrations all remain active. It is Session-scoped, does not
// fence external schedulers, and cannot close a check-to-use race by itself.
// A nil function fails closed. With no registrations, native behavior remains.
func (ssn *Session) AddAllocationVetoFn(name string, fn api.PredicateFn) {
	ssn.allocationVetoFns = append(ssn.allocationVetoFns, namedAllocationVeto{name: name, fn: fn})
}

func IsAllocationVeto(err error) bool { return errors.Is(err, ErrAllocationVeto) }

// HasAllocationVeto allows non-transactional actions to opt out when a veto
// could change after their irreversible side effects. It says nothing about
// other schedulers or future registrations in another Session.
func (ssn *Session) HasAllocationVeto() bool { return len(ssn.allocationVetoFns) != 0 }

func (ssn *Session) CheckAllocationVeto(task *api.TaskInfo, node *api.NodeInfo) error {
	if len(ssn.allocationVetoFns) == 0 {
		return nil
	}
	if task == nil || task.Pod == nil || node == nil {
		return fmt.Errorf("%w: missing task, Pod or node", ErrAllocationVeto)
	}
	for _, veto := range ssn.allocationVetoFns {
		if veto.fn == nil {
			return fmt.Errorf("%w: %s has no guard", ErrAllocationVeto, veto.name)
		}
		if err := veto.fn(task, node); err != nil {
			return fmt.Errorf("%w: %s: %v", ErrAllocationVeto, veto.name, err)
		}
	}
	return nil
}
