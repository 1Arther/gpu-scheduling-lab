package uidgate

import (
	"context"
	"errors"
	"testing"
	"time"

	v1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes/fake"
)

type localObservationSource struct {
	snapshot CheckpointSnapshot
	err      error
	onRead   func()
}

func (s *localObservationSource) ReadCheckpoint(context.Context) (CheckpointSnapshot, error) {
	if s.onRead != nil {
		s.onRead()
	}
	return s.snapshot, s.err
}

func localObservationFixture(t *testing.T) (*LocalCheckpointObserver, *fake.Clientset, *localObservationSource, Claim, ExpectedPodSnapshot) {
	t.Helper()
	now := time.Unix(1770000000, 0)
	c := claim()
	controller := true
	pod := &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "pod-a", Namespace: "test", UID: types.UID(c.PodUID), ResourceVersion: "pod-rv-1", OwnerReferences: []metav1.OwnerReference{{UID: types.UID(c.JobUID), Controller: &controller}}},
		Spec:   v1.PodSpec{NodeName: c.Node, Containers: []v1.Container{{Name: c.Container, Resources: v1.ResourceRequirements{Limits: v1.ResourceList{v1.ResourceName(c.Resource): resource.MustParse("1"), v1.ResourceName("volcano.sh/vgpu-memory"): resource.MustParse("4096"), v1.ResourceName("volcano.sh/vgpu-cores"): resource.MustParse("40")}}}}},
		Status: v1.PodStatus{Phase: v1.PodPending, Conditions: []v1.PodCondition{{Type: v1.PodScheduled, Status: v1.ConditionTrue}}}}
	node := &v1.Node{ObjectMeta: metav1.ObjectMeta{Name: c.Node, UID: "node-uid", ResourceVersion: "node-rv-1"}, Status: v1.NodeStatus{Conditions: []v1.NodeCondition{{Type: v1.NodeReady, Status: v1.ConditionTrue}}}}
	client := fake.NewSimpleClientset(pod, node)
	source := &localObservationSource{snapshot: CheckpointSnapshot{Bytes: checkpoint(t, entry(c.PodUID, c.Container, []string{"slot-1"}, baseResponse())), ModTime: now.Add(-time.Second)}}
	observer := &LocalCheckpointObserver{Client: client, Checkpoint: source, Now: func() time.Time { return now }, MaxFileAge: time.Minute}
	expected := ExpectedPodSnapshot{Namespace: pod.Namespace, Name: pod.Name, ResourceVersion: pod.ResourceVersion, NodeUID: node.UID, ContainerResources: pod.Spec.Containers[0].Resources.DeepCopy()}
	return observer, client, source, c, expected
}

func TestLocalObservationChecksStablePodNodeAndCheckpointButIsNotAck(t *testing.T) {
	o, _, source, c, expected := localObservationFixture(t)
	obs, err := o.Observe(context.Background(), c, expected)
	if err != nil {
		t.Fatal(err)
	}
	if obs.PodUID != c.PodUID || obs.PodResourceVersion != expected.ResourceVersion || obs.NodeUID != string(expected.NodeUID) ||
		obs.NodeResourceVersion != "node-rv-1" || obs.CheckpointSHA256 == "" || len(obs.RawCheckpoint) == 0 {
		t.Fatalf("unexpected observation %+v", obs)
	}
	if _, ok := any(o).(NodeObserver); ok {
		t.Fatal("diagnostic observer must not satisfy trusted NodeObserver")
	}
	obs.RawCheckpoint[0] ^= 1
	if obs.RawCheckpoint[0] == source.snapshot.Bytes[0] {
		t.Fatal("observation aliases source bytes")
	}
}

func TestLocalObservationRejectsStaleFutureMismatchAndAbsentEvidence(t *testing.T) {
	for _, name := range []string{"stale", "future", "missing", "bad-checkpoint", "wrong-quota", "expected-rv", "uid", "node-uid", "reader-error", "cancelled"} {
		t.Run(name, func(t *testing.T) {
			o, _, source, c, expected := localObservationFixture(t)
			ctx := context.Background()
			switch name {
			case "stale":
				source.snapshot.ModTime = o.Now().Add(-2 * time.Minute)
			case "future":
				source.snapshot.ModTime = o.Now().Add(time.Second)
			case "missing":
				source.snapshot.Bytes = nil
			case "bad-checkpoint":
				source.snapshot.Bytes = []byte("bad")
			case "wrong-quota":
				c.Allocations[0].Resources.Cores++
			case "expected-rv":
				expected.ResourceVersion = ""
			case "uid":
				c.PodUID = "replaced"
			case "node-uid":
				expected.NodeUID = "replacement"
			case "reader-error":
				source.err = errors.New("source unavailable")
			case "cancelled":
				var cancel context.CancelFunc
				ctx, cancel = context.WithCancel(ctx)
				cancel()
			}
			if _, err := o.Observe(ctx, c, expected); err == nil {
				t.Fatalf("%s was accepted", name)
			}
		})
	}
}

