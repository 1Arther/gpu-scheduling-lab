// Copyright 2026 The Volcano Authors. Apache-2.0.
package vgpu

import (
	"context"
	"encoding/json"
	"fmt"

	v1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	k8stypes "k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes"
)

// PatchPodAnnotationsIfUnchanged is a separate opt-in API primitive for a
// future coordinated path. It does not acquire/release NodeLock, reserve
// devices or modify a resource ledger. A write error can have an unknown
// outcome: callers must reconcile it before retrying or releasing anything.
// Existing RF and legacy callers do not use this helper.
//
// The fresh GET supplies other annotations to preserve, but is not the safety
// check by itself: JSON Patch tests UID and resourceVersion in the same API
// request as the mutation, protecting against same-name replacement and ABA.
func PatchPodAnnotationsIfUnchanged(ctx context.Context, kubeClient kubernetes.Interface, pod *v1.Pod, annotations map[string]string) (*v1.Pod, error) {
	if ctx == nil || kubeClient == nil || pod == nil || pod.Namespace == "" || pod.Name == "" || pod.UID == "" || pod.ResourceVersion == "" || len(annotations) == 0 {
		return nil, fmt.Errorf("coordinated pod annotation patch requires namespace, name, UID, resourceVersion and annotations")
	}
	current, err := kubeClient.CoreV1().Pods(pod.Namespace).Get(ctx, pod.Name, metav1.GetOptions{})
	if err != nil {
		return nil, err
	}
	if current.UID != pod.UID || current.ResourceVersion != pod.ResourceVersion || current.DeletionTimestamp != nil {
		return nil, fmt.Errorf("coordinated pod annotation patch: stale Pod UID or resourceVersion, or Pod is deleting")
	}
	merged := make(map[string]string, len(current.Annotations)+len(annotations))
	for key, value := range current.Annotations {
		merged[key] = value
	}
	for key, value := range annotations {
		merged[key] = value
	}
	type operation struct {
		Op    string      `json:"op"`
		Path  string      `json:"path"`
		Value interface{} `json:"value"`
	}
	patch := []operation{
		{Op: "test", Path: "/metadata/uid", Value: string(pod.UID)},
		{Op: "test", Path: "/metadata/resourceVersion", Value: pod.ResourceVersion},
		{Op: "add", Path: "/metadata/annotations", Value: merged},
	}
	data, err := json.Marshal(patch)
	if err != nil {
		return nil, err
	}
	updated, err := kubeClient.CoreV1().Pods(pod.Namespace).Patch(ctx, pod.Name, k8stypes.JSONPatchType, data, metav1.PatchOptions{})
	if err != nil {
		return nil, err
	}
	if updated == nil || updated.UID != pod.UID || updated.Name != pod.Name || updated.Namespace != pod.Namespace || updated.ResourceVersion == "" {
		return nil, fmt.Errorf("coordinated pod annotation patch: ambiguous API response; reconcile")
	}
	for key, value := range annotations {
		if updated.Annotations[key] != value {
			return nil, fmt.Errorf("coordinated pod annotation patch: unexpected API response; reconcile")
		}
	}
	return updated, nil
}
