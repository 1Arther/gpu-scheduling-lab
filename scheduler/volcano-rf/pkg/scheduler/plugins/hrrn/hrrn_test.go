package hrrn

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	v1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	schedulingv1 "volcano.sh/apis/pkg/apis/scheduling/v1beta1"
	"volcano.sh/volcano/cmd/scheduler/app/options"
	"volcano.sh/volcano/pkg/scheduler/actions/allocate"
	"volcano.sh/volcano/pkg/scheduler/actions/backfill"
	"volcano.sh/volcano/pkg/scheduler/actions/enqueue"
	"volcano.sh/volcano/pkg/scheduler/api"
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
	"volcano.sh/volcano/pkg/scheduler/uthelper"
	"volcano.sh/volcano/pkg/scheduler/util"
)

func TestMain(m *testing.M) { options.Default(); os.Exit(m.Run()) }

var base = time.Unix(1700000000, 0)

func ptr(x bool) *bool { return &x }

func pg(name, estimate string, n, min int) *schedulingv1.PodGroup {
	g := util.BuildPodGroup(name, "hrrn-test", "q", int32(min), nil, schedulingv1.PodGroupInqueue)
	g.UID = types.UID("uid-" + name)
	g.CreationTimestamp = metav1.NewTime(base.Add(-time.Minute))
	g.Annotations = map[string]string{EstimateAnnotation: estimate, TotalPodsAnnotation: fmt.Sprint(n)}
	return g
}
func pod(job, name string, number, cores int) *v1.Pod {
	req := api.BuildResourceList("100m", "16Mi")
	p := util.BuildPod("hrrn-test", name, "", v1.PodPending, req, job, map[string]string{"gpu-lab/workload-type": "training"}, nil)
	p.CreationTimestamp = metav1.NewTime(base.Add(-24 * time.Hour))
	p.Annotations["volcano.sh/task-spec"] = "worker"
	p.Spec.Containers[0].Name = "sleep"
	p.Spec.Containers[0].Resources.Limits = v1.ResourceList{"volcano.sh/vgpu-number": resource.MustParse(fmt.Sprint(number)), "volcano.sh/vgpu-memory": resource.MustParse("4096"), "volcano.sh/vgpu-cores": resource.MustParse(fmt.Sprint(cores))}
	return p
}

type fixture struct {
	test            uthelper.TestCommonStruct
	ssn             *framework.Session
	plugin          *Plugin
	gpus            *vgpu.GPUDevices
	tiers           []conf.Tier
	decisionSeconds float64
}