func TestLocalObservationRejectsPodAndNodeChangesDuringRead(t *testing.T) {
	for _, name := range []string{"pod-recreated", "pod-rv", "node-recreated", "node-rv", "pod-deleted"} {
		t.Run(name, func(t *testing.T) {
			o, client, source, c, expected := localObservationFixture(t)
			source.onRead = func() {
				ctx := context.Background()
				if name == "pod-deleted" {
					if err := client.CoreV1().Pods(expected.Namespace).Delete(ctx, expected.Name, metav1.DeleteOptions{}); err != nil {
						t.Fatal(err)
					}
					return
				}
				if name == "pod-recreated" || name == "pod-rv" {
					p, err := client.CoreV1().Pods(expected.Namespace).Get(ctx, expected.Name, metav1.GetOptions{})
					if err != nil {
						t.Fatal(err)
					}
					if name == "pod-recreated" {
						p.UID = "new-uid"
					} else {
						p.ResourceVersion = "pod-rv-2"
					}
					if _, err = client.CoreV1().Pods(expected.Namespace).Update(ctx, p, metav1.UpdateOptions{}); err != nil {
						t.Fatal(err)
					}
					return
				}
				n, err := client.CoreV1().Nodes().Get(ctx, c.Node, metav1.GetOptions{})
				if err != nil {
					t.Fatal(err)
				}
				if name == "node-recreated" {
					n.UID = "new-node-uid"
				} else {
					n.ResourceVersion = "node-rv-2"
				}
				if _, err = client.CoreV1().Nodes().Update(ctx, n, metav1.UpdateOptions{}); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := o.Observe(context.Background(), c, expected); err == nil {
				t.Fatalf("%s during read was accepted", name)
			}
		})
	}
}

func TestLocalObservationRejectsUnsafePodAndNodeState(t *testing.T) {
	for _, name := range []string{"wrong-owner", "unscheduled", "terminated", "wrong-resource", "wrong-memory", "wrong-cores", "unready-node", "wrong-node"} {
		t.Run(name, func(t *testing.T) {
			o, client, _, c, expected := localObservationFixture(t)
			ctx := context.Background()
			p, _ := client.CoreV1().Pods(expected.Namespace).Get(ctx, expected.Name, metav1.GetOptions{})
			n, _ := client.CoreV1().Nodes().Get(ctx, c.Node, metav1.GetOptions{})
			switch name {
			case "wrong-owner":
				p.OwnerReferences[0].UID = "wrong"
			case "unscheduled":
				p.Status.Conditions = nil
			case "terminated":
				p.Status.ContainerStatuses = []v1.ContainerStatus{{Name: c.Container, State: v1.ContainerState{Terminated: &v1.ContainerStateTerminated{}}}}
			case "wrong-resource":
				p.Spec.Containers[0].Resources.Limits[v1.ResourceName(c.Resource)] = resource.MustParse("2")
			case "wrong-memory":
				p.Spec.Containers[0].Resources.Limits[v1.ResourceName("volcano.sh/vgpu-memory")] = resource.MustParse("4097")
			case "wrong-cores":
				p.Spec.Containers[0].Resources.Limits[v1.ResourceName("volcano.sh/vgpu-cores")] = resource.MustParse("41")
			case "unready-node":
				n.Status.Conditions[0].Status = v1.ConditionFalse
			case "wrong-node":
				p.Spec.NodeName = "wrong"
			}
			if _, err := client.CoreV1().Pods(expected.Namespace).Update(ctx, p, metav1.UpdateOptions{}); err != nil {
				t.Fatal(err)
			}
			if _, err := client.CoreV1().Nodes().Update(ctx, n, metav1.UpdateOptions{}); err != nil {
				t.Fatal(err)
			}
			if _, err := o.Observe(ctx, c, expected); err == nil {
				t.Fatalf("%s was accepted", name)
			}
		})
	}
}
