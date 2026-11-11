// Copyright 2026 The Volcano Authors. Apache-2.0.
package hrrn

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	v1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	clienttesting "k8s.io/client-go/testing"
	batchv1 "volcano.sh/apis/pkg/apis/batch/v1alpha1"
	schedulingv1 "volcano.sh/apis/pkg/apis/scheduling/v1beta1"
	vcfake "volcano.sh/apis/pkg/client/clientset/versioned/fake"
	"volcano.sh/volcano/pkg/scheduler/api"
	devicesconfig "volcano.sh/volcano/pkg/scheduler/api/devices/config"
	"volcano.sh/volcano/pkg/scheduler/api/devices/nvidia/vgpu"
	"volcano.sh/volcano/pkg/scheduler/conf"
	"volcano.sh/volcano/pkg/scheduler/framework"
	"volcano.sh/volcano/pkg/scheduler/plugins/binpack"
	"volcano.sh/volcano/pkg/scheduler/plugins/conformance"
	"volcano.sh/volcano/pkg/scheduler/plugins/deviceshare"
	"volcano.sh/volcano/pkg/scheduler/plugins/drf"
	"volcano.sh/volcano/pkg/scheduler/plugins/gang"
	"volcano.sh/volcano/pkg/scheduler/plugins/nodeorder"
	"volcano.sh/volcano/pkg/scheduler/plugins/overcommit"
	"volcano.sh/volcano/pkg/scheduler/plugins/predicates"
	"volcano.sh/volcano/pkg/scheduler/plugins/priority"
	"volcano.sh/volcano/pkg/scheduler/plugins/proportion"
	"volcano.sh/volcano/pkg/scheduler/plugins/util/nodelock"
	"volcano.sh/volcano/pkg/scheduler/uthelper"
	"volcano.sh/volcano/pkg/scheduler/util"
)

// This fixture enables the new opt-in before the real Session opens. The
// actual NodeLock is enabled before actions, exactly as in the frozen Stage5
// diagnostic; a fake Kubernetes API supplies the Node and Pod objects. The
// frozen RF service must be available, and the fake binder is not a kubelet or
// a Device Plugin. None of these tests claims atomic cross-Pod binding.
func setupCoordinatedStage6(t *testing.T, groups []*schedulingv1.PodGroup, pods []*v1.Pod, tracker *Tracker, now func() time.Time, flags ...interface{}) *fixture {
	t.Helper()
	var enabled, complete, coordinated interface{} = true, true, true
	duplicate := ""
	if len(flags) != 0 {
		if len(flags) != 3 && len(flags) != 4 {
			t.Fatal("flags must supply enabled, complete, coordinated and optionally duplicate order")
		}
		enabled, complete, coordinated = flags[0], flags[1], flags[2]
		if len(flags) == 4 {
			var ok bool
			duplicate, ok = flags[3].(string)
			if !ok || (duplicate != "true-first" && duplicate != "false-first") {
				t.Fatal("invalid duplicate fixture")
			}
		}
	}
	f := &fixture{}
	dsargs := framework.Arguments{
		"deviceshare.VGPUEnable": true, "deviceshare.GPUSelectPolicy": vgpu.RFJobPolicy,
		"deviceshare.RFEnable": true, "deviceshare.RFEndpoint": os.Getenv("RF_LIVE_ENDPOINT"),
		"deviceshare.RFModelVersion": "rf-v4-known-states-ee35cfabe30f", "deviceshare.RFTimeoutMS": 1000,
		"deviceshare.ScheduleWeight": 1, "deviceshare.NodeLockEnable": false,
	}
	op := func(name string, args framework.Arguments) conf.PluginOption {
		return conf.PluginOption{Name: name, Arguments: args, EnabledJobOrder: ptr(true), EnabledJobReady: ptr(true),
			EnabledJobPipelined: ptr(true), EnabledTaskOrder: ptr(true), EnabledQueueOrder: ptr(true),
			EnabledPredicate: ptr(true), EnabledNodeOrder: ptr(true), EnabledOverused: ptr(true), EnabledAllocatable: ptr(true)}
	}
	f.tiers = []conf.Tier{
		{Plugins: []conf.PluginOption{op("priority", nil), op("gang", nil), op("conformance", nil)}},
		{Plugins: []conf.PluginOption{op("overcommit", nil), op("drf", nil),
			op("predicates", framework.Arguments{"predicate.CacheEnable": false}), op("proportion", nil),
			op("nodeorder", nil), op("binpack", nil), op("deviceshare", dsargs),
			op(PluginName, framework.Arguments{"enabled": enabled, "complete-job": complete, "coordinated-complete-job": coordinated})}},
	}
	if duplicate != "" {
		first, second := true, false
		if duplicate == "false-first" {
			first, second = false, true
		}
		plugins := &f.tiers[1].Plugins
		(*plugins)[len(*plugins)-1] = op(PluginName, framework.Arguments{"enabled": true, "complete-job": true, "coordinated-complete-job": first})
		*plugins = append(*plugins, op(PluginName, framework.Arguments{"enabled": true, "complete-job": true, "coordinated-complete-job": second}))
	}
	builders := map[string]framework.PluginBuilder{
		"priority": priority.New, "gang": gang.New, "drf": drf.New, "proportion": proportion.New,
		"predicates": predicates.New, "deviceshare": deviceshare.New, "nodeorder": nodeorder.New,
		"binpack": binpack.New, "overcommit": overcommit.New, "conformance": conformance.New,
		PluginName: func(a framework.Arguments) framework.Plugin {
			f.plugin = NewWithClock(a, now, tracker)
			return f.plugin
		},
	}
	node := util.BuildNode("node", api.BuildResourceList("8", "32Gi", api.ScalarResource{Name: "pods", Value: "100"}), nil)
	f.test = uthelper.TestCommonStruct{Name: t.Name(), Pods: pods, PodGroups: groups, Nodes: []*v1.Node{node},
		Queues: []*schedulingv1.Queue{util.BuildQueue("q", 1, nil)}, Plugins: builders}
	f.ssn = f.test.RegisterSession(f.tiers, []conf.Configuration{{Name: "allocate", Arguments: map[string]interface{}{conf.EnablePredicateErrCacheKey: false}}})
	for _, p := range pods {
		if _, err := f.ssn.KubeClient().CoreV1().Pods(p.Namespace).Create(context.Background(), p.DeepCopy(), metav1.CreateOptions{}); err != nil {
			t.Fatal(err)
		}
	}
	f.gpus = &vgpu.GPUDevices{Name: "node", Mode: "hami-core", Device: map[int]*vgpu.GPUDevice{}, Sharing: vgpu.HAMICoreFactory{}}
	g := vgpu.NewGPUDevice(0, 23552)
	g.UUID, g.Node, g.Type, g.Number, g.Health = "SIM-HRRN-GPU-0", "node", "NVIDIA GeForce RTX 4090", 10, true
	f.gpus.Device[0] = g
	f.ssn.Nodes["node"].Others[vgpu.DeviceName] = f.gpus
	apiNode := node.DeepCopy()
	apiNode.Annotations = map[string]string{}
	if _, err := f.ssn.KubeClient().CoreV1().Nodes().Create(context.Background(), apiNode, metav1.CreateOptions{}); err != nil {
		t.Fatal(err)
	}
	return f
}

