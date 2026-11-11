package coordination

import (
	"context"
	"sort"

	"k8s.io/apimachinery/pkg/types"
)

// ReservationMember is a copy of a live or uncertain ledger entry. A
// Consumed member is included until verified release; these figures are
// reservation charges, not measured GPU utilization.
type ReservationMember struct {
	JobUID, PodUID, Node string
	Stage                Stage
	Allocations          []Allocation
}

// ReservationView is a point-in-time read of one ConfigMap. It is diagnostic
// data only: it does not freeze the API object, fence another scheduler, or
// establish that the external baseline is accurate. A scheduler must not use
// an empty view as permission to allocate, because another writer could
// reserve immediately after the read.
type ReservationView struct {
	UID             types.UID
	ResourceVersion string
	Epoch           string
	Quarantined     bool
	Charged         map[DeviceKey]Resources
	Members         []ReservationMember
}

// ReservationView obtains one validated ledger revision and reconstructs
// reservation charges without counting the separate external baseline. All
// returned maps and allocations are copies. Even an Uncertain Pod continues
// to be charged until a separate verified release is committed.
func (d *DurableCoordinator) ReservationView(ctx context.Context) (ReservationView, error) {
	cm, ledger, model, err := d.read(ctx)
	if err != nil {
		return ReservationView{}, err
	}
	view := ReservationView{UID: cm.UID, ResourceVersion: cm.ResourceVersion, Epoch: ledger.Epoch,
		Quarantined: ledger.Quarantined, Charged: make(map[DeviceKey]Resources)}
	for jobUID, job := range model.jobs {
		for _, pod := range job.pods {
			if pod.stage == Finished || pod.stage == Cancelled {
				continue
			}
			member := ReservationMember{JobUID: jobUID, PodUID: pod.plan.UID, Node: pod.plan.Node,
				Stage: pod.stage, Allocations: append([]Allocation(nil), pod.plan.Allocations...)}
			for _, allocation := range member.Allocations {
				charged, ok := add(view.Charged[allocation.Device], allocation.Resources)
				if !ok {
					return ReservationView{}, reject("reservation view overflow")
				}
				view.Charged[allocation.Device] = charged
			}
			view.Members = append(view.Members, member)
		}
	}
	sort.Slice(view.Members, func(i, j int) bool {
		if view.Members[i].JobUID == view.Members[j].JobUID {
			return view.Members[i].PodUID < view.Members[j].PodUID
		}
		return view.Members[i].JobUID < view.Members[j].JobUID
	})
	return view, nil
}
