package vgpu

import (
	"fmt"
	"time"

	v1 "k8s.io/api/core/v1"
	"k8s.io/client-go/kubernetes"
)

// Keep the configured legacy device order and native feasibility rules. The
// canary changes the commit synchronization, not the DQN policy or its service.
// This mutex coordinates ONLY this process; it is not cross-scheduler isolation.
func (gs *GPUDevices) allocateLegacyShared(client kubernetes.Interface, pod *v1.Pod) error {
	if !checkVGPUResourcesInPod(pod) {
		return nil
	}
	if gs.Mode != vGPUControllerHAMICore {
		return fmt.Errorf("mixed-policy MVP requires hami-core; do not enable on MIG nodes")
	}
	rfLedgerMu.RLock()
	present := rfAlreadyAllocated(gs, pod)
	snap := rfClone(gs)
	rfLedgerMu.RUnlock()
	if present {
		return fmt.Errorf("legacy Pod already accounted; refusing duplicate Allocate")
	}
	token := rfSnapshotToken(snap)
	// Existing DQN RPCs run outside the ledger lock; Python is not called here.
	fit, devices, score, err := checkNodeGPUSharingNative(pod, snap, false, "")
	if err != nil {
		return err
	}
	if !fit {
		return fmt.Errorf("legacy native policy cannot fit Pod")
	}
	plan := &rfPlan{Created: time.Now(), SnapshotToken: token, Devices: devices, Score: score}
	plan.RequestID = rfHash(struct{ Token, UID, Policy string }{token, string(pod.UID), GPUSelectPolicy})
	rfLedgerMu.Lock()
	defer rfLedgerMu.Unlock()
	if rfSnapshotToken(gs) != token || rfAlreadyAllocated(gs, pod) {
		return fmt.Errorf("legacy stale plan; retry scheduling")
	}
	return gs.commitNativePlan(client, pod, plan, devices, "legacy-"+normalizeGPUSelectPolicy(GPUSelectPolicy))
}
