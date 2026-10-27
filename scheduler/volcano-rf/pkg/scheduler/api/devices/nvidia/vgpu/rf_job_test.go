// Offline native allocator tests. No Kubernetes server or NVIDIA hardware used.
package vgpu

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	v1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes/fake"
	clienttesting "k8s.io/client-go/testing"
)

type rfTraceTransport struct {
	base http.RoundTripper
	mu   sync.Mutex
	rows []map[string]interface{}
}

func (rt *rfTraceTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	request, err := io.ReadAll(r.Body)
	if err != nil {
		return nil, err
	}
	_ = r.Body.Close()
	r.Body = io.NopCloser(bytes.NewReader(request))
	response, err := rt.base.RoundTrip(r)
	if err != nil {
		return nil, err
	}
	body, err := io.ReadAll(response.Body)
	_ = response.Body.Close()
	if err != nil {
		return nil, err
	}
	response.Body = io.NopCloser(bytes.NewReader(body))
	rt.mu.Lock()
	rt.rows = append(rt.rows, map[string]interface{}{"request": json.RawMessage(request), "response": json.RawMessage(body), "http_status": response.StatusCode})
	rt.mu.Unlock()
	return response, nil
}

func rfTestConfig(t *testing.T) {
	t.Helper()
	predictionEnabled := RFPredictionEnabled
	RFPredictionEnabled = true
	t.Cleanup(func() { RFPredictionEnabled = predictionEnabled })
	enabled, policy, vgpu, lock, endpoint, version, timeout, client := RFEnabled, GPUSelectPolicy, VGPUEnable, NodeLockEnable, RFEndpoint, RFModelVersion, RFTimeoutMS, rfHTTP
	RFEnabled, GPUSelectPolicy, VGPUEnable, NodeLockEnable = true, RFJobPolicy, true, false
	RFTimeoutMS = 1000
	rfCacheMu.Lock()
	rfPlans = map[string]*rfPlan{}
	rfCacheMu.Unlock()
	t.Cleanup(func() {
		RFEnabled, GPUSelectPolicy, VGPUEnable, NodeLockEnable = enabled, policy, vgpu, lock
		RFEndpoint, RFModelVersion, RFTimeoutMS, rfHTTP = endpoint, version, timeout, client
	})
}

func rfTestPod(name, task string, mem, cores int64) *v1.Pod {
	labels := map[string]string{}
	if task != "" {
		labels[RFWorkloadLabel] = task
	}
	return &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "rf-offline", UID: types.UID(name), Labels: labels}, Spec: v1.PodSpec{Containers: []v1.Container{{Name: "work", Image: "offline-never-launched", Resources: v1.ResourceRequirements{Limits: v1.ResourceList{
		"volcano.sh/vgpu-number": *resource.NewQuantity(1, resource.DecimalSI), "volcano.sh/vgpu-memory": *resource.NewQuantity(mem, resource.DecimalSI), "volcano.sh/vgpu-cores": *resource.NewQuantity(cores, resource.DecimalSI),
	}}}}}}
}

func rfTestNode(name, model string, count int, mem uint) *GPUDevices {
	gs := &GPUDevices{Name: name, Mode: vGPUControllerHAMICore, Device: map[int]*GPUDevice{}, Sharing: HAMICoreFactory{}}
	for i := 0; i < count; i++ {
		g := NewGPUDevice(i, mem)
		g.UUID = fmt.Sprintf("SIM-%s-GPU-%02d", name, i)
		g.Node = name
		g.Type = model
		g.Number = 10
		g.Health = true
		gs.Device[i] = g
	}
	return gs
}