// Install a matching, authoritative fake VolcanoJob AFTER Session creation.
// Controller ownership is attached to the cached PG/Pods and fake Pod API,
// while the real guard performs its normal VCClient Job GET during the action.
func addStage6VolcanoJob(t *testing.T, f *fixture, group string, replicas int32) *batchv1.Job {
	t.Helper()
	job := f.ssn.Jobs[api.JobID("hrrn-test/"+group)]
	if job == nil || job.PodGroup == nil {
		t.Fatalf("missing JobInfo %s", group)
	}
	controller := true
	owner := metav1.OwnerReference{APIVersion: batchv1.SchemeGroupVersion.String(), Kind: "Job", Name: group,
		UID: types.UID("vc-job-" + group), Controller: &controller}
	job.PodGroup.OwnerReferences = []metav1.OwnerReference{owner}
	var template v1.PodSpec
	for _, task := range job.Tasks {
		task.Pod.OwnerReferences = []metav1.OwnerReference{owner}
		actual, err := f.ssn.KubeClient().CoreV1().Pods(task.Namespace).Get(context.Background(), task.Name, metav1.GetOptions{})
		if err != nil {
			t.Fatal(err)
		}
		actual.OwnerReferences = []metav1.OwnerReference{owner}
		if _, err := f.ssn.KubeClient().CoreV1().Pods(task.Namespace).Update(context.Background(), actual, metav1.UpdateOptions{}); err != nil {
			t.Fatal(err)
		}
		template = *task.Pod.Spec.DeepCopy()
	}
	object := &batchv1.Job{ObjectMeta: metav1.ObjectMeta{Name: group, Namespace: "hrrn-test", UID: owner.UID},
		Spec: batchv1.JobSpec{MinAvailable: job.MinAvailable, Tasks: []batchv1.TaskSpec{{Name: "worker", Replicas: replicas, Template: v1.PodTemplateSpec{Spec: template}}}}}
	created, err := f.ssn.VCClient().BatchV1alpha1().Jobs("hrrn-test").Create(context.Background(), object, metav1.CreateOptions{})
	if err != nil {
		t.Fatal(err)
	}
	return created
}

