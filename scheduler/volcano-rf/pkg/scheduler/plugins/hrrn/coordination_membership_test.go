// Copyright 2026 The Volcano Authors. Apache-2.0.
package hrrn

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	v1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/rest"
	clienttesting "k8s.io/client-go/testing"
	batch "volcano.sh/apis/pkg/apis/batch/v1alpha1"
	schedulinginternal "volcano.sh/apis/pkg/apis/scheduling"
	scheduling "volcano.sh/apis/pkg/apis/scheduling/v1beta1"
	vcclient "volcano.sh/apis/pkg/client/clientset/versioned"
	vcfake "volcano.sh/apis/pkg/client/clientset/versioned/fake"
	batchclient "volcano.sh/apis/pkg/client/clientset/versioned/typed/batch/v1alpha1"
	"volcano.sh/volcano/pkg/scheduler/api"
	deviceconfig "volcano.sh/volcano/pkg/scheduler/api/devices/config"
	"volcano.sh/volcano/pkg/scheduler/api/devices/nvidia/vgpu"
)

func membershipFixture() (*api.JobInfo, *batch.Job) {
	controller := true
	owner := metav1.OwnerReference{APIVersion: batch.SchemeGroupVersion.String(), Kind: "Job",
		Name: "the-job", UID: types.UID("job-uid"), Controller: &controller}
	pg := &api.PodGroup{PodGroup: schedulinginternal.PodGroup{
		ObjectMeta: metav1.ObjectMeta{Name: "the-pg", Namespace: "ns", UID: "pg-uid",
			Annotations: map[string]string{TotalPodsAnnotation: "1"}, OwnerReferences: []metav1.OwnerReference{owner}},
		Spec: schedulinginternal.PodGroupSpec{MinMember: 1},
	}}
	pod := &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "one-pod", Namespace: "ns", UID: "pod-uid",
		Annotations:     map[string]string{scheduling.KubeGroupNameAnnotationKey: pg.Name},
		OwnerReferences: []metav1.OwnerReference{owner}}, Status: v1.PodStatus{Phase: v1.PodPending}}
	task := api.NewTaskInfo(pod)
	info := api.NewJobInfo(api.JobID("ns/the-pg"), task)
	info.SetPodGroup(pg)
	object := &batch.Job{ObjectMeta: metav1.ObjectMeta{Name: "the-job", Namespace: "ns", UID: "job-uid"},
		Spec: batch.JobSpec{MinAvailable: 1, Tasks: []batch.TaskSpec{{Name: "worker", Replicas: 1}}}}
	return info, object
}

func singletonTask(job *api.JobInfo) *api.TaskInfo {
	for _, task := range job.Tasks {
		return task
	}
	return nil
}

type clientWithoutBatch struct{ vcclient.Interface }

func (clientWithoutBatch) BatchV1alpha1() batchclient.BatchV1alpha1Interface { return nil }

func TestVerifiedVolcanoSingletonRequiresMatchingAuthoritativeOwner(t *testing.T) {
	job, object := membershipFixture()
	client := vcfake.NewSimpleClientset(object)
	if !verifiedVolcanoSingleton(context.Background(), job, client) {
		t.Fatal("correct matching one-member VolcanoJob rejected")
	}
	actions := client.Actions()
	if len(actions) != 1 || actions[0].GetVerb() != "get" || actions[0].GetNamespace() != "ns" ||
		actions[0].(clienttesting.GetAction).GetName() != "the-job" {
		t.Fatalf("expected one direct namespaced Job GET, got %+v", actions)
	}
}

func twoMemberFixture() (*api.JobInfo, *batch.Job) {
	job, object := membershipFixture()
	first := singletonTask(job).Pod
	second := first.DeepCopy()
	second.Name, second.UID = "second-pod", "pod-uid-2"
	job.AddTaskInfo(api.NewTaskInfo(second))
	job.PodGroup.Annotations[TotalPodsAnnotation] = "2"
	object.Spec.Tasks[0].Replicas = 2
	return job, object
}