func setup(t *testing.T, groups []*schedulingv1.PodGroup, pods []*v1.Pod, enabled, complete bool, policy string, count int, tracker *Tracker, now func() time.Time) *fixture {
	t.Helper()
	f := &fixture{}
	dsargs := framework.Arguments{"deviceshare.VGPUEnable": true, "deviceshare.GPUSelectPolicy": policy,
		"deviceshare.RFEnable": policy == vgpu.RFJobPolicy, "deviceshare.RFEndpoint": os.Getenv("RF_LIVE_ENDPOINT"),
		"deviceshare.RFModelVersion": "rf-v4-known-states-ee35cfabe30f", "deviceshare.RFTimeoutMS": 1000, "deviceshare.ScheduleWeight": 1, "deviceshare.NodeLockEnable": false}
	op := func(name string, args framework.Arguments) conf.PluginOption {
		return conf.PluginOption{Name: name, Arguments: args, EnabledJobOrder: ptr(true), EnabledJobReady: ptr(true), EnabledJobPipelined: ptr(true), EnabledTaskOrder: ptr(true), EnabledQueueOrder: ptr(true), EnabledPredicate: ptr(true), EnabledNodeOrder: ptr(true), EnabledOverused: ptr(true), EnabledAllocatable: ptr(true)}
	}
	f.tiers = []conf.Tier{{Plugins: []conf.PluginOption{op("priority", nil), op("gang", nil), op("conformance", nil)}},
		{Plugins: []conf.PluginOption{op("overcommit", nil), op("drf", nil), op("predicates", framework.Arguments{"predicate.CacheEnable": false}), op("proportion", nil), op("nodeorder", nil), op("binpack", nil), op("deviceshare", dsargs), op(PluginName, framework.Arguments{"enabled": enabled, "complete-job": complete})}}}
	builders := map[string]framework.PluginBuilder{"priority": priority.New, "gang": gang.New, "drf": drf.New, "proportion": proportion.New,
		"predicates": predicates.New, "deviceshare": deviceshare.New, "nodeorder": nodeorder.New, "binpack": binpack.New, "overcommit": overcommit.New, "conformance": conformance.New,
		PluginName: func(a framework.Arguments) framework.Plugin {
			f.plugin = NewWithClock(a, now, tracker)
			return f.plugin
		}}
	node := util.BuildNode("node", api.BuildResourceList("8", "32Gi", api.ScalarResource{Name: "pods", Value: "100"}), nil)
	f.test = uthelper.TestCommonStruct{Name: t.Name(), Pods: pods, PodGroups: groups, Nodes: []*v1.Node{node}, Queues: []*schedulingv1.Queue{util.BuildQueue("q", 1, nil)}, Plugins: builders}
	f.ssn = f.test.RegisterSession(f.tiers, []conf.Configuration{{Name: "allocate", Arguments: map[string]interface{}{conf.EnablePredicateErrCacheKey: false}}})
	for _, p := range pods {
		_, err := f.ssn.KubeClient().CoreV1().Pods(p.Namespace).Create(context.Background(), p.DeepCopy(), metav1.CreateOptions{})
		if err != nil {
			t.Fatal(err)
		}
	}
	f.gpus = &vgpu.GPUDevices{Name: "node", Mode: "hami-core", Device: map[int]*vgpu.GPUDevice{}, Sharing: vgpu.HAMICoreFactory{}}
	for i := 0; i < count; i++ {
		g := vgpu.NewGPUDevice(i, 23552)
		g.UUID = fmt.Sprintf("SIM-HRRN-GPU-%d", i)
		g.Node = "node"
		g.Type = "NVIDIA GeForce RTX 4090"
		g.Number = 10
		g.Health = true
		f.gpus.Device[i] = g
	}
	f.ssn.Nodes["node"].Others[vgpu.DeviceName] = f.gpus
	return f
}
func closeFixture(f *fixture) { f.test.Close() }
func (f *fixture) run(t *testing.T, want ...string) {
	t.Helper()
	f.test.ExpectBindMap = map[string]string{}
	for _, name := range want {
		f.test.ExpectBindMap["hrrn-test/"+name] = "node"
	}
	f.test.ExpectBindsNum = len(want)
	started := time.Now()
	f.test.Run([]framework.Action{enqueue.New(), allocate.New(), backfill.New()})
	f.decisionSeconds = time.Since(started).Seconds()
	if err := f.test.CheckBind(0); err != nil {
		t.Fatal(err)
	}
	for _, g := range f.gpus.Device {
		if g.UsedCore > 100 || g.UsedMem > g.Memory || g.UsedNum > g.Number {
			t.Fatalf("overallocated: %+v", g)
		}
	}
}

