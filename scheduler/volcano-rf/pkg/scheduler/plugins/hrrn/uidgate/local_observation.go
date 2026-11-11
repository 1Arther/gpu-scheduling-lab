package uidgate

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"reflect"
	"time"

	v1 "k8s.io/api/core/v1"
	apiequality "k8s.io/apimachinery/pkg/api/equality"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes"
	"volcano.sh/volcano/pkg/scheduler/plugins/hrrn/coordination"
)

// CheckpointSnapshot is a local filesystem snapshot. Its timestamp and kubelet
// checksum do not authenticate the source or prove that a Pod is still live.
type CheckpointSnapshot struct {
	Bytes   []byte
	ModTime time.Time
}

// CheckpointSource permits a local secure reader or a test fixture. Callers
// must independently trust the source, node mount, and kernel credentials.
type CheckpointSource interface {
	ReadCheckpoint(context.Context) (CheckpointSnapshot, error)
}

type ExpectedPodSnapshot struct {
	Namespace, Name, ResourceVersion string
	NodeUID                          types.UID
	// Must be copied from the immutable, independently verified Pod plan. This
	// checks all request/limit dimensions, including custom GPU resource names.
	ContainerResources *v1.ResourceRequirements
}

// LocalObservation is deliberately NOT a NodeObserver and cannot be passed to
// Gate.CheckPreStart. It collects and cross-checks local evidence, but a file
// modification time, Pod GET, Node GET, or kubelet checkpoint alone does not
// establish fresh device use, exclusive device ownership, old RPC fencing,
// or that the checkpoint source is the actual kubelet on the named node.
type LocalObservation struct {
	PodUID, PodResourceVersion, NodeUID, NodeResourceVersion string
	CheckpointSHA256                                         string
	CheckpointModTime, ObservedAt                            time.Time
	RawCheckpoint                                            []byte
}

// LocalCheckpointObserver combines a bounded local file read with live Pod
// and Node GETs. Client authentication, RBAC, the node filesystem/mount, and
// time source are caller responsibilities. It provides useful negative checks
// but MUST NOT be treated as an authenticated NodeObserver implementation.
type LocalCheckpointObserver struct {
	Client     kubernetes.Interface
	Checkpoint CheckpointSource
	Now        func() time.Time
	MaxFileAge time.Duration
}

func observationNil(value interface{}) bool {
	if value == nil {
		return true
	}
	v := reflect.ValueOf(value)
	switch v.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map, reflect.Ptr, reflect.Slice:
		return v.IsNil()
	default:
		return false
	}
}

func observationPod(p *v1.Pod, claim Claim, want ExpectedPodSnapshot) error {
	if p == nil || p.Namespace != want.Namespace || p.Name != want.Name || string(p.UID) != claim.PodUID ||
		p.ResourceVersion != want.ResourceVersion || p.ResourceVersion == "" || p.Spec.NodeName != claim.Node || p.DeletionTimestamp != nil ||
		(p.Status.Phase != v1.PodPending && p.Status.Phase != v1.PodRunning) {
		return fmt.Errorf("Pod identity, version, node or active phase mismatch")
	}
	owners := 0
	for _, owner := range p.OwnerReferences {
		if owner.Controller != nil && *owner.Controller {
			owners++
			if string(owner.UID) != claim.JobUID {
				return fmt.Errorf("Pod controller UID mismatch")
			}
		}
	}
	if owners != 1 {
		return fmt.Errorf("Pod must have exactly one expected controller")
	}
	scheduled := 0
	for _, cond := range p.Status.Conditions {
		if cond.Type == v1.PodScheduled {
			scheduled++
			if cond.Status != v1.ConditionTrue {
				return fmt.Errorf("PodScheduled condition is not true")
			}
		}
	}
	if scheduled != 1 {
		return fmt.Errorf("PodScheduled condition missing or duplicated")
	}
	found := 0
	for _, c := range p.Spec.Containers {
		if c.Name != claim.Container {
			continue
		}
		found++
		if want.ContainerResources == nil || !apiequality.Semantic.DeepEqual(c.Resources, *want.ContainerResources) {
			return fmt.Errorf("Pod container resources changed from snapshot")
		}
		limit, ok := c.Resources.Limits[v1.ResourceName(claim.Resource)]
		count, exact := limit.AsInt64()
		if !ok || !exact || count != int64(len(claim.Allocations)) {
			return fmt.Errorf("Pod resource count mismatch")
		}
	}
	if found != 1 {
		return fmt.Errorf("expected application container missing or duplicated")
	}
	for _, c := range p.Status.ContainerStatuses {
		if c.Name == claim.Container && c.State.Terminated != nil {
			return fmt.Errorf("container is terminated")
		}
	}
	return nil
}