func assertNoStage6Assignment(t *testing.T, f *fixture, names ...string) {
	t.Helper()
	keys := []string{vgpu.AssignedIDsAnnotations, vgpu.AssignedIDsToAllocateAnnotations,
		vgpu.AssignedNodeAnnotations, vgpu.AssignedTimeAnnotations, vgpu.DeviceBindPhase, vgpu.BindTimeAnnotations}
	wanted := map[string]bool{}
	for _, name := range names {
		wanted[name] = true
		p, err := f.ssn.KubeClient().CoreV1().Pods("hrrn-test").Get(context.Background(), name, metav1.GetOptions{})
		if err != nil {
			t.Fatal(err)
		}
		if p.Spec.NodeName != "" {
			t.Fatalf("unexpected actual API binding for %s: %s", name, p.Spec.NodeName)
		}
		for _, key := range keys {
			if p.Annotations[key] != "" {
				t.Fatalf("unsafe residual assignment on %s, key %s: %q", name, key, p.Annotations[key])
			}
		}
	}
	// No transient publish is allowed either: deleting an exposed assignment
	// after the fact is too late if a live device plugin can consume it.
	history, ok := f.ssn.KubeClient().(interface{ Actions() []clienttesting.Action })
	if !ok {
		t.Fatal("expected fake Kubernetes client action history")
	}
	for _, action := range history.Actions() {
		patch, ok := action.(clienttesting.PatchAction)
		if !ok || patch.GetResource().Resource != "pods" || !wanted[patch.GetName()] {
			continue
		}
		var body struct {
			Metadata struct {
				Annotations map[string]*string `json:"annotations"`
			} `json:"metadata"`
		}
		if err := json.Unmarshal(patch.GetPatch(), &body); err != nil {
			t.Fatal(err)
		}
		for _, key := range keys {
			if v := body.Metadata.Annotations[key]; v != nil && *v != "" {
				t.Fatalf("blocked Job transiently published %s on %s: %q", key, patch.GetName(), *v)
			}
		}
	}
}