func TestSnapshotWaitingClockAndFallback(t *testing.T) {
	tracker := NewTracker()
	now := base
	calls := 0
	clock := func() time.Time { calls++; return now }
	gs := []*schedulingv1.PodGroup{pg("long", "100", 1, 1), pg("short", "10", 1, 1)}
	ps := []*v1.Pod{pod("long", "long-0", 1, 50), pod("short", "short-0", 1, 50)}
	f := setup(t, gs, ps, true, false, vgpu.OriginalPolicy, 1, tracker, clock)
	if s, _ := f.plugin.Snapshot("hrrn-test/long"); s.WaitingSeconds != 0 {
		t.Fatalf("Pod creation incorrectly counted: %+v", s)
	}
	closeFixture(f)
	now = base.Add(20 * time.Second)
	f = setup(t, gs, ps, true, false, vgpu.OriginalPolicy, 1, tracker, clock)
	defer closeFixture(f)
	short, _ := f.plugin.Snapshot("hrrn-test/short")
	long, _ := f.plugin.Snapshot("hrrn-test/long")
	if short.Score != 3 || long.Score != 1.2 || !short.ReadyAt.Equal(base) {
		t.Fatalf("wrong HRRN: %+v %+v", short, long)
	}
	now = now.Add(time.Hour)
	if s, _ := f.plugin.Snapshot("hrrn-test/short"); s != short || calls != 2 {
		t.Fatal("Session time changed")
	}
	a, b := f.ssn.Jobs["hrrn-test/short"], f.ssn.Jobs["hrrn-test/long"]
	if !f.ssn.JobOrderFn(a, b) {
		t.Fatal("HRRN not reached after native tie")
	}
	b.Priority = 20
	if f.ssn.JobOrderFn(a, b) || f.plugin.compare(a, b) != 0 {
		t.Fatal("priority isolation failed")
	}
	b.Priority = a.Priority
	b.Queue = "another"
	if f.plugin.compare(a, b) != 0 {
		t.Fatal("cross-queue HRRN")
	}
	for _, value := range []string{"", "0", "-1", "NaN", "+Inf", "garbage"} {
		b.PodGroup.Annotations[EstimateAnnotation] = value
		duration, why := estimate(b, 60)
		if duration != 60 || !strings.Contains(why, "invalid") {
			t.Fatal(value, duration, why)
		}
	}
	delete(b.PodGroup.Annotations, EstimateAnnotation)
	if duration, why := estimate(b, 60); duration != 60 || why != "estimate_missing_default" {
		t.Fatal(duration, why)
	}
	if positive(math.NaN()) || positive(math.Inf(1)) {
		t.Fatal("non-finite accepted")
	}
}

func TestCompleteReadinessGatesCancellationAndRestart(t *testing.T) {
	tracker := NewTracker()
	now := base
	groups := []*schedulingv1.PodGroup{pg("job", "10", 2, 1)}
	pods := []*v1.Pod{pod("job", "p0", 1, 50), pod("job", "p1", 1, 50)}
	pods[1].Spec.SchedulingGates = []v1.PodSchedulingGate{{Name: "gpu-lab/ready"}}
	f := setup(t, groups, pods, true, true, vgpu.OriginalPolicy, 1, tracker, func() time.Time { return now })
	if s, _ := f.plugin.Snapshot("hrrn-test/job"); s.Ready {
		t.Fatal("gated member counted")
	}
	closeFixture(f)
	now = now.Add(time.Minute)
	pods[1].Spec.SchedulingGates = nil
	f = setup(t, groups, pods, true, true, vgpu.OriginalPolicy, 1, tracker, func() time.Time { return now })
	if s, _ := f.plugin.Snapshot("hrrn-test/job"); !s.Ready || s.WaitingSeconds != 0 {
		t.Fatal(s)
	}
	closeFixture(f)
	now = now.Add(time.Minute)
	pods[1].DeletionTimestamp = &metav1.Time{Time: now}
	f = setup(t, groups, pods, true, true, vgpu.OriginalPolicy, 1, tracker, func() time.Time { return now })
	if s, _ := f.plugin.Snapshot("hrrn-test/job"); s.Ready {
		t.Fatal("deleted member counted")
	}
	closeFixture(f)
	pods[1].DeletionTimestamp = nil
	pods[1].UID = "replacement-uid"
	now = now.Add(time.Minute)
	f = setup(t, groups, pods, true, true, vgpu.OriginalPolicy, 1, tracker, func() time.Time { return now })
	if s, _ := f.plugin.Snapshot("hrrn-test/job"); !s.Ready || s.WaitingSeconds != 0 {
		t.Fatal(s)
	}
	closeFixture(f)
	// Scheduler restart deliberately does not manufacture historical waiting.
	now = now.Add(time.Minute)
	f = setup(t, groups, pods, true, true, vgpu.OriginalPolicy, 1, NewTracker(), func() time.Time { return now })
	defer closeFixture(f)
	if s, _ := f.plugin.Snapshot("hrrn-test/job"); s.WaitingSeconds != 0 {
		t.Fatal(s)
	}
}

func TestTieAndMisorderedConfiguration(t *testing.T) {
	p := NewWithClock(nil, time.Now, NewTracker())
	a, b := api.NewJobInfo("a"), api.NewJobInfo("b")
	p.snapshots[a.UID] = Snapshot{Score: 2, ReadyAt: base}
	p.snapshots[b.UID] = Snapshot{Score: 2, ReadyAt: base}
	if p.compare(a, b) != -1 || p.compare(b, a) != 1 || p.compare(a, a) != 0 {
		t.Fatal("UID tie break")
	}
	p.snapshots[b.UID] = Snapshot{Score: 2, ReadyAt: base.Add(-time.Second)}
	if p.compare(a, b) != 1 {
		t.Fatal("ready-at tie break")
	}
	if safeOrder([]conf.Tier{{Plugins: []conf.PluginOption{{Name: PluginName}, {Name: "drf"}}}}) {
		t.Fatal("unsafe order accepted")
	}
}