func TestVerifiedVolcanoMembershipRequiresActualCompleteCPUGroup(t *testing.T) {
	job, object := twoMemberFixture()
	client := vcfake.NewSimpleClientset(object)
	if !verifiedVolcanoMembership(context.Background(), job, client) {
		t.Fatal("fully observed two-member Job with matching owner/spec rejected")
	}
	if verifiedVolcanoSingleton(context.Background(), job, client) {
		t.Fatal("two-member Job accepted by singleton wrapper")
	}
	// CPU-only Pod observed so far, claimed count 1, but the real Job wants 2.
	// An unseen second Pod may request vGPU even though the observed Pod does not.
	job, object = membershipFixture()
	singletonTask(job).Pod.Spec.Containers = []v1.Container{{Name: "cpu-only"}}
	object.Spec.Tasks[0].Replicas = 2
	if verifiedVolcanoMembership(context.Background(), job, vcfake.NewSimpleClientset(object)) {
		t.Fatal("claimed singleton CPU prefix concealed a second member")
	}
}

func TestVerifiedVolcanoMembershipRejectsInconsistentMultiMemberOwners(t *testing.T) {
	cases := []struct {
		name   string
		mutate func(*api.JobInfo, *batch.Job)
	}{
		{"actual-three-despite-observed-two", func(_ *api.JobInfo, v *batch.Job) { v.Spec.Tasks[0].Replicas = 3 }},
		{"actual-one-despite-observed-two", func(_ *api.JobInfo, v *batch.Job) { v.Spec.Tasks[0].Replicas = 1 }},
		{"wrong-second-owner", func(j *api.JobInfo, _ *batch.Job) {
			for _, task := range j.Tasks {
				if task.Name == "second-pod" {
					task.Pod.OwnerReferences[0].UID = "other-job"
				}
			}
		}},
		{"second-missing-owner", func(j *api.JobInfo, _ *batch.Job) {
			for _, task := range j.Tasks {
				if task.Name == "second-pod" {
					task.Pod.OwnerReferences = nil
				}
			}
		}},
		{"duplicate-pod-name", func(j *api.JobInfo, _ *batch.Job) {
			for _, task := range j.Tasks {
				task.Name = "one-pod"
				task.Pod.Name = "one-pod"
			}
		}},
		{"podgroup-minimum-not-job-spec", func(j *api.JobInfo, _ *batch.Job) { j.MinAvailable = 2; j.PodGroup.Spec.MinMember = 2 }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			job, object := twoMemberFixture()
			tc.mutate(job, object)
			if verifiedVolcanoMembership(context.Background(), job, vcfake.NewSimpleClientset(object)) {
				t.Fatal("inconsistent multi-member Job accepted")
			}
		})
	}
}