// This is a safety test of fail-closed multi-Pod gating and bypass, not a
// demonstration that the two-member Job eventually starts. The user-visible
// goal of complete multi-Pod startup requires a later device-plugin handshake.
func TestStage6CoordinatedFailClosedBypassesToSinglePodWithRealNodeLock(t *testing.T) {
	requireRF(t)
	oldEnabled, oldClient := vgpu.NodeLockEnable, nodelock.GetClient()
	t.Cleanup(func() { vgpu.NodeLockEnable = oldEnabled; _ = nodelock.UseClient(oldClient) })
	groups := []*schedulingv1.PodGroup{pg("background", "100", 1, 1), pg("large", "10", 2, 1), pg("small", "100", 1, 1)}
	groups[0].Status.Phase = schedulingv1.PodGroupRunning
	groups[2].CreationTimestamp = metav1.NewTime(base.Add(-30 * time.Second))
	background := pod("background", "background-0", 1, 30)
	background.Spec.NodeName, background.Status.Phase = "node", v1.PodRunning
	background.Annotations[vgpu.AssignedIDsAnnotations] = "SIM-HRRN-GPU-0,NVIDIA,4096,30:"
	background.Annotations[vgpu.AssignedNodeAnnotations] = "node"
	pods := []*v1.Pod{background, pod("large", "large-0", 1, 40), pod("large", "large-1", 1, 40), pod("small", "small-0", 1, 60)}
	tracker := NewTracker()
	warm := setupCoordinatedStage6(t, groups, pods, tracker, func() time.Time { return base })
	closeFixture(warm)
	f := setupCoordinatedStage6(t, groups, pods, tracker, func() time.Time { return base.Add(20 * time.Second) })
	defer closeFixture(f)
	addStage6VolcanoJob(t, f, "small", 1)
	f.gpus.AddResource(background)
	vgpu.NodeLockEnable = true
	large, small := f.ssn.Jobs[api.JobID("hrrn-test/large")], f.ssn.Jobs[api.JobID("hrrn-test/small")]
	if f.plugin.compare(large, small) >= 0 {
		t.Fatal("fixture does not prefer the large Job in HRRN")
	}
	if vr := f.ssn.JobValid(large); vr == nil || vr.Pass {
		t.Fatal("unsafe multi-Pod GPU Job was not blocked before allocation", vr)
	}
	if vr := f.ssn.JobValid(small); vr != nil && !vr.Pass {
		t.Fatal("single-Pod bypass was unexpectedly blocked", vr)
	}
	f.run(t, "small-0")
	if large.ReadyTaskNum() != 0 || small.ReadyTaskNum() != 1 {
		t.Fatal("blocked Job partially started or small Job failed to bind")
	}
	assertNoStage6Assignment(t, f, "large-0", "large-1")
	device := f.gpus.Device[0]
	if device.UsedCore != 90 || device.UsedMem != 8192 || device.UsedNum != 2 {
		t.Fatalf("background and bypass ledger wrong: %+v", device)
	}
	got, err := f.ssn.KubeClient().CoreV1().Pods("hrrn-test").Get(context.Background(), "small-0", metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	want := "SIM-HRRN-GPU-0,NVIDIA,4096,60:"
	if got.Annotations[vgpu.AssignedIDsAnnotations] != want || got.Annotations[vgpu.AssignedIDsToAllocateAnnotations] != want || got.Annotations[vgpu.AssignedNodeAnnotations] != "node" {
		t.Fatalf("small Job UUID/quota mismatch: %+v", got.Annotations)
	}
	locked, err := f.ssn.KubeClient().CoreV1().Nodes().Get(context.Background(), "node", metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := time.Parse(time.RFC3339, locked.Annotations[vgpu.DeviceName]); err != nil {
		t.Fatalf("real NodeLock missing for bound single Pod: %v", err)
	}
	// With no device plugin/kubelet here, the held lock proves single-Pod
	// ownership is pending, not a leaked speculative multi-Pod lock. No test
	// manually clears it or claims a real Device Plugin acknowledgement.
	if !strings.Contains(got.Annotations[vgpu.AssignedIDsAnnotations], "SIM-HRRN-GPU-0") {
		t.Fatal("missing native GPU UUID")
	}
	// Simulate completion by removing the two scheduled tasks through the
	// native NodeInfo/SubResource path. No Kubernetes kubelet or device plugin
	// runs here, so do not synthesize an acknowledgement or unlock the node.
	for _, name := range []string{"background", "small"} {
		job := f.ssn.Jobs[api.JobID("hrrn-test/"+name)]
		for _, task := range job.Tasks {
			if err := f.ssn.Nodes["node"].RemoveTask(task); err != nil {
				t.Fatal(err)
			}
			if err := job.UpdateTaskStatus(task, api.Succeeded); err != nil {
				t.Fatal(err)
			}
			uid := task.Pod.UID
			if err := f.ssn.KubeClient().CoreV1().Pods(task.Namespace).Delete(context.Background(), task.Name, metav1.DeleteOptions{Preconditions: &metav1.Preconditions{UID: &uid}}); err != nil {
				t.Fatal(err)
			}
		}
	}
	if device.UsedCore != 0 || device.UsedMem != 0 || device.UsedNum != 0 {
		t.Fatalf("native task removal leaked GPU resources: %+v", device)
	}
	after, err := f.ssn.KubeClient().CoreV1().Nodes().Get(context.Background(), "node", metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if after.Annotations[vgpu.DeviceName] != locked.Annotations[vgpu.DeviceName] {
		t.Fatal("lock unexpectedly cleared without device-plugin acknowledgement")
	}
}

func TestStage6CoordinatedEmptyCapacityStillFailsClosed(t *testing.T) {
	requireRF(t)
	oldEnabled, oldClient := vgpu.NodeLockEnable, nodelock.GetClient()
	t.Cleanup(func() { vgpu.NodeLockEnable = oldEnabled; _ = nodelock.UseClient(oldClient) })
	groups := []*schedulingv1.PodGroup{pg("large", "10", 2, 1)}
	pods := []*v1.Pod{pod("large", "large-0", 1, 40), pod("large", "large-1", 1, 40)}
	f := setupCoordinatedStage6(t, groups, pods, NewTracker(), func() time.Time { return base })
	defer closeFixture(f)
	vgpu.NodeLockEnable = true
	if vr := f.ssn.JobValid(f.ssn.Jobs["hrrn-test/large"]); vr == nil || vr.Pass {
		t.Fatal("empty but uncoordinated multi-Pod Job must fail closed", vr)
	}
	f.run(t)
	assertNoStage6Assignment(t, f, "large-0", "large-1")
	device := f.gpus.Device[0]
	if device.UsedCore != 0 || device.UsedMem != 0 || device.UsedNum != 0 {
		t.Fatalf("blocked Job changed ledger: %+v", device)
	}
	locked, err := f.ssn.KubeClient().CoreV1().Nodes().Get(context.Background(), "node", metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if value := locked.Annotations[vgpu.DeviceName]; value != "" {
		t.Fatal("blocked Job took a node lock", value)
	}
}

// The guard is scoped to complete Jobs with GPU demand. A mixed GPU/CPU Job
// is still multi-member and must be blocked; unrelated CPU Jobs may bypass.
func TestStage6CoordinatedGuardPreservesCPUMultiPodAndBlocksMixedJob(t *testing.T) {
	requireRF(t)
	oldEnabled, oldClient := vgpu.NodeLockEnable, nodelock.GetClient()
	t.Cleanup(func() { vgpu.NodeLockEnable = oldEnabled; _ = nodelock.UseClient(oldClient) })
	for _, gpuInRequests := range []bool{false, true} {
		name := "gpu-limits"
		if gpuInRequests {
			name = "gpu-requests"
		}
		t.Run(name, func(t *testing.T) {
			groups := []*schedulingv1.PodGroup{pg("mixed", "1", 2, 1), pg("cpu", "100", 2, 1)}
			gpu := pod("mixed", "mixed-gpu", 1, 40)
			if gpuInRequests {
				gpu.Spec.Containers[0].Resources.Limits = nil
				gpu.Spec.Containers[0].Resources.Requests[v1.ResourceName("volcano.sh/vgpu-number")] = resource.MustParse("1")
			}
			cpuMember := pod("mixed", "mixed-cpu", 0, 0)
			cpuMember.Spec.Containers[0].Resources.Limits = nil
			cpu0, cpu1 := pod("cpu", "cpu-0", 0, 0), pod("cpu", "cpu-1", 0, 0)
			cpu0.Spec.Containers[0].Resources.Limits, cpu1.Spec.Containers[0].Resources.Limits = nil, nil
			f := setupCoordinatedStage6(t, groups, []*v1.Pod{gpu, cpuMember, cpu0, cpu1}, NewTracker(), func() time.Time { return base })
			defer closeFixture(f)
			addStage6VolcanoJob(t, f, "cpu", 2)
			vgpu.NodeLockEnable = true
			if vr := f.ssn.JobValid(f.ssn.Jobs["hrrn-test/mixed"]); vr == nil || vr.Pass {
				t.Fatal("mixed multi-member GPU Job was not blocked", vr)
			}
			if vr := f.ssn.JobValid(f.ssn.Jobs["hrrn-test/cpu"]); vr != nil && !vr.Pass {
				t.Fatal("unrelated CPU-only Job was blocked", vr)
			}
			f.run(t, "cpu-0", "cpu-1")
			assertNoStage6Assignment(t, f, "mixed-gpu", "mixed-cpu")
			device := f.gpus.Device[0]
			if device.UsedCore != 0 || device.UsedMem != 0 || device.UsedNum != 0 {
				t.Fatalf("CPU-only Job changed GPU ledger: %+v", device)
			}
			node, err := f.ssn.KubeClient().CoreV1().Nodes().Get(context.Background(), "node", metav1.GetOptions{})
			if err != nil {
				t.Fatal(err)
			}
			if value := node.Annotations[vgpu.DeviceName]; value != "" {
				t.Fatal("CPU bypass took a GPU node lock", value)
			}
		})
	}
}

// An invalid explicit opt-in must never silently fall back to the known unsafe
// partial-commit path. A future startup configuration error also satisfies this
// safety objective, but the current Session API exposes JobValid rather than an
// open-session error, so assert no allocation through the real action.
func TestStage6MisconfiguredExplicitOptInStillFailsClosed(t *testing.T) {
	requireRF(t)
	oldEnabled, oldClient := vgpu.NodeLockEnable, nodelock.GetClient()
	t.Cleanup(func() { vgpu.NodeLockEnable = oldEnabled; _ = nodelock.UseClient(oldClient) })
	for _, flags := range []struct {
		name              string
		enabled, complete bool
	}{
		{"disabled-hrnn", false, true}, {"disabled-complete", true, false},
	} {
		t.Run(flags.name, func(t *testing.T) {
			groups := []*schedulingv1.PodGroup{pg("large", "10", 2, 1)}
			pods := []*v1.Pod{pod("large", "large-0", 1, 40), pod("large", "large-1", 1, 40)}
			f := setupCoordinatedStage6(t, groups, pods, NewTracker(), func() time.Time { return base }, flags.enabled, flags.complete, true)
			defer closeFixture(f)
			vgpu.NodeLockEnable = true
			if vr := f.ssn.JobValid(f.ssn.Jobs["hrrn-test/large"]); vr == nil || vr.Pass {
				t.Fatal("invalid opt-in silently allowed multi-Pod GPU Job", vr)
			}
			f.run(t)
			assertNoStage6Assignment(t, f, "large-0", "large-1")
			if d := f.gpus.Device[0]; d.UsedCore != 0 || d.UsedMem != 0 || d.UsedNum != 0 {
				t.Fatalf("invalid opt-in leaked GPU ledger: %+v", d)
			}
			node, err := f.ssn.KubeClient().CoreV1().Nodes().Get(context.Background(), "node", metav1.GetOptions{})
			if err != nil {
				t.Fatal(err)
			}
			if node.Annotations[vgpu.DeviceName] != "" {
				t.Fatal("invalid opt-in leaked NodeLock")
			}
		})
	}
}

func TestStage6InvalidCoordinationAndIncompleteMembershipFailClosed(t *testing.T) {
	requireRF(t)
	oldEnabled, oldClient := vgpu.NodeLockEnable, nodelock.GetClient()
	t.Cleanup(func() { vgpu.NodeLockEnable = oldEnabled; _ = nodelock.UseClient(oldClient) })
	tests := []struct {
		name         string
		coordination interface{}
		declared     string
		observed     int
	}{
		{"malformed-opt-in", "not-a-boolean", "2", 2},
		{"declared-two-one-observed", true, "2", 1},
		{"missing-declaration", true, "", 1},
		{"invalid-declaration", true, "not-a-number", 1},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			group := pg("large", "10", 2, 1)
			if tc.declared == "" {
				delete(group.Annotations, TotalPodsAnnotation)
			} else {
				group.Annotations[TotalPodsAnnotation] = tc.declared
			}
			pods := []*v1.Pod{pod("large", "large-0", 1, 40)}
			if tc.observed == 2 {
				pods = append(pods, pod("large", "large-1", 1, 40))
			}
			f := setupCoordinatedStage6(t, []*schedulingv1.PodGroup{group}, pods, NewTracker(), func() time.Time { return base }, true, true, tc.coordination)
			defer closeFixture(f)
			vgpu.NodeLockEnable = true
			if vr := f.ssn.JobValid(f.ssn.Jobs["hrrn-test/large"]); vr == nil || vr.Pass {
				t.Fatal("unsafe incomplete or malformed Job accepted", vr)
			}
			f.run(t)
			names := []string{"large-0"}
			if tc.observed == 2 {
				names = append(names, "large-1")
			}
			assertNoStage6Assignment(t, f, names...)
			if d := f.gpus.Device[0]; d.UsedCore != 0 || d.UsedMem != 0 || d.UsedNum != 0 {
				t.Fatalf("GPU ledger changed: %+v", d)
			}
			node, err := f.ssn.KubeClient().CoreV1().Nodes().Get(context.Background(), "node", metav1.GetOptions{})
			if err != nil {
				t.Fatal(err)
			}
			if node.Annotations[vgpu.DeviceName] != "" {
				t.Fatal("unexpected node lock")
			}
		})
	}
}

func TestStage6CustomResourceNamesAndInitContainersFailClosed(t *testing.T) {
	requireRF(t)
	oldEnabled, oldClient := vgpu.NodeLockEnable, nodelock.GetClient()
	t.Cleanup(func() { vgpu.NodeLockEnable = oldEnabled; _ = nodelock.UseClient(oldClient) })
	// Create the usual default config if no test has initialized it, then restore
	// its entire NvidiaConfig after the subtests; no model or scoring is changed.
	if devicesconfig.GetConfig() == nil {
		devicesconfig.InitDevicesConfig("stage6-not-installed", "hrrn-test")
	}
	cfg := devicesconfig.GetConfig()
	if cfg == nil {
		t.Fatal("default device configuration not initialized")
	}
	original := cfg.NvidiaConfig
	t.Cleanup(func() { cfg.NvidiaConfig = original })
	cfg.NvidiaConfig.ResourceCountName = "example.org/split-gpu"
	cfg.NvidiaConfig.ResourceMemoryName = "example.org/video-memory"
	cfg.NvidiaConfig.ResourceCoreName = "example.org/core-share"
	cfg.NvidiaConfig.ResourceMemoryPercentageName = "example.org/memory-percent"
	for _, tc := range []struct {
		name, key string
		init      bool
		quantity  string
	}{
		{"custom-count", "example.org/split-gpu", false, "1"},
		{"custom-memory", "example.org/video-memory", false, "1"},
		{"custom-core", "example.org/core-share", false, "1"},
		{"custom-percent", "example.org/memory-percent", false, "1"},
		{"init-container", "example.org/split-gpu", true, "1"},
		{"zero-value-key", "example.org/split-gpu", false, "0"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			gpu := pod("mixed", "mixed-gpu", 0, 0)
			gpu.Spec.Containers[0].Resources.Limits = nil
			resources := v1.ResourceRequirements{Limits: v1.ResourceList{v1.ResourceName(tc.key): resource.MustParse(tc.quantity)}}
			if tc.init {
				gpu.Spec.InitContainers = []v1.Container{{Name: "gpu-init", Resources: resources}}
			} else {
				gpu.Spec.Containers[0].Resources.Limits = resources.Limits
			}
			cpu := pod("mixed", "mixed-cpu", 0, 0)
			cpu.Spec.Containers[0].Resources.Limits = nil
			f := setupCoordinatedStage6(t, []*schedulingv1.PodGroup{pg("mixed", "10", 2, 1)}, []*v1.Pod{gpu, cpu}, NewTracker(), func() time.Time { return base })
			defer closeFixture(f)
			vgpu.NodeLockEnable = true
			if vr := f.ssn.JobValid(f.ssn.Jobs["hrrn-test/mixed"]); vr == nil || vr.Pass {
				t.Fatalf("custom vGPU resource %q bypassed guard: %+v", tc.key, vr)
			}
			f.run(t)
			assertNoStage6Assignment(t, f, "mixed-gpu", "mixed-cpu")
			if d := f.gpus.Device[0]; d.UsedCore != 0 || d.UsedMem != 0 || d.UsedNum != 0 {
				t.Fatalf("blocked Job changed ledger: %+v", d)
			}
			node, err := f.ssn.KubeClient().CoreV1().Nodes().Get(context.Background(), "node", metav1.GetOptions{})
			if err != nil {
				t.Fatal(err)
			}
			if node.Annotations[vgpu.DeviceName] != "" {
				t.Fatal("blocked Job took NodeLock")
			}
		})
	}
}

// A CPU-only observed prefix does not establish that the complete Job has no
// vGPU demand. An unseen member may request a GPU. In particular, invalid
// explicit coordination configuration must not fall back to partial binding.
func TestStage6IncompleteCPUOnlyPrefixCannotBypassGuard(t *testing.T) {
	requireRF(t)
	oldEnabled, oldClient := vgpu.NodeLockEnable, nodelock.GetClient()
	t.Cleanup(func() { vgpu.NodeLockEnable = oldEnabled; _ = nodelock.UseClient(oldClient) })
	tests := []struct {
		name              string
		enabled, complete bool
		coordination      interface{}
		declared          string
	}{
		{name: "hrnn-disabled-declared-two", enabled: false, complete: true, coordination: true, declared: "2"},
		{name: "complete-disabled-declared-two", enabled: true, complete: false, coordination: true, declared: "2"},
		{name: "enabled-declaration-missing", enabled: true, complete: true, coordination: true, declared: ""},
		{name: "hrnn-disabled-declaration-missing", enabled: false, complete: true, coordination: true, declared: ""},
		{name: "complete-disabled-declaration-missing", enabled: true, complete: false, coordination: true, declared: ""},
		{name: "malformed-declaration", enabled: true, complete: true, coordination: true, declared: "invalid"},
		{name: "malformed-coordination", enabled: false, complete: false, coordination: "invalid", declared: "2"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			group := pg("incomplete", "10", 2, 1)
			if tc.declared == "" {
				delete(group.Annotations, TotalPodsAnnotation)
			} else {
				group.Annotations[TotalPodsAnnotation] = tc.declared
			}
			cpu := pod("incomplete", "cpu-observed", 0, 0)
			cpu.Spec.Containers[0].Resources.Limits = nil
			f := setupCoordinatedStage6(t, []*schedulingv1.PodGroup{group}, []*v1.Pod{cpu}, NewTracker(),
				func() time.Time { return base }, tc.enabled, tc.complete, tc.coordination)
			defer closeFixture(f)
			vgpu.NodeLockEnable = true
			if vr := f.ssn.JobValid(f.ssn.Jobs["hrrn-test/incomplete"]); vr == nil || vr.Pass {
				t.Fatalf("unsafe incomplete CPU-only prefix accepted: %+v", vr)
			}
			f.run(t)
			assertNoStage6Assignment(t, f, "cpu-observed")
			if d := f.gpus.Device[0]; d.UsedCore != 0 || d.UsedMem != 0 || d.UsedNum != 0 {
				t.Fatalf("incomplete Job changed GPU ledger: %+v", d)
			}
			node, err := f.ssn.KubeClient().CoreV1().Nodes().Get(context.Background(), "node", metav1.GetOptions{})
			if err != nil {
				t.Fatal(err)
			}
			if node.Annotations[vgpu.DeviceName] != "" {
				t.Fatal("incomplete Job took NodeLock")
			}
		})
	}
}