func rfMockServer(t *testing.T, fn func(rfRequest) *rfResponse) *atomic.Int32 {
	t.Helper()
	calls := &atomic.Int32{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		var request rfRequest
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			t.Error(err)
			w.WriteHeader(400)
			return
		}
		// In the single-request test there can be no unrelated commit. Other
		// concurrent callers may legitimately hold this lock during our RPC.
		if request.Pod.Name == "train" {
			if !rfLedgerMu.TryLock() {
				t.Error("RPC holds logical-ledger lock")
			} else {
				rfLedgerMu.Unlock()
			}
		}
		response := fn(request)
		if response == nil {
			w.WriteHeader(503)
			return
		}
		_ = json.NewEncoder(w).Encode(response)
	}))
	t.Cleanup(server.Close)
	RFEndpoint = server.URL
	return calls
}

func rfMockResponse(r rfRequest) *rfResponse {
	a := []rfAssignment{}
	gs := rfTestNode(r.GPUs[0].Node, r.GPUs[0].Model, len(r.GPUs), r.GPUs[0].RegisteredMemory)
	reqs := rfContainerRequests(r.Pod)
	for _, c := range r.Pod.Spec.Containers {
		req, ok := reqs[c.Name]
		if !ok {
			continue
		}
		for u := 0; u < int(req.Nums); u++ {
			g := r.GPUs[u]
			req = rfResolve(gs.Device[u], req)
			a = append(a, rfAssignment{Container: c.Name, Unit: u, UUID: g.UUID, Node: g.Node, Memory: req.Memreq, Compute: req.Coresreq, Instances: 1})
		}
	}
	return &rfResponse{PredictionEnabled: r.PredictionEnabled, Schema: rfSchema, RequestID: r.RequestID, SnapshotToken: r.SnapshotToken, ModelVersion: r.ModelVersion, Status: "ranked", ModelReady: true, Score: 80, Assignments: a, Classifications: json.RawMessage(`[{"source":"mock-not-RF"}]`)}
}

func rfAssertEmpty(t *testing.T, gs *GPUDevices) {
	t.Helper()
	for _, g := range gs.Device {
		if g.UsedMem != 0 || g.UsedCore != 0 || g.UsedNum != 0 || len(g.PodMap) != 0 {
			t.Fatalf("ledger not restored: %+v", g)
		}
	}
}