func requireRF(t *testing.T) {
	t.Helper()
	if os.Getenv("RF_LIVE_ENDPOINT") == "" {
		t.Skip("set RF_LIVE_ENDPOINT to the unchanged frozen Python RF service")
	}
}

func TestNativeOrderingAndPlacementComparisons(t *testing.T) {
	requireRF(t)
	modes := []struct {
		name              string
		enabled, complete bool
		policy, want      string
	}{
		{"A-native-rf", false, false, vgpu.RFJobPolicy, "long-0"},
		{"B-hrrn-rf", true, false, vgpu.RFJobPolicy, "short-0"},
		{"C-hrrn-complete-rf", true, true, vgpu.RFJobPolicy, "short-0"},
		{"D-native-binpack", false, false, vgpu.BinpackPolicy, "long-0"},
		{"E-native-spread", false, false, vgpu.SpreadPolicy, "long-0"},
	}
	var records []map[string]interface{}
	for _, mode := range modes {
		t.Run(mode.name, func(t *testing.T) {
			groups := []*schedulingv1.PodGroup{pg("long", "100", 1, 1), pg("short", "10", 1, 1)}
			groups[1].CreationTimestamp = metav1.NewTime(base.Add(-30 * time.Second))
			pods := []*v1.Pod{pod("long", "long-0", 1, 100), pod("short", "short-0", 1, 100)}
			tracker := NewTracker()
			warm := setup(t, groups, pods, mode.enabled, mode.complete, mode.policy, 1, tracker, func() time.Time { return base })
			closeFixture(warm)
			f := setup(t, groups, pods, mode.enabled, mode.complete, mode.policy, 1, tracker, func() time.Time { return base.Add(20 * time.Second) })
			defer closeFixture(f)
			start := time.Now()
			f.run(t, mode.want)
			elapsed := time.Since(start)
			saved, err := f.ssn.KubeClient().CoreV1().Pods("hrrn-test").Get(context.Background(), mode.want, metav1.GetOptions{})
			if err != nil {
				t.Fatal(err)
			}
			ids := saved.Annotations[vgpu.AssignedIDsToAllocateAnnotations]
			if !strings.Contains(ids, "SIM-HRRN-GPU-0") {
				t.Fatalf("actual fake-client UUID missing: %q", ids)
			}
			records = append(records, map[string]interface{}{"mode": mode.name, "gpuPolicy": mode.policy, "tiers": f.tiers, "actualBoundPod": mode.want, "actualUUIDAnnotation": ids, "actionsWallSeconds": f.decisionSeconds, "executeAndCheckWallSeconds": elapsed.Seconds(), "logicalUsedCores": f.gpus.Device[0].UsedCore, "evidence": "native enqueue/allocate/backfill + fake binder + fake-client UUID; no container execution"})
		})
	}
	if out := os.Getenv("HRRN_RESULTS"); out != "" {
		data, _ := json.MarshalIndent(records, "", "  ")
		if err := os.WriteFile(filepath.Join(out, "native-comparisons.json"), data, 0644); err != nil {
			t.Fatal(err)
		}
	}
}