// Duplicated HRRN plugins share name-keyed hooks. Both tier orders must keep
// the union of explicitly requested safety restrictions even after overwrite.
func TestStage6DuplicatePluginsCannotDisableCoordinationGuard(t *testing.T) {
	requireRF(t)
	oldEnabled, oldClient := vgpu.NodeLockEnable, nodelock.GetClient()
	t.Cleanup(func() { vgpu.NodeLockEnable = oldEnabled; _ = nodelock.UseClient(oldClient) })
	for _, order := range []string{"true-first", "false-first"} {
		t.Run(order, func(t *testing.T) {
			groups := []*schedulingv1.PodGroup{pg("large", "10", 2, 1)}
			pods := []*v1.Pod{pod("large", "large-0", 1, 40), pod("large", "large-1", 1, 40)}
			f := setupCoordinatedStage6(t, groups, pods, NewTracker(), func() time.Time { return base }, true, true, true, order)
			defer closeFixture(f)
			vgpu.NodeLockEnable = true
			if vr := f.ssn.JobValid(f.ssn.Jobs["hrrn-test/large"]); vr == nil || vr.Pass || vr.Reason != "HRRNCoordinatedHandoffUnavailable" {
				t.Fatalf("duplicate plugins bypassed coordinated fail-closed guard: %+v", vr)
			}
			f.run(t)
			assertNoStage6Assignment(t, f, "large-0", "large-1")
			if d := f.gpus.Device[0]; d.UsedCore != 0 || d.UsedMem != 0 || d.UsedNum != 0 {
				t.Fatalf("duplicate plugin changed GPU ledger: %+v", d)
			}
			node, err := f.ssn.KubeClient().CoreV1().Nodes().Get(context.Background(), "node", metav1.GetOptions{})
			if err != nil {
				t.Fatal(err)
			}
			if node.Annotations[vgpu.DeviceName] != "" {
				t.Fatal("duplicate plugin allowed node lock")
			}
		})
	}
}

