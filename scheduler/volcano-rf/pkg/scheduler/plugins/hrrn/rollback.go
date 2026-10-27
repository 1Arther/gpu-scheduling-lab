// Copyright 2026 The Volcano Authors. Apache-2.0.
package hrrn

import (
	"context"
	"encoding/json"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/klog/v2"
	"time"
	"volcano.sh/volcano/pkg/scheduler/api"
	"volcano.sh/volcano/pkg/scheduler/api/devices/nvidia/vgpu"
	"volcano.sh/volcano/pkg/scheduler/framework"
)

// The native vGPU Allocate callback patches UUIDs before Statement.Commit.
// Native Discard restores the ledger, but Release is a no-op. Restore only the
// assignment metadata of unbound pending Pods; never alter RF scoring or quotas.
func (p *Plugin) installRollbackCleanup(ssn *framework.Session) {
	keys := []string{vgpu.AssignedNodeAnnotations, vgpu.AssignedTimeAnnotations, vgpu.AssignedIDsAnnotations,
		vgpu.AssignedIDsToAllocateAnnotations, vgpu.DeviceBindPhase, vgpu.BindTimeAnnotations}
	before := map[api.TaskID]map[string]interface{}{}
	for _, j := range ssn.Jobs {
		for _, task := range j.TaskStatusIndex[api.Pending] {
			values := map[string]interface{}{}
			for _, key := range keys {
				if v, ok := task.Pod.Annotations[key]; ok {
					values[key] = v
				} else {
					values[key] = nil
				}
			}
			before[task.UID] = values
		}
	}
	ssn.AddEventHandler(&framework.EventHandler{DeallocateFunc: func(event *framework.Event) {
		task := event.Task
		values, ok := before[task.UID]
		if !ok || task.Status != api.Pending {
			return
		}
		// Runs after predicates.DeallocateFunc (config places HRRN last).
		task.Pod.Spec.NodeName = ""
		changed := false
		for key, value := range values {
			if value == nil {
				if _, exists := task.Pod.Annotations[key]; exists {
					changed = true
				}
				delete(task.Pod.Annotations, key)
			} else {
				if task.Pod.Annotations[key] != value.(string) {
					changed = true
				}
				task.Pod.Annotations[key] = value.(string)
			}
		}
		if !changed {
			return
		}
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		pod, err := ssn.KubeClient().CoreV1().Pods(task.Namespace).Get(ctx, task.Name, metav1.GetOptions{})
		if err != nil {
			klog.ErrorS(err, "HRRN rollback metadata read failed", "pod", task.UID)
			return
		}
		if pod.UID != task.Pod.UID || pod.Spec.NodeName != "" {
			klog.ErrorS(nil, "HRRN rollback refuses replaced/bound Pod", "pod", task.UID)
			return
		}
		// ResourceVersion is an optimistic concurrency precondition on a real API.
		metadata := map[string]interface{}{"uid": pod.UID, "annotations": values}
		if pod.ResourceVersion != "" {
			metadata["resourceVersion"] = pod.ResourceVersion
		}
		body, _ := json.Marshal(map[string]interface{}{"metadata": metadata})
		_, err = ssn.KubeClient().CoreV1().Pods(task.Namespace).Patch(ctx, task.Name, types.MergePatchType, body, metav1.PatchOptions{})
		if err != nil {
			klog.ErrorS(err, "HRRN rollback metadata patch failed; native ledger already rolled back", "pod", task.UID)
		}
	}})
}