func TestNativeFullJobBypassAndRollback(t *testing.T) {
	// Structural infeasibility/rollback control only: 2x60 cannot fit a
	// 100-core GPU even after all background Jobs finish. Eventual progress is
	// covered separately by TestTemporarilyUnavailableCompleteJobEventuallyBinds.
	requireRF(t)
	var records []map[string]interface{}
	for _, mode := range []struct {
		name              string
		enabled, complete bool
	}{{"A-native-rf", false, false}, {"B-hrrn-rf", true, false}, {"C-hrrn-complete-rf", true, true}} {
		complete := mode.complete
		t.Run(mode.name, func(t *testing.T) {
			// Gang min=1 is intentionally less than the two declared Pods.
			groups := []*schedulingv1.PodGroup{pg("large", "1", 2, 1), pg("small", "100", 1, 1)}
			groups[1].CreationTimestamp = metav1.NewTime(base.Add(-30 * time.Second))
			pods := []*v1.Pod{pod("large", "large-0", 1, 60), pod("large", "large-1", 1, 60), pod("small", "small-0", 1, 100)}
			tracker := NewTracker()
			warm := setup(t, groups, pods, mode.enabled, complete, vgpu.RFJobPolicy, 1, tracker, func() time.Time { return base })
			closeFixture(warm)
			f := setup(t, groups, pods, mode.enabled, complete, vgpu.RFJobPolicy, 1, tracker, func() time.Time { return base.Add(30 * time.Second) })
			defer closeFixture(f)
			if complete {
				f.run(t, "small-0")
			} else {
				f.run(t, "large-0")
			}
			if complete {
				large := f.ssn.Jobs["hrrn-test/large"]
				if large.ReadyTaskNum() != 0 || f.ssn.JobPipelined(large) {
					t.Fatal("partial full Job survived native rollback")
				}
				if f.gpus.Device[0].UsedCore != 100 || f.gpus.Device[0].UsedNum != 1 {
					t.Fatalf("rollback ledger wrong: %+v", f.gpus.Device[0])
				}
				for _, name := range []string{"large-0", "large-1"} {
					saved, _ := f.ssn.KubeClient().CoreV1().Pods("hrrn-test").Get(context.Background(), name, metav1.GetOptions{})
					if saved.Annotations[vgpu.AssignedIDsToAllocateAnnotations] != "" {
						t.Fatalf("stale UUID after rollback: %+v", saved.Annotations)
					}
				}
			}
			records = append(records, map[string]interface{}{"mode": mode.name, "largeReadyPods": f.ssn.Jobs["hrrn-test/large"].ReadyTaskNum(), "smallReadyPods": f.ssn.Jobs["hrrn-test/small"].ReadyTaskNum(), "usedCores": f.gpus.Device[0].UsedCore, "actionsWallSeconds": f.decisionSeconds, "gangMinAvailable": 1, "declaredTotalPods": 2})
		})
	}
	if out := os.Getenv("HRRN_RESULTS"); out != "" {
		data, _ := json.MarshalIndent(records, "", "  ")
		if err := os.WriteFile(filepath.Join(out, "bypass-comparisons.json"), data, 0644); err != nil {
			t.Fatal(err)
		}
	}
}

func TestOriginalGangAlreadyBypassesUnfitCompleteMinimum(t *testing.T) {
	requireRF(t)
	f := setup(t, []*schedulingv1.PodGroup{pg("large", "1", 2, 2), pg("small", "100", 1, 1)}, []*v1.Pod{pod("large", "large-0", 1, 60), pod("large", "large-1", 1, 60), pod("small", "small-0", 1, 100)}, false, false, vgpu.RFJobPolicy, 1, NewTracker(), func() time.Time { return base })
	defer closeFixture(f)
	f.run(t, "small-0")
	if f.ssn.Jobs["hrrn-test/large"].ReadyTaskNum() != 0 {
		t.Fatal("native Gang minimum violated")
	}
}

func TestNativeMultiGPUInsufficientAndCompleteSuccess(t *testing.T) {
	requireRF(t)
	for _, cards := range []int{1, 2} {
		t.Run(fmt.Sprint(cards), func(t *testing.T) {
			groups := []*schedulingv1.PodGroup{pg("multi", "10", 1, 1), pg("small", "100", 1, 1)}
			pods := []*v1.Pod{pod("multi", "multi-0", 2, 100), pod("small", "small-0", 1, 100)}
			f := setup(t, groups, pods, true, true, vgpu.RFJobPolicy, cards, NewTracker(), func() time.Time { return base })
			defer closeFixture(f)
			if cards == 1 {
				f.run(t, "small-0")
			} else {
				f.run(t, "multi-0")
				saved, _ := f.ssn.KubeClient().CoreV1().Pods("hrrn-test").Get(context.Background(), "multi-0", metav1.GetOptions{})
				ids := saved.Annotations[vgpu.AssignedIDsToAllocateAnnotations]
				if strings.Count(ids, "SIM-HRRN-GPU-0") != 1 || strings.Count(ids, "SIM-HRRN-GPU-1") != 1 {
					t.Fatal("incomplete or duplicate GPU UUIDs", ids)
				}
			}
		})
	}
}