func TestRFNativeAllocateUsesAdvisedUUIDAndNativeLedger(t *testing.T) {
	rfTestConfig(t)
	calls := rfMockServer(t, rfMockResponse)
	gs := rfTestNode("4090", "NVIDIA-GeForce RTX 4090", 2, 24564)
	pod := rfTestPod("train", "training", 4096, 30)
	client := fake.NewSimpleClientset(pod.DeepCopy())
	if _, _, err := gs.FilterNode(pod, "binpack"); err != nil {
		t.Fatal(err)
	}
	if gs.ScoreNode(pod, "binpack") != 80 {
		t.Fatal("RF score lost")
	}
	rfAssertEmpty(t, gs)
	plan, err := rfPlanFor(gs, pod)
	if err != nil || plan.Fallback != "" {
		t.Fatalf("%+v %v", plan, err)
	}
	if err = gs.Allocate(client, pod); err != nil {
		t.Fatal(err)
	}
	saved, err := client.CoreV1().Pods(pod.Namespace).Get(context.Background(), pod.Name, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	devices := decodePodDevices(saved.Annotations[AssignedIDsToAllocateAnnotations])
	uuid := devices[0][0].UUID
	if uuid != gs.Device[0].UUID || uuid != plan.Devices[0][0].UUID {
		t.Fatalf("RF/Allocate UUID mismatch %s", uuid)
	}
	if calls.Load() != 1 {
		t.Fatalf("Filter/Score/Allocate should reuse current-snapshot advice, got %d calls", calls.Load())
	}
	if gs.Device[0].UsedMem != 4096 || gs.Device[0].UsedCore != 30 || gs.Device[0].UsedNum != 1 || gs.Device[1].UsedNum != 0 {
		t.Fatal("wrong native accounting")
	}
	if rfGPUs(gs)[0].TrainingCount != 1 {
		t.Fatal("task composition missing")
	}
	gs.AddResource(saved)
	if gs.Device[0].UsedNum != 1 {
		t.Fatal("informer replay double-counted")
	}
	if err = gs.Allocate(client, pod); err == nil {
		t.Fatal("duplicate Allocate must not reserve twice")
	}
	gs.SubResource(saved)
	rfAssertEmpty(t, gs)
}

func TestRFEnableDisableAndMissingLabel(t *testing.T) {
	for _, tc := range []struct {
		name, task string
		enabled    bool
		wantID     int
		calls      int32
	}{{"enabled", "inference", true, 0, 1}, {"disabled", "training", false, 1, 0}, {"missing-label", "", true, 1, 0}} {
		t.Run(tc.name, func(t *testing.T) {
			rfTestConfig(t)
			calls := rfMockServer(t, rfMockResponse)
			RFEnabled = tc.enabled
			gs := rfTestNode("node", "NVIDIA-L40", 2, 46068)
			p := rfTestPod(tc.name, tc.task, 4096, 30)
			client := fake.NewSimpleClientset(p.DeepCopy())
			if err := gs.Allocate(client, p); err != nil {
				t.Fatal(err)
			}
			got, _ := client.CoreV1().Pods(p.Namespace).Get(context.Background(), p.Name, metav1.GetOptions{})
			if decodePodDevices(got.Annotations[AssignedIDsAnnotations])[0][0].UUID != gs.Device[tc.wantID].UUID {
				t.Fatal("unexpected UUID")
			}
			if calls.Load() != tc.calls {
				t.Fatal("unexpected RPC")
			}
		})
	}
}

func TestRFExceptionFallback(t *testing.T) {
	for _, name := range []string{"unavailable", "timeout", "wrong-version", "alien-uuid", "wrong-quota", "invalid-score", "partial-plan"} {
		t.Run(name, func(t *testing.T) {
			rfTestConfig(t)
			RFTimeoutMS = 30
			calls := rfMockServer(t, func(r rfRequest) *rfResponse {
				out := rfMockResponse(r)
				switch name {
				case "unavailable":
					return nil
				case "timeout":
					time.Sleep(70 * time.Millisecond)
				case "wrong-version":
					out.ModelVersion = "wrong"
				case "alien-uuid":
					out.Assignments[0].UUID = "GPU-not-present"
				case "wrong-quota":
					out.Assignments[0].Memory = 1
				case "invalid-score":
					out.Score = 101
				case "partial-plan":
					out.Assignments = nil
				}
				return out
			})
			gs := rfTestNode("node", "NVIDIA-L40", 2, 46068)
			p := rfTestPod(name, "training", 4096, 30)
			plan, err := rfPlanFor(gs, p)
			if err != nil || plan.Fallback == "" {
				t.Fatalf("fallback absent: %+v %v", plan, err)
			}
			if strings.Contains(plan.Fallback, "memory factor") {
				t.Fatalf("case %s did not exercise intended failure: %s", name, plan.Fallback)
			}
			want := map[string]string{"unavailable": "HTTP status 503", "timeout": "deadline", "wrong-version": "identity/version mismatch", "alien-uuid": "unknown GPU UUID", "wrong-quota": "native resource constraints", "invalid-score": "invalid score", "partial-plan": "incomplete assignment count"}[name]
			if !strings.Contains(plan.Fallback, want) || calls.Load() != 1 {
				t.Fatalf("case %s wrong branch: calls=%d reason=%s", name, calls.Load(), plan.Fallback)
			}
			if plan.Devices[0][0].UUID != gs.Device[1].UUID {
				t.Fatal("fallback must use native deterministic order")
			}
			if err = gs.Allocate(fake.NewSimpleClientset(p.DeepCopy()), p); err != nil {
				t.Fatal(err)
			}
			gs.SubResource(p)
			rfAssertEmpty(t, gs)
		})
	}
}

func TestRFRejectsImpossibleOrInvalidRequestsBeforeHTTP(t *testing.T) {
	for _, name := range []string{"memory", "compute", "instances", "health", "backend", "label", "mode", "multi-device"} {
		t.Run(name, func(t *testing.T) {
			rfTestConfig(t)
			calls := rfMockServer(t, rfMockResponse)
			gs := rfTestNode("node", "NVIDIA-L40", 1, 46068)
			p := rfTestPod(name, "training", 4096, 30)
			switch name {
			case "memory":
				gs.Device[0].UsedMem = 45000
			case "compute":
				gs.Device[0].UsedCore = 90
			case "instances":
				gs.Device[0].UsedNum = 10
			case "health":
				gs.Device[0].Health = false
			case "backend":
				p.Annotations = map[string]string{"gpu-lab/backend": "ROCm"}
			case "label":
				p.Labels[RFWorkloadLabel] = "auto"
			case "mode":
				p.Annotations = map[string]string{GPUModeAnnotation: "mig"}
			case "multi-device":
				p.Spec.Containers[0].Resources.Limits["volcano.sh/vgpu-number"] = resource.MustParse("2")
			}
			if _, _, err := gs.FilterNode(p, "binpack"); err == nil {
				t.Fatal("invalid/unfit Pod passed")
			}
			if calls.Load() != 0 {
				t.Fatal("unfit Pod sent to RF")
			}
			if err := gs.Allocate(fake.NewSimpleClientset(p.DeepCopy()), p); err == nil {
				t.Fatal("Allocate bypassed native constraint")
			}
		})
	}
}

func TestRFStaleStateDuringRPCAndRetry(t *testing.T) {
	rfTestConfig(t)
	gs := rfTestNode("node", "NVIDIA-L40", 1, 46068)
	first := true
	rfMockServer(t, func(r rfRequest) *rfResponse {
		if first {
			first = false
			rfLedgerMu.Lock()
			gs.Device[0].UsedCore = 10
			rfLedgerMu.Unlock()
		}
		return rfMockResponse(r)
	})
	p := rfTestPod("stale", "training", 4096, 30)
	client := fake.NewSimpleClientset(p.DeepCopy())
	if err := gs.Allocate(client, p); err == nil || !strings.Contains(err.Error(), "stale") {
		t.Fatalf("stale commit accepted: %v", err)
	}
	if gs.Device[0].UsedMem != 0 || gs.Device[0].UsedNum != 0 {
		t.Fatal("stale plan changed native ledger")
	}
	if err := gs.Allocate(client, p); err != nil {
		t.Fatal("fresh retry failed", err)
	}
	if gs.Device[0].UsedCore != 40 {
		t.Fatal("fresh snapshot lost existing quota")
	}
	gs.SubResource(p)
	if gs.Device[0].UsedCore != 10 {
		t.Fatal("release lost unrelated allocation")
	}
}

func TestRFPatchFailureRollsBackNativeLedger(t *testing.T) {
	rfTestConfig(t)
	rfMockServer(t, rfMockResponse)
	gs := rfTestNode("node", "NVIDIA-L40", 1, 46068)
	p := rfTestPod("rollback", "inference", 4096, 30)
	client := fake.NewSimpleClientset(p.DeepCopy())
	client.PrependReactor("patch", "pods", func(clienttesting.Action) (bool, runtime.Object, error) {
		return true, nil, fmt.Errorf("injected patch failure")
	})
	if gs.Allocate(client, p) == nil {
		t.Fatal("patch failure hidden")
	}
	rfAssertEmpty(t, gs)
}

func TestRFMultipleGPUsAndCPUContainer(t *testing.T) {
	rfTestConfig(t)
	rfMockServer(t, rfMockResponse)
	gs := rfTestNode("node", "NVIDIA-L40", 2, 46068)
	p := rfTestPod("multiple", "training", 4096, 30)
	p.Spec.Containers[0].Resources.Limits["volcano.sh/vgpu-number"] = resource.MustParse("2")
	p.Spec.Containers = append([]v1.Container{{Name: "cpu", Image: "never-launched"}}, p.Spec.Containers...)
	if err := gs.Allocate(fake.NewSimpleClientset(p.DeepCopy()), p); err != nil {
		t.Fatal(err)
	}
	d := decodePodDevices(p.Annotations[AssignedIDsAnnotations])
	if len(d) != 2 || len(d[0]) != 0 || len(d[1]) != 2 || d[1][0].UUID == d[1][1].UUID {
		t.Fatalf("container/UUID shape wrong: %+v", d)
	}
	for _, g := range gs.Device {
		if g.UsedMem != 4096 || g.UsedCore != 30 || g.UsedNum != 1 {
			t.Fatal("incorrect multi-device accounting")
		}
	}
	gs.SubResource(p)
	rfAssertEmpty(t, gs)
}

func TestRFPercentResolutionUsesRegisteredMiB(t *testing.T) {
	g := NewGPUDevice(0, 46068)
	r := rfResolve(g, ContainerDeviceRequest{Nums: 1, MemPercentagereq: 50})
	if r.Memreq != 23034 {
		t.Fatal("percentage must use native registered MiB")
	}
}

func TestRFQuantityWireContractAndInvalidInputs(t *testing.T) {
	rfTestConfig(t)
	gs := rfTestNode("node", "NVIDIA-L40", 1, 46068)
	p := rfTestPod("wire", "training", 10000, 65)
	r, err := rfRequestFor(gs, p)
	if err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(r)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(raw), `"volcano.sh/vgpu-memory":10000`) {
		t.Fatalf("quantity not normalized: %s", raw)
	}
	for _, q := range []string{"1.5", "-1"} {
		p.Spec.Containers[0].Resources.Limits["volcano.sh/vgpu-memory"] = resource.MustParse(q)
		if _, err = rfRequestFor(gs, p); err == nil {
			t.Fatal("invalid quantity accepted", q)
		}
	}
}

