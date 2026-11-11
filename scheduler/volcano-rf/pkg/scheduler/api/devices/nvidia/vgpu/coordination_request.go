// Copyright 2026 The Volcano Authors. Apache-2.0.
package vgpu

import (
	"strings"

	v1 "k8s.io/api/core/v1"
)

// HasCoordinatedVGPURequest conservatively identifies resource declarations
// relevant to the optional multi-Pod handoff guard. Both the configured vGPU
// names and default resource namespace are recognized, including requests
// before admission has copied them to limits. This is not a fit predicate.
func HasCoordinatedVGPURequest(pod *v1.Pod) bool {
	if pod == nil {
		return false
	}
	cfg := getConfig()
	uses := func(resources v1.ResourceList) bool {
		for key := range resources {
			name := string(key)
			if strings.HasPrefix(name, "volcano.sh/vgpu-") ||
				(name != "" && (name == cfg.ResourceCountName || name == cfg.ResourceMemoryName ||
					name == cfg.ResourceMemoryPercentageName || name == cfg.ResourceCoreName)) {
				return true
			}
		}
		return false
	}
	for _, container := range append(append([]v1.Container(nil), pod.Spec.Containers...), pod.Spec.InitContainers...) {
		if uses(container.Resources.Requests) || uses(container.Resources.Limits) {
			return true
		}
	}
	return false
}