func observationNode(n *v1.Node, name string, uid types.UID) error {
	if n == nil || n.Name != name || n.UID != uid || n.ResourceVersion == "" || n.DeletionTimestamp != nil {
		return fmt.Errorf("Node identity/version mismatch")
	}
	ready := 0
	for _, c := range n.Status.Conditions {
		if c.Type == v1.NodeReady {
			ready++
			if c.Status != v1.ConditionTrue {
				return fmt.Errorf("Node not ready")
			}
		}
	}
	if ready != 1 {
		return fmt.Errorf("NodeReady condition missing or duplicated")
	}
	return nil
}

// Observe verifies the exact claimed Pod before and after a local checkpoint
// read, requiring unchanged API resourceVersions and the expected node UID.
// File mtime must be recent, but a kubelet rewrite can refresh an old entry,
// so this timestamp does not prove the contained entry is fresh. This routine
// is read-only, does not release locks or reservations, and deliberately does
// not implement the NodeObserver trust contract.
func (o *LocalCheckpointObserver) Observe(ctx context.Context, claim Claim, expected ExpectedPodSnapshot) (LocalObservation, error) {
	var zero LocalObservation
	if ctx == nil || o == nil || observationNil(o.Client) || observationNil(o.Checkpoint) || o.Now == nil ||
		o.MaxFileAge <= 0 || o.MaxFileAge > 5*time.Minute || !validClaim(claim) || expected.Namespace == "" || expected.Name == "" ||
		expected.ResourceVersion == "" || expected.NodeUID == "" || expected.ContainerResources == nil {
		return zero, fmt.Errorf("incomplete local observation configuration")
	}
	// The caller must not mutate its input concurrently while it is copied.
	claim.Allocations = append([]coordination.Allocation(nil), claim.Allocations...)
	expected.ContainerResources = expected.ContainerResources.DeepCopy()
	before, err := o.Client.CoreV1().Pods(expected.Namespace).Get(ctx, expected.Name, metav1.GetOptions{})
	if err != nil {
		return zero, err
	}
	if err = observationPod(before, claim, expected); err != nil {
		return zero, err
	}
	node, err := o.Client.CoreV1().Nodes().Get(ctx, claim.Node, metav1.GetOptions{})
	if err != nil {
		return zero, err
	}
	if err = observationNode(node, claim.Node, expected.NodeUID); err != nil {
		return zero, err
	}
	snapshot, err := o.Checkpoint.ReadCheckpoint(ctx)
	if err != nil {
		return zero, err
	}
	if len(snapshot.Bytes) == 0 || len(snapshot.Bytes) > 32<<20 {
		return zero, fmt.Errorf("checkpoint is empty or too large")
	}
	snapshot.Bytes = append([]byte(nil), snapshot.Bytes...)
	now := o.Now()
	if now.IsZero() || snapshot.ModTime.IsZero() || snapshot.ModTime.After(now) || now.Sub(snapshot.ModTime) > o.MaxFileAge {
		return zero, fmt.Errorf("checkpoint is stale, future or too large")
	}
	receipt, err := coordination.InspectKubeletCheckpoint(snapshot.Bytes, claim.PodUID, claim.Container, claim.Resource)
	if err != nil {
		return zero, err
	}
	if err = receipt.Matches(claim.Allocations); err != nil {
		return zero, err
	}
	after, err := o.Client.CoreV1().Pods(expected.Namespace).Get(ctx, expected.Name, metav1.GetOptions{})
	if err != nil {
		return zero, err
	}
	if err = observationPod(after, claim, expected); err != nil {
		return zero, err
	}
	nodeAfter, err := o.Client.CoreV1().Nodes().Get(ctx, claim.Node, metav1.GetOptions{})
	if err != nil {
		return zero, err
	}
	if err = observationNode(nodeAfter, claim.Node, expected.NodeUID); err != nil {
		return zero, err
	}
	if nodeAfter.ResourceVersion != node.ResourceVersion {
		return zero, fmt.Errorf("Node changed while reading checkpoint")
	}
	if err := ctx.Err(); err != nil {
		return zero, err
	}
	digest := sha256.Sum256(snapshot.Bytes)
	return LocalObservation{PodUID: claim.PodUID, PodResourceVersion: before.ResourceVersion,
		NodeUID: string(node.UID), NodeResourceVersion: node.ResourceVersion,
		CheckpointSHA256: hex.EncodeToString(digest[:]), CheckpointModTime: snapshot.ModTime,
		ObservedAt: now, RawCheckpoint: append([]byte(nil), snapshot.Bytes...)}, nil
}