func TestCompleteTwoPodsBindOnlyAfterWholePlanFits(t *testing.T) {
	requireRF(t)
	f := setup(t, []*schedulingv1.PodGroup{pg("pair", "10", 2, 1)}, []*v1.Pod{pod("pair", "pair-0", 1, 40), pod("pair", "pair-1", 1, 40)}, true, true, vgpu.RFJobPolicy, 1, NewTracker(), func() time.Time { return base })
	defer closeFixture(f)
	f.run(t, "pair-0", "pair-1")
	if f.gpus.Device[0].UsedCore != 80 || f.gpus.Device[0].UsedNum != 2 {
		t.Fatal("incomplete plan", f.gpus.Device[0])
	}
}

func TestNativeDiscardReleaseRetryAndDRFFairness(t *testing.T) {
	requireRF(t)
	groups := []*schedulingv1.PodGroup{pg("a", "1", 2, 2), pg("b", "100", 2, 2)}
	pods := []*v1.Pod{pod("a", "a-0", 1, 40), pod("a", "a-1", 1, 40), pod("b", "b-0", 1, 40), pod("b", "b-1", 1, 40)}
	tracker := NewTracker()
	warm := setup(t, groups, pods, true, true, vgpu.RFJobPolicy, 1, tracker, func() time.Time { return base })
	closeFixture(warm)
	f := setup(t, groups, pods, true, true, vgpu.RFJobPolicy, 1, tracker, func() time.Time { return base.Add(30 * time.Second) })
	defer closeFixture(f)
	a, b := f.ssn.Jobs["hrrn-test/a"], f.ssn.Jobs["hrrn-test/b"]
	var task *api.TaskInfo
	for _, x := range a.Tasks {
		if x.Name == "a-0" {
			task = x
		}
	}
	for round := 0; round < 2; round++ {
		if err := f.ssn.PrePredicateFn(task); err != nil {
			t.Fatal(err)
		}
		if err := f.ssn.PredicateFn(task, f.ssn.Nodes["node"]); err != nil {
			t.Fatal(err)
		}
		statement := framework.NewStatement(f.ssn)
		if err := statement.Allocate(task, f.ssn.Nodes["node"]); err != nil {
			t.Fatal(err)
		}
		if f.gpus.Device[0].UsedCore != 40 || f.gpus.Device[0].UsedNum != 1 {
			t.Fatal("allocation did not use native ledger")
		}
		if f.plugin.compare(a, b) >= 0 || !f.ssn.JobOrderFn(b, a) {
			t.Fatal("HRRN overrode DRF allocated share")
		}
		statement.Discard()
		if f.gpus.Device[0].UsedCore != 0 || f.gpus.Device[0].UsedMem != 0 || f.gpus.Device[0].UsedNum != 0 {
			t.Fatal("release leaked resources")
		}
		if task.Pod.Spec.NodeName != "" || task.Pod.Annotations[vgpu.AssignedIDsAnnotations] != "" {
			t.Fatal("retry retained assignment")
		}
	}
	f.run(t, "a-0", "a-1")
}

func TestNativeClosedQueueAndJobCancellationRecovery(t *testing.T) {
	requireRF(t)
	groups := []*schedulingv1.PodGroup{pg("job", "1", 1, 1)}
	pods := []*v1.Pod{pod("job", "job-0", 1, 100)}
	tracker := NewTracker()
	f := setup(t, groups, pods, true, true, vgpu.RFJobPolicy, 1, tracker, func() time.Time { return base })
	f.ssn.Queues["q"].Queue.Status.State = "Closed"
	f.run(t)
	closeFixture(f)
	// A cancelled job disappears from the next cache snapshot. No reservation
	// exists in HRRN; removing membership must prune readiness history too.
	empty := setup(t, nil, nil, true, true, vgpu.RFJobPolicy, 1, tracker, func() time.Time { return base.Add(time.Second) })
	if len(tracker.ready) != 0 {
		t.Fatal("cancelled job kept readiness history")
	}
	closeFixture(empty)
	groups[0].UID = "new-job-uid"
	f = setup(t, groups, pods, true, true, vgpu.RFJobPolicy, 1, tracker, func() time.Time { return base.Add(2 * time.Second) })
	defer closeFixture(f)
	if s, _ := f.plugin.Snapshot("hrrn-test/job"); s.WaitingSeconds != 0 {
		t.Fatal("recreated job inherited waiting")
	}
	f.run(t, "job-0")
}

