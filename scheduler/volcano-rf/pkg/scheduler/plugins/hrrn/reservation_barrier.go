// Copyright 2026 The Volcano Authors. Apache-2.0.
package hrrn

import (
	"context"
	"fmt"
	"time"

	"k8s.io/apimachinery/pkg/types"
	"volcano.sh/volcano/pkg/scheduler/api"
	"volcano.sh/volcano/pkg/scheduler/api/devices/nvidia/vgpu"
	"volcano.sh/volcano/pkg/scheduler/conf"
	"volcano.sh/volcano/pkg/scheduler/framework"
	"volcano.sh/volcano/pkg/scheduler/plugins/hrrn/coordination"
)

// A reservation read is only diagnostic, never a fencing mechanism. The
// opt-in barrier therefore denies ALL vGPU allocations until a future atomic
// admission protocol and real UID-bound plugin handoff are implemented. CPU
// allocations are not blocked. This is intentionally not production C mode.
func reservationBarrierRequested(tiers []conf.Tier, args framework.Arguments) bool {
	requested := func(a framework.Arguments) bool {
		value, present := a["reservation-barrier"]
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

type reservationViewFunc func(context.Context) (coordination.ReservationView, error)

// newReservationBarrier never opens an allocation based on a read snapshot.
// A nil or failed source and a quarantined ledger all fail closed.
func newReservationBarrier(view reservationViewFunc) api.PredicateFn {
	return func(task *api.TaskInfo, node *api.NodeInfo) error {
		if task == nil || task.Pod == nil || node == nil {
			return fmt.Errorf("missing Pod or node")
		}
		if !vgpu.HasCoordinatedVGPURequest(task.Pod) {
			return nil
		}
		if view == nil {
			return fmt.Errorf("durable reservation source is unavailable")
		}
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		ledger, err := view(ctx)
		if err != nil {
			return fmt.Errorf("durable reservation read failed: %w", err)
		}
		if ledger.Quarantined {
			return fmt.Errorf("durable reservation recovery is quarantined")
		}
		return fmt.Errorf("vGPU allocation requires atomic admission and UID-bound plugin confirmation (ledger UID=%s RV=%s)", ledger.UID, ledger.ResourceVersion)
	}
}

func (p *Plugin) installReservationBarrier(ssn *framework.Session) {
	var namespace, name, uid, epoch string
	p.args.GetString(&namespace, "reservation-namespace")
	p.args.GetString(&name, "reservation-name")
	p.args.GetString(&uid, "reservation-uid")
	p.args.GetString(&epoch, "reservation-epoch")
	var view reservationViewFunc
	if namespace != "" && name != "" && uid != "" && epoch != "" {
		view = func(ctx context.Context) (coordination.ReservationView, error) {
			store, err := coordination.OpenDurable(ctx, ssn.KubeClient(), namespace, name, types.UID(uid), epoch)
			if err != nil {
				return coordination.ReservationView{}, err
			}
			return store.ReservationView(ctx)
		}
	}
	ssn.AddAllocationVetoFn("hrrn-reservation-barrier", newReservationBarrier(view))
}
