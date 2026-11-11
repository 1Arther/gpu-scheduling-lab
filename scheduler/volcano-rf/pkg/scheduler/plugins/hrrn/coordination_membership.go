// Copyright 2026 The Volcano Authors. Apache-2.0.
package hrrn

import (
	"context"
	"reflect"
	"strconv"
	"time"

	v1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	batch "volcano.sh/apis/pkg/apis/batch/v1alpha1"
	scheduling "volcano.sh/apis/pkg/apis/scheduling/v1beta1"
	vcclient "volcano.sh/apis/pkg/client/clientset/versioned"
	"volcano.sh/volcano/pkg/scheduler/api"
	"volcano.sh/volcano/pkg/scheduler/api/devices/nvidia/vgpu"
)

const singletonJobReadTimeout = 2 * time.Second

// singletonJobOwner requires exactly one owner reference and it must be a
// VolcanoJob controller with an unambiguous name and UID. Auxiliary, duplicate,
// non-controller, foreign-kind and malformed owner references fail closed.
func singletonJobOwner(refs []metav1.OwnerReference) (metav1.OwnerReference, bool) {
	if len(refs) != 1 {
		return metav1.OwnerReference{}, false
	}
	ref := refs[0]
	if ref.APIVersion != batch.SchemeGroupVersion.String() || ref.Kind != "Job" ||
		ref.Name == "" || ref.UID == "" || ref.Controller == nil || !*ref.Controller {
		return metav1.OwnerReference{}, false
	}
	return ref, true
}

func nilClientObject(value interface{}) bool {
	if value == nil {
		return true
	}
	r := reflect.ValueOf(value)
	switch r.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map, reflect.Ptr, reflect.Slice:
		return r.IsNil()
	default:
		return false
	}
}

// verifiedVolcanoMembership consults the authoritative VolcanoJob API rather
// than trusting the submitted PodGroup count alone, including when all observed
// Pods are CPU-only. It also rejects multi-Pod Jobs with any vGPU resource in
// a positive-replica task template; it is not a generic membership validator.
// This is not an atomic cross-object snapshot: owner refs,
// Job spec and other Pods may change after the GET. A production deployment
// must separately enforce RBAC/admission preventing forged ownership, extra
// members and Job mutation between this observation and binding. Standalone
// PodGroups fail closed. The standard client-go REST transport honors the
// context deadline; clients that ignore contexts cannot be bounded here.
func verifiedVolcanoMembership(ctx context.Context, job *api.JobInfo, client vcclient.Interface) bool {
	if ctx == nil || ctx.Err() != nil || job == nil || job.PodGroup == nil || nilClientObject(client) {
		return false
	}
	pg := job.PodGroup
	declared, err := strconv.Atoi(pg.Annotations[TotalPodsAnnotation])
	if err != nil || declared < 1 || pg.UID == "" || pg.Name == "" || pg.Namespace == "" ||
		pg.DeletionTimestamp != nil || pg.Spec.MinMember < 1 || int64(pg.Spec.MinMember) > int64(declared) ||
		job.MinAvailable != pg.Spec.MinMember ||
		job.PgUID != pg.UID || job.Namespace != pg.Namespace || job.Name != pg.Name ||
		job.UID != api.JobID(pg.Namespace+"/"+pg.Name) || len(job.Tasks) != declared {
		return false
	}
	pgOwner, ok := singletonJobOwner(pg.OwnerReferences)
	if !ok {
		return false
	}
	seenNames := make(map[string]bool, len(job.Tasks))
	for uid, candidate := range job.Tasks {
		if candidate == nil || candidate.Pod == nil || candidate.UID != uid ||
			candidate.Pod.UID == "" || api.TaskID(candidate.Pod.UID) != uid ||
			candidate.Pod.Namespace != pg.Namespace || candidate.Namespace != pg.Namespace ||
			candidate.Pod.Name == "" || candidate.Name != candidate.Pod.Name ||
			candidate.Job != job.UID || candidate.Pod.DeletionTimestamp != nil ||
			candidate.Pod.Annotations[scheduling.KubeGroupNameAnnotationKey] != pg.Name || seenNames[candidate.Pod.Name] {
			return false
		}
		seenNames[candidate.Pod.Name] = true
		podOwner, valid := singletonJobOwner(candidate.Pod.OwnerReferences)
		if !valid || podOwner.APIVersion != pgOwner.APIVersion || podOwner.Kind != pgOwner.Kind ||
			podOwner.Name != pgOwner.Name || podOwner.UID != pgOwner.UID {
			return false
		}
	}
	readCtx, cancel := context.WithTimeout(ctx, singletonJobReadTimeout)
	defer cancel()
	batchClient := client.BatchV1alpha1()
	if nilClientObject(batchClient) {
		return false
	}
	jobs := batchClient.Jobs(pg.Namespace)
	if nilClientObject(jobs) {
		return false
	}
	volcanoJob, err := jobs.Get(readCtx, pgOwner.Name, metav1.GetOptions{})
	if err != nil || readCtx.Err() != nil || volcanoJob == nil || volcanoJob.Namespace != pg.Namespace ||
		volcanoJob.Name != pgOwner.Name || volcanoJob.UID != pgOwner.UID || volcanoJob.DeletionTimestamp != nil ||
		volcanoJob.Spec.MinAvailable != pg.Spec.MinMember {
		return false
	}
	var replicas int64
	for _, spec := range volcanoJob.Spec.Tasks {
		if spec.Replicas < 0 {
			return false
		}
		// A multi-member Job must remain blocked if any live task template
		// declares vGPU, even when observed Pods appear CPU-only. Otherwise an
		// altered/forged Pod can hide the owner Job's real device demand.
		if declared > 1 && spec.Replicas > 0 && vgpu.HasCoordinatedVGPURequest(&v1.Pod{Spec: spec.Template.Spec}) {
			return false
		}
		replicas += int64(spec.Replicas)
		if replicas > int64(declared) {
			return false
		}
	}
	return replicas == int64(declared)
}

// verifiedVolcanoSingleton provides the stricter one-member convenience check
// for callers which must apply the legacy single-Pod handoff path.
func verifiedVolcanoSingleton(ctx context.Context, job *api.JobInfo, client vcclient.Interface) bool {
	return job != nil && len(job.Tasks) == 1 && verifiedVolcanoMembership(ctx, job, client)
}