// Use the actual Session and fake VolcanoJob API, not an injected always-true
// predicate, to verify declared membership and controller UID failure cases.
func TestStage6AuthoritativeVolcanoJobRejectsUnverifiedSingletons(t *testing.T) {
	requireRF(t)
	oldEnabled, oldClient := vgpu.NodeLockEnable, nodelock.GetClient()
	t.Cleanup(func() { vgpu.NodeLockEnable = oldEnabled; _ = nodelock.UseClient(oldClient) })
	tests := []struct {
		name                                    string
		cpu                                     bool
		actualReplicas                          int32
		ownerless, mismatch, deleteJob, denyGet bool
	}{
		{name: "gpu-declares-one-spec-two", actualReplicas: 2},
		{name: "cpu-declares-one-spec-two", cpu: true, actualReplicas: 2},
		{name: "gpu-ownerless", ownerless: true},
		{name: "cpu-ownerless", cpu: true, ownerless: true},
		{name: "gpu-owner-uid-mismatch", actualReplicas: 1, mismatch: true},
		{name: "gpu-owner-job-missing", actualReplicas: 1, deleteJob: true},
		{name: "gpu-owner-api-error", actualReplicas: 1, denyGet: true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			group := pg("single", "10", 1, 1)
			p := pod("single", "single-0", 1, 40)
			if tc.cpu {
				p.Spec.Containers[0].Resources.Limits = nil
			}
			f := setupCoordinatedStage6(t, []*schedulingv1.PodGroup{group}, []*v1.Pod{p}, NewTracker(), func() time.Time { return base })
			defer closeFixture(f)
			vgpu.NodeLockEnable = true
			if !tc.ownerless {
				object := addStage6VolcanoJob(t, f, "single", tc.actualReplicas)
				if tc.mismatch {
					object.UID = "recreated-volcano-job"
					if _, err := f.ssn.VCClient().BatchV1alpha1().Jobs("hrrn-test").Update(context.Background(), object, metav1.UpdateOptions{}); err != nil {
						t.Fatal(err)
					}
				}
				if tc.deleteJob {
					if err := f.ssn.VCClient().BatchV1alpha1().Jobs("hrrn-test").Delete(context.Background(), object.Name, metav1.DeleteOptions{}); err != nil {
						t.Fatal(err)
					}
				}
				if tc.denyGet {
					client, ok := f.ssn.VCClient().(*vcfake.Clientset)
					if !ok {
						t.Fatal("expected real fake VolcanoJob client")
					}
					client.PrependReactor("get", "jobs", func(clienttesting.Action) (bool, runtime.Object, error) {
						return true, nil, fmt.Errorf("synthetic API failure")
					})
				}
			}
			if vr := f.ssn.JobValid(f.ssn.Jobs["hrrn-test/single"]); vr == nil || vr.Pass || vr.Reason != "HRRNCoordinatedHandoffUnavailable" {
				t.Fatalf("unverified singleton was accepted: %+v", vr)
			}
			f.run(t)
			assertNoStage6Assignment(t, f, "single-0")
			if d := f.gpus.Device[0]; d.UsedCore != 0 || d.UsedMem != 0 || d.UsedNum != 0 {
				t.Fatalf("unverified singleton changed GPU ledger: %+v", d)
			}
			node, err := f.ssn.KubeClient().CoreV1().Nodes().Get(context.Background(), "node", metav1.GetOptions{})
			if err != nil {
				t.Fatal(err)
			}
			if node.Annotations[vgpu.DeviceName] != "" {
				t.Fatal("unverified singleton acquired NodeLock")
			}
		})
	}
}

