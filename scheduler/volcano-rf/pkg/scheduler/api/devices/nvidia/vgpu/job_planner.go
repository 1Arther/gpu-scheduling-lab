// Copyright 2026 The Volcano Authors. Apache-2.0.
package vgpu

import (
	"errors"
	"fmt"
	"sort"
	"time"

	v1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/types"
)

// This is a vGPU-only planning primitive. It does not publish Pod annotations,
// lock Nodes, bind Pods, reserve real resources, or validate Kubernetes/Volcano
// CPU, memory, queues, affinity, volume, gang, topology, or other predicates.
// Device-plugin handoff and durable, cross-Session reservations are separate.
var (
	ErrVGPUPlanNoCandidate = errors.New("no complete vGPU plan in the explored RF candidates")
	ErrVGPUPlanSearchLimit = errors.New("vGPU plan search limit reached")
	ErrVGPUPlanStale       = errors.New("vGPU source ledger changed during or after planning")
)

// A limit is necessary because exploring node combinations is exponential.
// Reaching it is inconclusive, never proof that the Job is infeasible.
const VGPUJobPlanAttemptLimit = 4096

// Each individual RF request has its own configured timeout; a single call
// already in flight can extend this bound by up to that request timeout.
const VGPUJobPlanTimeLimit = 30 * time.Second

// VGPUJobMemberPlan is an offline proposal, not an allocated or bound Pod.
// Devices is indexed by the Pod's container index. SnapshotToken describes the
// private scratch ledger immediately before this member's proposed placement.
type VGPUJobMemberPlan struct {
	PodUID        types.UID
	Namespace     string
	Name          string
	Node          string
	Devices       []ContainerDevices
	SnapshotToken string
	RequestID     string
	Score         float64
	Fallback      string
}

// VGPUJobPlan contains only a complete proposal. NodeSnapshots are tokens of
// the original coherent ledger snapshot, before any scratch placements.
// The mapping must be revalidated immediately before any future commit and is
// not an authority to allocate. A plan does not establish a durable reservation.
type VGPUJobPlan struct {
	Members        []VGPUJobMemberPlan
	NodeSnapshots  map[string]string
	PolicySnapshot string
	Attempts       int
}

type vgpuCandidate struct {
	node string
	plan *rfPlan
}

// rfSnapshotToken intentionally hashes the features used by the RF adviser.
// This stronger source token also includes each PodMap identity, so replacing a
// Pod with an equal aggregate usage cannot silently pass a later stale check.
func vgpuJobSourceToken(node *GPUDevices) string {
	return rfHash(struct {
		Name, Mode string
		Devices    map[int]*GPUDevice
	}{node.Name, node.Mode, node.Device})
}

func vgpuJobPolicyToken() string {
	return rfHash(struct {
		Enabled, VGPUEnabled, CanaryEnabled, PredictionEnabled bool
		Policy, ModelVersion, Endpoint, CanaryNamespace        string
		TimeoutMS                                              int
		DeviceConfig                                           interface{}
	}{RFEnabled, VGPUEnable, RFCanaryEnabled, RFPredictionEnabled, GPUSelectPolicy,
		RFModelVersion, RFEndpoint, RFCanaryNamespace, RFTimeoutMS, getConfig()})
}