func TestRFConnectionRefusedFallback(t *testing.T) {
	rfTestConfig(t)
	server := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	RFEndpoint = server.URL
	server.Close()
	gs := rfTestNode("node", "NVIDIA-L40", 2, 46068)
	p := rfTestPod("disconnected", "training", 4096, 30)
	plan, err := rfPlanFor(gs, p)
	if err != nil || plan.Fallback == "" || plan.Devices[0][0].UUID != gs.Device[1].UUID {
		t.Fatalf("bad connection fallback: %+v %v", plan, err)
	}
}

func TestRFConcurrentPodsNeverOvercommit(t *testing.T) {
	rfTestConfig(t)
	rfMockServer(t, rfMockResponse)
	gs := rfTestNode("node", "NVIDIA-L40", 1, 512)
	gs.Device[0].Number = 2
	client := fake.NewSimpleClientset()
	var wg sync.WaitGroup
	var mu sync.Mutex
	allocated := []*v1.Pod{}
	for i := 0; i < 20; i++ {
		p := rfTestPod(fmt.Sprintf("parallel-%02d", i), "inference", 256, 50)
		_, _ = client.CoreV1().Pods(p.Namespace).Create(context.Background(), p, metav1.CreateOptions{})
		wg.Add(1)
		go func() {
			defer wg.Done()
			if gs.Allocate(client, p) == nil {
				mu.Lock()
				allocated = append(allocated, p)
				mu.Unlock()
			}
		}()
	}
	wg.Wait()
	if len(allocated) == 0 || len(allocated) > 2 {
		t.Fatalf("wrong success count %d", len(allocated))
	}
	g := gs.Device[0]
	if g.UsedNum != uint(len(allocated)) || g.UsedMem > 512 || g.UsedCore > 100 || g.UsedNum > 2 {
		t.Fatal("overcommit/inconsistent native ledger")
	}
	for _, p := range allocated {
		gs.SubResource(p)
	}
	rfAssertEmpty(t, gs)
}