func TestStage6VolcanoTemplateCannotHideVGPUFromMultiPodGuard(t *testing.T) {
	requireRF(t)
	oldEnabled, oldClient := vgpu.NodeLockEnable, nodelock.GetClient()
	t.Cleanup(func() { vgpu.NodeLockEnable = oldEnabled; _ = nodelock.UseClient(oldClient) })
	group := pg("cpu", "10", 2, 1)
	p0, p1 := pod("cpu", "cpu-0", 0, 0), pod("cpu", "cpu-1", 0, 0)
	p0.Spec.Containers[0].Resources.Limits, p1.Spec.Containers[0].Resources.Limits = nil, nil
	f := setupCoordinatedStage6(t, []*schedulingv1.PodGroup{group}, []*v1.Pod{p0, p1}, NewTracker(), func() time.Time { return base })
	defer closeFixture(f)
	vgpu.NodeLockEnable = true
	object := addStage6VolcanoJob(t, f, "cpu", 2)
	object.Spec.Tasks[0].Template.Spec.Containers[0].Resources.Limits = v1.ResourceList{"volcano.sh/vgpu-number": resource.MustParse("1")}
	if _, err := f.ssn.VCClient().BatchV1alpha1().Jobs("hrrn-test").Update(context.Background(), object, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	if vr := f.ssn.JobValid(f.ssn.Jobs["hrrn-test/cpu"]); vr == nil || vr.Pass || vr.Reason != "HRRNCoordinatedHandoffUnavailable" {
		t.Fatalf("GPU template hidden behind CPU-only observed members: %+v", vr)
	}
	f.run(t)
	assertNoStage6Assignment(t, f, "cpu-0", "cpu-1")
	if d := f.gpus.Device[0]; d.UsedCore != 0 || d.UsedMem != 0 || d.UsedNum != 0 {
		t.Fatalf("blocked template changed GPU ledger: %+v", d)
	}
}