func TestVerifiedVolcanoMembershipRejectsHiddenTemplateVGPU(t *testing.T) {
	// Global device config is explicitly restored. The check itself reads it in
	// the same way as the scheduler's native vGPU resource-name configuration.
	if deviceconfig.GetConfig() == nil {
		deviceconfig.InitDevicesConfig("stage6-not-installed", "hrrn-test")
	}
	cfg := deviceconfig.GetConfig()
	if cfg == nil {
		t.Fatal("default device configuration unavailable")
	}
	original := cfg.NvidiaConfig
	t.Cleanup(func() { cfg.NvidiaConfig = original })
	cfg.NvidiaConfig.ResourceCountName = "example.org/custom-count"
	cfg.NvidiaConfig.ResourceMemoryName = "example.org/custom-memory"
	cfg.NvidiaConfig.ResourceCoreName = "example.org/custom-cores"
	cfg.NvidiaConfig.ResourceMemoryPercentageName = "example.org/custom-percent"

	cases := []struct {
		name, key, quantity string
		init, request       bool
	}{
		{"default-limit", "volcano.sh/vgpu-number", "1", false, false},
		{"default-request", "volcano.sh/vgpu-memory", "4096", false, true},
		{"custom-count", "example.org/custom-count", "1", false, false},
		{"custom-memory", "example.org/custom-memory", "4096", false, false},
		{"custom-core", "example.org/custom-cores", "40", false, false},
		{"custom-percent", "example.org/custom-percent", "50", false, false},
		{"init-container", "example.org/custom-count", "1", true, false},
		{"zero-valued-key", "example.org/custom-count", "0", false, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			job, object := twoMemberFixture()
			for _, task := range job.Tasks {
				task.Pod.Spec.Containers = []v1.Container{{Name: "observed-cpu"}}
				if vgpu.HasCoordinatedVGPURequest(task.Pod) {
					t.Fatal("fixture must expose CPU-only observed Pods")
				}
			}
			reqs := v1.ResourceRequirements{}
			if tc.request {
				reqs.Requests = v1.ResourceList{v1.ResourceName(tc.key): resource.MustParse(tc.quantity)}
			} else {
				reqs.Limits = v1.ResourceList{v1.ResourceName(tc.key): resource.MustParse(tc.quantity)}
			}
			container := v1.Container{Name: "spec-gpu", Resources: reqs}
			if tc.init {
				object.Spec.Tasks[0].Template.Spec.InitContainers = []v1.Container{container}
			} else {
				object.Spec.Tasks[0].Template.Spec.Containers = []v1.Container{container}
			}
			if verifiedVolcanoMembership(context.Background(), job, vcfake.NewSimpleClientset(object)) {
				t.Fatal("observed CPU Pods concealed real multi-member vGPU template")
			}
		})
	}
	t.Run("second-positive-task-template-uses-gpu", func(t *testing.T) {
		job, object := twoMemberFixture()
		object.Spec.Tasks[0].Replicas = 1
		object.Spec.Tasks = append(object.Spec.Tasks, batch.TaskSpec{Name: "gpu-role", Replicas: 1,
			Template: v1.PodTemplateSpec{Spec: v1.PodSpec{Containers: []v1.Container{{Name: "gpu",
				Resources: v1.ResourceRequirements{Limits: v1.ResourceList{"volcano.sh/vgpu-number": resource.MustParse("1")}}}}}}})
		if verifiedVolcanoMembership(context.Background(), job, vcfake.NewSimpleClientset(object)) {
			t.Fatal("a positive replica of a second GPU task role must fail closed")
		}
	})
	t.Run("zero-replica-gpu-template-is-not-an-active-member", func(t *testing.T) {
		job, object := twoMemberFixture()
		object.Spec.Tasks = append(object.Spec.Tasks, batch.TaskSpec{Name: "zero", Replicas: 0,
			Template: v1.PodTemplateSpec{Spec: v1.PodSpec{Containers: []v1.Container{{Name: "gpu",
				Resources: v1.ResourceRequirements{Limits: v1.ResourceList{"volcano.sh/vgpu-number": resource.MustParse("1")}}}}}}})
		if !verifiedVolcanoMembership(context.Background(), job, vcfake.NewSimpleClientset(object)) {
			t.Fatal("zero-replica task should not hide a complete CPU-only Job")
		}
	})
	t.Run("one-gpu-singleton-remains-eligible", func(t *testing.T) {
		job, object := membershipFixture()
		object.Spec.Tasks[0].Template.Spec.Containers = []v1.Container{{Name: "gpu",
			Resources: v1.ResourceRequirements{Limits: v1.ResourceList{"volcano.sh/vgpu-number": resource.MustParse("1")}}}}
		if !verifiedVolcanoMembership(context.Background(), job, vcfake.NewSimpleClientset(object)) {
			t.Fatal("one-member GPU Job should remain verifiable")
		}
	})
}