// Run with RF_LIVE_ENDPOINT set to the actual resident Python HTTP server.
// Uses production FilterNode/ScoreNode/Allocate, native HAMICoreFactory and a
// client-go fake API server. This is NOT a real Kubernetes cluster deployment.
func TestRFLivePythonNativeExperiments(t *testing.T) {
	endpoint := os.Getenv("RF_LIVE_ENDPOINT")
	if endpoint == "" {
		t.Skip("set RF_LIVE_ENDPOINT for real Go-Python integration")
	}
	rfTestConfig(t)
	RFEndpoint = endpoint
	RFTimeoutMS = 3000
	trace := &rfTraceTransport{base: rfHTTP.Transport}
	if trace.base == nil {
		trace.base = http.DefaultTransport
	}
	clientCopy := *rfHTTP
	clientCopy.Transport = trace
	rfHTTP = &clientCopy
	fleet := func() []*GPUDevices {
		return []*GPUDevices{rfTestNode("sim-4090", "NVIDIA-NVIDIA GeForce RTX 4090", 4, 24564), rfTestNode("sim-5090", "NVIDIA-NVIDIA GeForce RTX 5090", 4, 32607), rfTestNode("sim-l40", "NVIDIA-NVIDIA L40", 3, 46068)}
	}
	type decision struct {
		Stage           string                   `json:"stage"`
		Task            string                   `json:"task"`
		Node            string                   `json:"node"`
		RFUUID          string                   `json:"recommended_uuid"`
		Actual          string                   `json:"allocate_annotation_uuid"`
		Fallback        string                   `json:"fallback"`
		Score           float64                  `json:"score"`
		Before          []rfGPU                  `json:"before"`
		After           []rfGPU                  `json:"after"`
		Classifications json.RawMessage          `json:"classifications"`
		Candidates      []map[string]interface{} `json:"candidate_nodes"`
	}
	decisions := []decision{}
	schedule := func(nodes []*GPUDevices, p *v1.Pod, stage string) (*GPUDevices, *v1.Pod) {
		t.Helper()
		client := fake.NewSimpleClientset(p.DeepCopy())
		var chosen *GPUDevices
		var best *rfPlan
		candidates := []map[string]interface{}{}
		for _, n := range nodes {
			if _, _, err := n.FilterNode(p, "binpack"); err != nil {
				continue
			}
			plan, err := rfPlanFor(n, p)
			if err != nil {
				t.Fatal(err)
			}
			score := n.ScoreNode(p, "binpack")
			candidates = append(candidates, map[string]interface{}{"node": n.Name, "score": score, "plan": plan, "ledger": rfGPUs(n)})
			if chosen == nil || score > best.Score {
				chosen, best = n, plan
			}
		}
		if chosen == nil {
			t.Fatal("no feasible node")
		}
		before := rfGPUs(chosen)
		if err := chosen.Allocate(client, p); err != nil {
			t.Fatal(err)
		}
		saved, err := client.CoreV1().Pods(p.Namespace).Get(context.Background(), p.Name, metav1.GetOptions{})
		if err != nil {
			t.Fatal(err)
		}
		actual := decodePodDevices(saved.Annotations[AssignedIDsToAllocateAnnotations])[0][0].UUID
		if best.Fallback != "" {
			t.Fatalf("real RF was not used: %s", best.Fallback)
		}
		if actual != best.Devices[0][0].UUID {
			t.Fatal("real RF/native Allocate UUID mismatch")
		}
		decisions = append(decisions, decision{Stage: stage, Task: p.Labels[RFWorkloadLabel], Node: chosen.Name, RFUUID: best.Devices[0][0].UUID, Actual: actual, Score: best.Score, Before: before, After: rfGPUs(chosen), Classifications: best.Diagnostics, Candidates: candidates})
		return chosen, saved
	}
	// A: both task types request IDENTICAL resources, every one of 11 GPUs fits.
	baseline := []map[string]interface{}{}
	for _, task := range []string{"training", "inference"} {
		nodes := fleet()
		p := rfTestPod("baseline-"+task, task, 4096, 30)
		RFEnabled = false
		fit, dev, _, err := checkNodeGPUSharingPredicateAndScore(p, nodes[0], true, "")
		if err != nil || !fit {
			t.Fatal(err)
		}
		client := fake.NewSimpleClientset(p.DeepCopy())
		if err = nodes[0].Allocate(client, p); err != nil {
			t.Fatal(err)
		}
		saved, _ := client.CoreV1().Pods(p.Namespace).Get(context.Background(), p.Name, metav1.GetOptions{})
		baseline = append(baseline, map[string]interface{}{"task": task, "policy": "original node-score tie, stable node order", "planned_uuid": dev[0][0].UUID, "actual_uuid": decodePodDevices(saved.Annotations[AssignedIDsAnnotations])[0][0].UUID})
		RFEnabled = true
		nodes = fleet()
		for _, n := range nodes {
			for _, g := range n.Device {
				for _, r := range rfContainerRequests(p) {
					if !rfFits(p, g, r) {
						t.Fatal("A must not rely on resource rejection")
					}
				}
			}
		}
		schedule(nodes, rfTestPod("rf-"+task, task, 4096, 30), "A-"+task)
	}
	// B: native ledger changes, subsequent requests use a new snapshot/cache key.
	nodes := fleet()
	nA, pA := schedule(nodes, rfTestPod("sequence-A", "training", 10000, 65), "B-allocate-A")
	_, _ = schedule(nodes, rfTestPod("sequence-B", "inference", 6000, 25), "B-allocate-B")
	beforeRelease := rfGPUs(nA)
	nA.SubResource(pA)
	afterRelease := rfGPUs(nA)
	_, _ = schedule(nodes, rfTestPod("sequence-C", "training", 4096, 30), "B-after-release-A")
	// A controlled single-GPU transition demonstrates reclassification independent
	// of fleet placement. The model may return pending; its output is not forced.
	one := rfTestNode("sim-single-l40", "NVIDIA-NVIDIA L40", 1, 46068)
	transitions := []map[string]interface{}{}
	probe := rfTestPod("probe", "inference", 100, 1)
	capture := func(stage string) {
		q, e := rfRequestFor(one, probe)
		if e != nil {
			t.Fatal(e)
		}
		resp, e := rfCall(q)
		if e != nil {
			t.Fatal(e)
		}
		transitions = append(transitions, map[string]interface{}{"stage": stage, "ledger": rfGPUs(one), "classification": json.RawMessage(resp.Classifications), "score": resp.Score})
	}
	capture("initial")
	_, a := schedule([]*GPUDevices{one}, rfTestPod("compute-heavy", "training", 4000, 65), "B-single-compute")
	capture("after-compute")
	_, b := schedule([]*GPUDevices{one}, rfTestPod("memory-heavy", "inference", 34000, 20), "B-single-memory")
	capture("after-memory")
	one.SubResource(a)
	capture("release-compute")
	one.SubResource(b)
	capture("release-all")
	rfAssertEmpty(t, one)
	out := map[string]interface{}{"evidence_level": "real Go-Python HTTP + native Volcano GPUDevices Allocate + fake Kubernetes client; NOT live cluster", "model_version": RFModelVersion, "synthetic_only": true, "baseline": baseline, "decisions": decisions, "release_A_before": beforeRelease, "release_A_after": afterRelease, "dynamic_transitions": transitions}
	if dir := os.Getenv("RF_DEMO_OUTPUT"); dir != "" {
		if err := os.MkdirAll(dir, 0755); err != nil {
			t.Fatal(err)
		}
		data, _ := json.MarshalIndent(out, "", "  ")
		if err := os.WriteFile(filepath.Join(dir, "native-experiments.json"), data, 0644); err != nil {
			t.Fatal(err)
		}
		exchanges, _ := json.MarshalIndent(trace.rows, "", "  ")
		if err := os.WriteFile(filepath.Join(dir, "rf-http-exchanges.json"), exchanges, 0644); err != nil {
			t.Fatal(err)
		}
	}
}