// PlanCompleteVGPUJob searches candidate nodes in the supplied order of Pods.
// At each depth it tries the unchanged rf-job device proposal for each node,
// in descending RF score and then node-name order. It backtracks between nodes,
// not alternate GPU combinations on the same node; failure is consequently
// only a failure to find a plan among these candidates. RF's existing fallback
// and HTTP request/cache semantics are unchanged. Callers must hold their own
// lock if the input nodes map or Pod objects can be modified concurrently.
// Existing RF config globals must also remain immutable while this runs; the
// fingerprint detects stable before/after changes, not a concurrent ABA. The
// caller must separately freeze and verify the model file's actual contents.
func PlanCompleteVGPUJob(nodes map[string]*GPUDevices, pods []*v1.Pod) (*VGPUJobPlan, error) {
	if !RFActive() {
		return nil, fmt.Errorf("vGPU job planner requires active rf-job policy")
	}
	if len(nodes) == 0 || len(pods) == 0 {
		return nil, fmt.Errorf("vGPU job planner requires nodes and Pods")
	}
	// Copy Pod requests so caller-owned maps, labels and annotations remain
	// untouched. A pending proposal is not valid for already-bound Pods.
	requests := make([]*v1.Pod, len(pods))
	seenPods := make(map[types.UID]bool, len(pods))
	seenNames := make(map[string]bool, len(pods))
	for i, pod := range pods {
		if pod == nil || pod.UID == "" || pod.Namespace == "" || pod.Name == "" || seenPods[pod.UID] || seenNames[pod.Namespace+"/"+pod.Name] {
			return nil, fmt.Errorf("missing or duplicate Pod identity at index %d", i)
		}
		if pod.Spec.NodeName != "" || pod.DeletionTimestamp != nil ||
			(pod.Status.Phase != "" && pod.Status.Phase != v1.PodPending) ||
			!RFForPod(pod) || !checkVGPUResourcesInPod(pod) {
			return nil, fmt.Errorf("Pod %s/%s must be pending and use the active RF vGPU path", pod.Namespace, pod.Name)
		}
		for _, key := range []string{AssignedNodeAnnotations, AssignedTimeAnnotations, AssignedIDsAnnotations,
			AssignedIDsToAllocateAnnotations, DeviceBindPhase, BindTimeAnnotations} {
			if pod.Annotations[key] != "" {
				return nil, fmt.Errorf("Pod %s/%s has existing vGPU allocation metadata", pod.Namespace, pod.Name)
			}
		}
		if _, err := rfPodRequest(pod); err != nil {
			return nil, fmt.Errorf("invalid Pod %s/%s: %w", pod.Namespace, pod.Name, err)
		}
		seenPods[pod.UID] = true
		seenNames[pod.Namespace+"/"+pod.Name] = true
		requests[i] = pod.DeepCopy()
	}

	names := make([]string, 0, len(nodes))
	for name := range nodes {
		names = append(names, name)
	}
	sort.Strings(names)
	working := make(map[string]*GPUDevices, len(nodes))
	snapshots := make(map[string]string, len(nodes))
	globalUUIDs := map[string]bool{}
	// One lock covers all node copies: proposals start from a coherent snapshot
	// of this process's native RF ledger. No lock is held across adviser calls.
	rfLedgerMu.RLock()
	for _, name := range names {
		node := nodes[name]
		if node == nil || name == "" || node.Name != name || node.Mode != vGPUControllerHAMICore {
			rfLedgerMu.RUnlock()
			return nil, fmt.Errorf("invalid or unsupported vGPU node %q", name)
		}
		if _, ok := node.Sharing.(HAMICoreFactory); !ok {
			rfLedgerMu.RUnlock()
			return nil, fmt.Errorf("node %q has an unsupported sharing implementation", name)
		}
		for _, gpu := range node.Device {
			if gpu == nil || gpu.UUID == "" || gpu.Node != name || globalUUIDs[gpu.UUID] {
				rfLedgerMu.RUnlock()
				return nil, fmt.Errorf("invalid or duplicate GPU identity on node %q", name)
			}
			globalUUIDs[gpu.UUID] = true
			for uid := range seenPods {
				if gpu.PodMap[string(uid)] != nil {
					rfLedgerMu.RUnlock()
					return nil, fmt.Errorf("Pod %s is already present in a vGPU ledger", uid)
				}
			}
		}
		// Hash the original representation, not the clone: clone helpers may
		// normalize nil/empty nested fields while preserving placement meaning.
		snapshots[name] = vgpuJobSourceToken(node)
		working[name] = rfClone(node)
	}
	rfLedgerMu.RUnlock()

	proposal := &VGPUJobPlan{NodeSnapshots: snapshots, PolicySnapshot: vgpuJobPolicyToken()}
	deadline := time.Now().Add(VGPUJobPlanTimeLimit)
	var search func(int, map[string]*GPUDevices) (bool, error)
	search = func(index int, scratch map[string]*GPUDevices) (bool, error) {
		if index == len(requests) {
			return true, nil
		}
		pod := requests[index]
		candidates := make([]vgpuCandidate, 0, len(names))
		for _, name := range names {
			if proposal.Attempts >= VGPUJobPlanAttemptLimit || time.Now().After(deadline) {
				return false, ErrVGPUPlanSearchLimit
			}
			proposal.Attempts++
			plan, err := rfPlanFor(scratch[name], pod)
			if time.Now().After(deadline) {
				return false, ErrVGPUPlanSearchLimit
			}
			if err == nil && len(plan.Devices) == len(pod.Spec.Containers) {
				candidates = append(candidates, vgpuCandidate{node: name, plan: plan})
			}
		}
		sort.SliceStable(candidates, func(i, j int) bool {
			if candidates[i].plan.Score != candidates[j].plan.Score {
				return candidates[i].plan.Score > candidates[j].plan.Score
			}
			return candidates[i].node < candidates[j].node
		})
		for _, candidate := range candidates {
			privateNode := rfClone(scratch[candidate.node])
			plan := candidate.plan
			if rfSnapshotToken(privateNode) != plan.SnapshotToken {
				return false, ErrVGPUPlanStale
			}
			assignments := []rfAssignment{}
			for ci, devices := range plan.Devices {
				for unit, device := range devices {
					assignments = append(assignments, rfAssignment{Container: pod.Spec.Containers[ci].Name, Unit: unit,
						UUID: device.UUID, Node: candidate.node, Memory: device.Usedmem, Compute: device.Usedcores, Instances: 1})
				}
			}
			devices, err := rfValidate(privateNode, pod, assignments)
			if err != nil || len(assignments) == 0 {
				continue
			}
			// Account on the private copy using the same native HAMi accounting
			// and RF workload metadata, without global Prometheus metrics or API.
			byUUID := make(map[string]*GPUDevice, len(privateNode.Device))
			for _, device := range privateNode.Device {
				byUUID[device.UUID] = device
			}
			valid := true
			for _, container := range devices {
				for _, assignment := range container {
					device := byUUID[assignment.UUID]
					if device == nil || privateNode.Sharing.AddPod(device, assignment.Usedmem, assignment.Usedcores, string(pod.UID), assignment.UUID) != nil {
						valid = false
						break
					}
					rfRecordPod(device, pod, assignment.Usedcores, 1)
				}
				if !valid {
					break
				}
			}
			if !valid {
				continue // Throw away the entire private node, including partial work.
			}
			member := VGPUJobMemberPlan{PodUID: pod.UID, Namespace: pod.Namespace, Name: pod.Name,
				Node: candidate.node, Devices: devices, SnapshotToken: plan.SnapshotToken,
				RequestID: plan.RequestID, Score: plan.Score, Fallback: plan.Fallback}
			proposal.Members = append(proposal.Members, member)
			next := make(map[string]*GPUDevices, len(scratch))
			for name, node := range scratch {
				next[name] = node
			}
			next[candidate.node] = privateNode
			found, err := search(index+1, next)
			if err != nil {
				return false, err
			}
			if found {
				return true, nil
			}
			proposal.Members = proposal.Members[:len(proposal.Members)-1]
		}
		return false, nil
	}
	found, err := search(0, working)
	if err != nil {
		return nil, err
	}
	// A stale source must never be accepted as proof even if no candidate fit.
	if err := ValidateVGPUJobPlanSnapshot(nodes, proposal); err != nil {
		return nil, err
	}
	if !found {
		return nil, ErrVGPUPlanNoCandidate
	}
	return proposal, nil
}

// ValidateVGPUJobPlanSnapshot checks only native vGPU ledger drift. It does not
// claim that node predicates, Pod identities, quotas, or API state are current.
func ValidateVGPUJobPlanSnapshot(nodes map[string]*GPUDevices, plan *VGPUJobPlan) error {
	if plan == nil || len(nodes) != len(plan.NodeSnapshots) || !RFActive() || plan.PolicySnapshot != vgpuJobPolicyToken() {
		return ErrVGPUPlanStale
	}
	rfLedgerMu.RLock()
	defer rfLedgerMu.RUnlock()
	for name, token := range plan.NodeSnapshots {
		node := nodes[name]
		if node == nil || node.Name != name {
			return ErrVGPUPlanStale
		}
		if _, ok := node.Sharing.(HAMICoreFactory); !ok || vgpuJobSourceToken(node) != token {
			return ErrVGPUPlanStale
		}
	}
	return nil
}