func TestVerifiedVolcanoSingletonRejectsUnverifiedOrInconsistentInput(t *testing.T) {
	now := metav1.Now()
	noController := false
	cases := []struct {
		name   string
		mutate func(*api.JobInfo, *batch.Job)
	}{
		{"no-podgroup-owner", func(j *api.JobInfo, _ *batch.Job) { j.PodGroup.OwnerReferences = nil }},
		{"multiple-podgroup-owners", func(j *api.JobInfo, _ *batch.Job) {
			j.PodGroup.OwnerReferences = append(j.PodGroup.OwnerReferences, j.PodGroup.OwnerReferences[0])
		}},
		{"no-pod-owner", func(j *api.JobInfo, _ *batch.Job) { singletonTask(j).Pod.OwnerReferences = nil }},
		{"multiple-pod-owners", func(j *api.JobInfo, _ *batch.Job) {
			p := singletonTask(j).Pod
			p.OwnerReferences = append(p.OwnerReferences, p.OwnerReferences[0])
		}},
		{"non-controller-owner", func(j *api.JobInfo, _ *batch.Job) { j.PodGroup.OwnerReferences[0].Controller = &noController }},
		{"nil-controller-owner", func(j *api.JobInfo, _ *batch.Job) { j.PodGroup.OwnerReferences[0].Controller = nil }},
		{"wrong-owner-api", func(j *api.JobInfo, _ *batch.Job) { j.PodGroup.OwnerReferences[0].APIVersion = "batch/v1" }},
		{"wrong-owner-kind", func(j *api.JobInfo, _ *batch.Job) { j.PodGroup.OwnerReferences[0].Kind = "CronJob" }},
		{"empty-owner-uid", func(j *api.JobInfo, _ *batch.Job) { j.PodGroup.OwnerReferences[0].UID = "" }},
		{"empty-owner-name", func(j *api.JobInfo, _ *batch.Job) { j.PodGroup.OwnerReferences[0].Name = "" }},
		{"pod-owner-different-uid", func(j *api.JobInfo, _ *batch.Job) { singletonTask(j).Pod.OwnerReferences[0].UID = "other" }},
		{"pod-owner-different-name", func(j *api.JobInfo, _ *batch.Job) { singletonTask(j).Pod.OwnerReferences[0].Name = "other" }},
		{"job-recreated-uid", func(_ *api.JobInfo, o *batch.Job) { o.UID = "new-job-uid" }},
		{"job-different-namespace", func(_ *api.JobInfo, o *batch.Job) { o.Namespace = "other" }},
		{"job-deleting", func(_ *api.JobInfo, o *batch.Job) { o.DeletionTimestamp = &now }},
		{"podgroup-deleting", func(j *api.JobInfo, _ *batch.Job) { j.PodGroup.DeletionTimestamp = &now }},
		{"pod-deleting", func(j *api.JobInfo, _ *batch.Job) { singletonTask(j).Pod.DeletionTimestamp = &now }},
		{"pg-uid-empty", func(j *api.JobInfo, _ *batch.Job) { j.PodGroup.UID = "" }},
		{"pg-uid-changed", func(j *api.JobInfo, _ *batch.Job) { j.PodGroup.UID = "replacement" }},
		{"jobinfo-namespace-mismatch", func(j *api.JobInfo, _ *batch.Job) { j.Namespace = "other" }},
		{"jobinfo-name-mismatch", func(j *api.JobInfo, _ *batch.Job) { j.Name = "other" }},
		{"jobinfo-uid-mismatch", func(j *api.JobInfo, _ *batch.Job) { j.UID = "other/pg" }},
		{"task-job-mismatch", func(j *api.JobInfo, _ *batch.Job) { singletonTask(j).Job = "other/pg" }},
		{"podgroup-membership-mismatch", func(j *api.JobInfo, _ *batch.Job) {
			singletonTask(j).Pod.Annotations[scheduling.KubeGroupNameAnnotationKey] = "other"
		}},
		{"task-uid-mismatch", func(j *api.JobInfo, _ *batch.Job) { singletonTask(j).Pod.UID = "replacement" }},
		{"task-namespace-mismatch", func(j *api.JobInfo, _ *batch.Job) { singletonTask(j).Pod.Namespace = "other" }},
		{"missing-declaration", func(j *api.JobInfo, _ *batch.Job) { delete(j.PodGroup.Annotations, TotalPodsAnnotation) }},
		{"bad-declaration", func(j *api.JobInfo, _ *batch.Job) { j.PodGroup.Annotations[TotalPodsAnnotation] = "bad" }},
		{"multi-declaration", func(j *api.JobInfo, _ *batch.Job) { j.PodGroup.Annotations[TotalPodsAnnotation] = "2" }},
		{"min-member-zero", func(j *api.JobInfo, _ *batch.Job) { j.MinAvailable = 0; j.PodGroup.Spec.MinMember = 0 }},
		{"min-member-two", func(j *api.JobInfo, _ *batch.Job) { j.MinAvailable = 2; j.PodGroup.Spec.MinMember = 2 }},
		{"job-invalid-minimum", func(_ *api.JobInfo, o *batch.Job) { o.Spec.MinAvailable = 2 }},
		{"job-empty-spec", func(_ *api.JobInfo, o *batch.Job) { o.Spec.Tasks = nil }},
		{"job-zero-replicas", func(_ *api.JobInfo, o *batch.Job) { o.Spec.Tasks[0].Replicas = 0 }},
		{"job-negative-replicas", func(_ *api.JobInfo, o *batch.Job) { o.Spec.Tasks[0].Replicas = -1 }},
		// This is the key hazard: a stale/misleading declaration says one,
		// while the authoritative Job has a second unobserved member.
		{"declares-one-actual-two", func(_ *api.JobInfo, o *batch.Job) { o.Spec.Tasks[0].Replicas = 2 }},
		{"two-task-specs", func(_ *api.JobInfo, o *batch.Job) {
			o.Spec.Tasks = append(o.Spec.Tasks, batch.TaskSpec{Name: "other", Replicas: 1})
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			job, object := membershipFixture()
			tc.mutate(job, object)
			if verifiedVolcanoSingleton(context.Background(), job, vcfake.NewSimpleClientset(object)) {
				t.Fatal("unverified or inconsistent identity/membership accepted")
			}
		})
	}
}