func TestCompleteGuardRejectsUnsafeConfigAndBestEffort(t *testing.T) {
	if safeCompleteOrder([]conf.Tier{{Plugins: []conf.PluginOption{{Name: PluginName}, {Name: "predicates"}}}}) {
		t.Fatal("cleanup ordering unchecked")
	}
	if safeCompleteOrder([]conf.Tier{{Plugins: []conf.PluginOption{{Name: "predicates"}, {Name: PluginName, EnabledJobReady: ptr(false)}}}}) {
		t.Fatal("disabled JobReady accepted")
	}
	p := pod("job", "job-0", 1, 50)
	p.Spec.Containers[0].Resources = v1.ResourceRequirements{}
	f := setup(t, []*schedulingv1.PodGroup{pg("job", "1", 1, 1)}, []*v1.Pod{p}, true, true, vgpu.OriginalPolicy, 1, NewTracker(), func() time.Time { return base })
	defer closeFixture(f)
	if v := f.ssn.JobValid(f.ssn.Jobs["hrrn-test/job"]); v == nil || v.Pass {
		t.Fatal("BestEffort bypasses complete guard")
	}
	f.run(t)
}

func TestNativeGPUPlacementPolicyReference(t *testing.T) {
	requireRF(t)
	var records []map[string]interface{}
	for _, policy := range []string{vgpu.BinpackPolicy, vgpu.SpreadPolicy, vgpu.RFJobPolicy} {
		t.Run(policy, func(t *testing.T) {
			f := setup(t, []*schedulingv1.PodGroup{pg("job", "10", 1, 1)}, []*v1.Pod{pod("job", "job-0", 1, 20)}, false, false, policy, 2, NewTracker(), func() time.Time { return base })
			defer closeFixture(f)
			// Unequal existing logical allocations, not measured GPU performance.
			g := f.gpus.Device[0]
			g.UsedMem = 8192
			g.UsedCore = 40
			g.UsedNum = 2
			f.run(t, "job-0")
			saved, _ := f.ssn.KubeClient().CoreV1().Pods("hrrn-test").Get(context.Background(), "job-0", metav1.GetOptions{})
			ids := saved.Annotations[vgpu.AssignedIDsToAllocateAnnotations]
			if policy == vgpu.BinpackPolicy && !strings.Contains(ids, "SIM-HRRN-GPU-0") {
				t.Fatal("binpack did not pack", ids)
			}
			if policy == vgpu.SpreadPolicy && !strings.Contains(ids, "SIM-HRRN-GPU-1") {
				t.Fatal("spread did not spread", ids)
			}
			records = append(records, map[string]interface{}{"policy": policy, "boundPod": "job-0", "UUIDAnnotation": ids, "logicalUsedCores": []uint{f.gpus.Device[0].UsedCore, f.gpus.Device[1].UsedCore}, "tiers": f.tiers})
		})
	}
	if out := os.Getenv("HRRN_RESULTS"); out != "" {
		data, _ := json.MarshalIndent(records, "", "  ")
		if err := os.WriteFile(filepath.Join(out, "placement-comparisons.json"), data, 0644); err != nil {
			t.Fatal(err)
		}
	}
}

func TestPluginOffRestoresNativeAndGangStillBlocksMissingMembers(t *testing.T) {
	groups := []*schedulingv1.PodGroup{pg("missing", "1", 2, 2)}
	f := setup(t, groups, []*v1.Pod{pod("missing", "one-0", 1, 50)}, false, true, vgpu.OriginalPolicy, 1, NewTracker(), func() time.Time { return base })
	defer closeFixture(f)
	if len(f.plugin.snapshots) != 0 {
		t.Fatal("disabled plugin changed state")
	}
	if v := f.ssn.JobValid(f.ssn.Jobs["hrrn-test/missing"]); v == nil || v.Pass {
		t.Fatal("native gang bypassed")
	}
	f.run(t)
}