func TestVerifiedVolcanoSingletonAPIErrorMissingAndContext(t *testing.T) {
	job, object := membershipFixture()
	if verifiedVolcanoSingleton(nil, job, vcfake.NewSimpleClientset(object)) ||
		verifiedVolcanoSingleton(context.Background(), nil, vcfake.NewSimpleClientset(object)) ||
		verifiedVolcanoSingleton(context.Background(), job, nil) {
		t.Fatal("nil context/job/client accepted")
	}
	var typedNil *vcfake.Clientset
	if verifiedVolcanoSingleton(context.Background(), job, typedNil) {
		t.Fatal("typed nil client accepted")
	}
	if verifiedVolcanoSingleton(context.Background(), job, clientWithoutBatch{vcfake.NewSimpleClientset(object)}) {
		t.Fatal("nil batch subclient accepted")
	}
	if verifiedVolcanoSingleton(context.Background(), job, vcfake.NewSimpleClientset()) {
		t.Fatal("missing Job accepted")
	}
	forbidden := vcfake.NewSimpleClientset(object)
	forbidden.PrependReactor("get", "jobs", func(clienttesting.Action) (bool, runtime.Object, error) {
		return true, nil, apierrors.NewForbidden(schema.GroupResource{Group: "batch.volcano.sh", Resource: "jobs"}, "the-job", nil)
	})
	if verifiedVolcanoSingleton(context.Background(), job, forbidden) {
		t.Fatal("API access failure accepted")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if verifiedVolcanoSingleton(ctx, job, vcfake.NewSimpleClientset(object)) {
		t.Fatal("cancelled context accepted")
	}
}

func TestVerifiedVolcanoSingletonHonorsRESTDeadline(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) {
		<-r.Context().Done()
	}))
	defer server.Close()
	client, err := vcclient.NewForConfig(&rest.Config{Host: server.URL})
	if err != nil {
		t.Fatal(err)
	}
	job, _ := membershipFixture()
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Millisecond)
	defer cancel()
	start := time.Now()
	if verifiedVolcanoSingleton(ctx, job, client) {
		t.Fatal("timed out Job read accepted")
	}
	if time.Since(start) > time.Second {
		t.Fatal("REST request did not respect the caller deadline")
	}
}
