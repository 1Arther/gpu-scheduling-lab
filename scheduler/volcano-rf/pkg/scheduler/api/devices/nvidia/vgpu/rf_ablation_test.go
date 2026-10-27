// Paired causal mechanism checks, using production Filter/Score/Allocate.
package vgpu

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"sort"
	"testing"

	v1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"
)

type ablState struct {
	ID      string `json:"id"`
	Free    *uint  `json:"free_memory_mib"`
	Compute uint   `json:"free_compute_quota"`
}
type ablNode struct {
	Name     string     `json:"name"`
	Model    string     `json:"model"`
	Capacity uint       `json:"capacity_mib"`
	States   []ablState `json:"states"`
}
type ablCase struct {
	ID      string    `json:"id"`
	Family  string    `json:"family"`
	Task    string    `json:"task"`
	Memory  int64     `json:"memory_mib"`
	Compute int64     `json:"compute_quota"`
	Nodes   []ablNode `json:"nodes"`
}
type ablScore struct {
	Score         float64 `json:"score"`
	Packing       float64 `json:"packing_residual"`
	Feasible      bool    `json:"feasible"`
	Basis         string  `json:"ranking_basis"`
	Decomposition struct {
		RF           float64 `json:"applied_rf_contribution"`
		Potential    float64 `json:"potential_rf_contribution"`
		Heuristic    float64 `json:"heuristic_contribution"`
		Conservative float64 `json:"conservative_fallback_contribution"`
	} `json:"score_decomposition"`
}
type ablDiagnostic struct {
	UUID           string                 `json:"gpu_uuid"`
	Classification map[string]interface{} `json:"classification"`
	Scores         map[string]ablScore    `json:"request_specific_scores"`
}
type ablCandidate struct {
	UUID               string                 `json:"gpu_uuid"`
	Node               string                 `json:"node"`
	Model              string                 `json:"model"`
	Classification     map[string]interface{} `json:"classification"`
	RF                 float64                `json:"rf_contribution"`
	PotentialRF        float64                `json:"potential_rf_contribution"`
	Heuristic          float64                `json:"heuristic_contribution"`
	Conservative       float64                `json:"conservative_fallback_contribution"`
	Score              float64                `json:"final_score"`
	NodeScore          float64                `json:"node_score"`
	Rounded            float64                `json:"rounded_node_score"`
	Packing            float64                `json:"packing_residual"`
	DeviceRank         int                    `json:"device_rank"`
	NodeRank           int                    `json:"node_rank"`
	GlobalRank         int                    `json:"global_rank"`
	FinalUUID          string                 `json:"final_allocate_uuid"`
	Feasible           bool                   `json:"native_hard_fit"`
	ClassificationUsed bool                   `json:"classification_used_by_strategy"`
}
type ablTrial struct {
	Case         string         `json:"case_id"`
	Family       string         `json:"family"`
	Task         string         `json:"task"`
	Strategy     string         `json:"strategy"`
	PodHash      string         `json:"pod_hash"`
	SnapshotHash string         `json:"snapshot_hash"`
	RestoredHash string         `json:"restored_hash"`
	UUID         string         `json:"allocate_uuid"`
	Node         string         `json:"allocate_node"`
	Recommended  string         `json:"recommended_uuid"`
	Candidates   []ablCandidate `json:"candidates"`
	Before       []rfGPU        `json:"before"`
	After        []rfGPU        `json:"after"`
}

func ablSnapshots(nodes []*GPUDevices) []rfGPU {
	out := []rfGPU{}
	for _, n := range nodes {
		out = append(out, rfGPUs(n)...)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].UUID < out[j].UUID })
	return out
}
func ablClone(nodes []*GPUDevices) []*GPUDevices {
	result := []*GPUDevices{}
	for _, n := range nodes {
		result = append(result, rfClone(n))
	}
	return result
}

func ablSeed(c ablCase) []*GPUDevices {
	RFEnabled, GPUSelectPolicy, RFPredictionEnabled = true, RFJobPolicy, true
	nodes := []*GPUDevices{}
	for _, spec := range c.Nodes {
		n := rfTestNode(spec.Name, spec.Model, len(spec.States), spec.Capacity)
		for i, s := range spec.States {
			if s.Free == nil {
				continue
			}
			g := n.Device[i]
			p := rfTestPod(fmt.Sprintf("seed-%s-%02d", spec.Name, i), "training", int64(g.Memory-*s.Free), int64(100-s.Compute))
			p.Annotations = map[string]string{AssignedIDsAnnotations: encodePodDevices([]ContainerDevices{{{UUID: g.UUID, Type: NvidiaGPUDevice, Usedmem: g.Memory - *s.Free, Usedcores: 100 - s.Compute}}})}
			n.AddResource(p)
		}
		nodes = append(nodes, n)
	}
	return nodes
}

func ablMode(mode string) {
	RFEnabled = true
	GPUSelectPolicy = RFJobPolicy
	RFPredictionEnabled = mode != "rf_off"
	if mode == "binpack" {
		RFEnabled = false
		GPUSelectPolicy = BinpackPolicy
	}
}

func ablTrialRun(t *testing.T, c ablCase, seed []*GPUDevices, p *v1.Pod, mode string, prior map[string]map[string]interface{}) ablTrial {
	t.Helper()
	nodes := ablClone(seed)
	ablMode(mode)
	result := ablTrial{Case: c.ID, Family: c.Family, Task: c.Task, Strategy: mode, PodHash: rfHash(p), Before: ablSnapshots(nodes)}
	result.SnapshotHash = rfHash(result.Before)
	type nodePlan struct {
		Node       *GPUDevices
		Score      float64
		UUID       string
		Candidates []ablCandidate
	}
	plans := []nodePlan{}
	for _, n := range nodes {
		req := rfContainerRequests(p)[p.Spec.Containers[0].Name]
		for _, g := range n.Device {
			if !rfFits(p, g, req) {
				t.Fatalf("primary ablation must not use filtering: %s %s", c.ID, g.UUID)
			}
		}
		if _, _, e := n.FilterNode(p, "binpack"); e != nil {
			t.Fatal(e)
		}
		score := n.ScoreNode(p, "binpack")
		np := nodePlan{Node: n, Score: score}
		if mode != "binpack" {
			plan, e := rfPlanFor(n, p)
			if e != nil || plan.Fallback != "" {
				t.Fatalf("RF communication/plan failed: %v %+v", e, plan)
			}
			np.UUID = plan.Devices[0][0].UUID
			ds := []ablDiagnostic{}
			if e = json.Unmarshal(plan.Diagnostics, &ds); e != nil {
				t.Fatal(e)
			}
			for _, d := range ds {
				s := d.Scores[c.Task]
				np.Candidates = append(np.Candidates, ablCandidate{UUID: d.UUID, Node: n.Name, Model: n.Device[0].Type, Classification: d.Classification, RF: 100 * s.Decomposition.RF, PotentialRF: 100 * s.Decomposition.Potential, Heuristic: 100 * s.Decomposition.Heuristic, Conservative: 100 * s.Decomposition.Conservative, Score: 100 * s.Score, Packing: s.Packing, NodeScore: score, Rounded: math.Floor(score + .5), Feasible: s.Feasible, ClassificationUsed: true})
			}
			sort.Slice(np.Candidates, func(i, j int) bool {
				a, b := np.Candidates[i], np.Candidates[j]
				if a.Score != b.Score {
					return a.Score > b.Score
				}
				if a.Packing != b.Packing {
					return a.Packing < b.Packing
				}
				return a.UUID < b.UUID
			})
		} else {
			fit, dev, _, e := checkNodeGPUSharingPredicateAndScore(p, n, true, "binpack")
			if e != nil || !fit {
				t.Fatal(e)
			}
			np.UUID = dev[0][0].UUID
			for _, i := range orderedGPUIndexes(n, p, req) {
				g := n.Device[i]
				copy := *g
				_, _ = n.Sharing.TryAddPod(&copy, req.Memreq, req.Coresreq)
				s := GPUScore("binpack", &copy)
				np.Candidates = append(np.Candidates, ablCandidate{UUID: g.UUID, Node: n.Name, Model: g.Type, Classification: prior[g.UUID], Heuristic: s, Score: s, NodeScore: score, Rounded: math.Floor(score + .5), Feasible: true})
			}
		}
		if len(np.Candidates) != len(n.Device) || np.Candidates[0].UUID != np.UUID {
			t.Fatal("reported rank differs from native selected UUID")
		}
		for i := range np.Candidates {
			np.Candidates[i].DeviceRank = i + 1
		}
		plans = append(plans, np)
	}
	sort.Slice(plans, func(i, j int) bool {
		a, b := plans[i], plans[j]
		sa, sb := math.Floor(a.Score+.5), math.Floor(b.Score+.5)
		if sa != sb {
			return sa > sb
		}
		return a.Node.Name < b.Node.Name
	})
	chosen := plans[0]
	client := fake.NewSimpleClientset(p.DeepCopy())
	if e := chosen.Node.Allocate(client, p); e != nil {
		t.Fatal(e)
	}
	saved, e := client.CoreV1().Pods(p.Namespace).Get(context.Background(), p.Name, metav1.GetOptions{})
	if e != nil {
		t.Fatal(e)
	}
	actual := decodePodDevices(saved.Annotations[AssignedIDsToAllocateAnnotations])[0][0].UUID
	if actual != chosen.UUID {
		t.Fatal("native Allocate UUID differs from scored GPU")
	}
	result.UUID, result.Node, result.Recommended = actual, chosen.Node.Name, chosen.UUID
	result.After = ablSnapshots(nodes)
	for i, g := range result.After {
		before := result.Before[i]
		delta := uint(0)
		if g.UUID == actual {
			delta = 1
		}
		if g.AllocatedMemory != before.AllocatedMemory+delta*uint(c.Memory) || g.AllocatedCompute != before.AllocatedCompute+delta*uint(c.Compute) || g.AllocatedCount != before.AllocatedCount+delta {
			t.Fatal("native ledger delta wrong")
		}
		if g.AllocatedMemory > g.RegisteredMemory || g.AllocatedCompute > 100 || g.AllocatedCount > g.MaxInstances {
			t.Fatal("native overcommit")
		}
	}
	rank := 0
	for nodeRank, np := range plans {
		for _, candidate := range np.Candidates {
			rank++
			candidate.NodeRank = nodeRank + 1
			candidate.GlobalRank = rank
			candidate.FinalUUID = actual
			result.Candidates = append(result.Candidates, candidate)
		}
	}
	chosen.Node.SubResource(saved)
	result.RestoredHash = rfHash(ablSnapshots(nodes))
	if result.RestoredHash != result.SnapshotHash {
		t.Fatal("native release did not restore exact snapshot")
	}
	return result
}

func TestRFLinuxExecutionPlatform(t *testing.T) {
	if os.Getenv("RF_REQUIRE_LINUX") == "" {
		t.Skip("Linux requirement checked by dedicated runner")
	}
	if runtime.GOOS != "linux" {
		t.Fatalf("actual Linux execution required, got %s", runtime.GOOS)
	}
	t.Logf("ACTUAL_RUNTIME=%s/%s go=%s", runtime.GOOS, runtime.GOARCH, runtime.Version())
}

func TestRFLivePredictionTermAblation(t *testing.T) {
	path := os.Getenv("RF_ABLATION_PLAN")
	if path == "" {
		t.Skip("set frozen RF_ABLATION_PLAN")
	}
	rfTestConfig(t)
	RFEndpoint = os.Getenv("RF_LIVE_ENDPOINT")
	RFTimeoutMS = 10000
	bytes, e := os.ReadFile(path)
	if e != nil {
		t.Fatal(e)
	}
	var plan struct {
		Cases []ablCase `json:"cases"`
	}
	if e = json.Unmarshal(bytes, &plan); e != nil {
		t.Fatal(e)
	}
	trials := []ablTrial{}
	for _, c := range plan.Cases {
		t.Run(c.ID, func(t *testing.T) {
			seed := ablSeed(c)
			p := rfTestPod("paired-"+c.ID, c.Task, c.Memory, c.Compute)
			on := ablTrialRun(t, c, seed, p.DeepCopy(), "rf_on", nil)
			prior := map[string]map[string]interface{}{}
			byUUID := map[string]ablCandidate{}
			for _, r := range on.Candidates {
				prior[r.UUID] = r.Classification
				byUUID[r.UUID] = r
			}
			off := ablTrialRun(t, c, seed, p.DeepCopy(), "rf_off", nil)
			if on.PodHash != off.PodHash || on.SnapshotHash != off.SnapshotHash {
				t.Fatal("paired inputs differ")
			}
			for _, b := range off.Candidates {
				a := byUUID[b.UUID]
				if !reflect.DeepEqual(a.Classification, b.Classification) || !a.Feasible || !b.Feasible {
					t.Fatal("predictions or admission changed under switch")
				}
				if math.Abs(a.Heuristic-b.Heuristic) > 1e-9 || math.Abs(a.Conservative-b.Conservative) > 1e-9 || b.RF != 0 || math.Abs((a.Score-b.Score)-a.RF) > 1e-9 {
					t.Fatal("ablation altered something beyond RF term")
				}
			}
			base := ablTrialRun(t, c, seed, p.DeepCopy(), "binpack", prior)
			if base.SnapshotHash != on.SnapshotHash || base.PodHash != on.PodHash {
				t.Fatal("binpack used different input")
			}
			trials = append(trials, on, off, base)
		})
	}
	if dir := os.Getenv("RF_ABLATION_OUTPUT"); dir != "" {
		if e = os.MkdirAll(dir, 0755); e != nil {
			t.Fatal(e)
		}
		data, _ := json.MarshalIndent(map[string]interface{}{"runtime_os": runtime.GOOS, "runtime_arch": runtime.GOARCH, "go_version": runtime.Version(), "plan_sha256": fmt.Sprintf("%x", sha256.Sum256(bytes)), "trials": trials, "real_kubernetes_client": false, "supervision": "synthetic"}, "", "  ")
		if e = os.WriteFile(filepath.Join(dir, "native-ablation.json"), data, 0644); e != nil {
			t.Fatal(e)
		}
	}
}

func TestRFLiveAblationSequentialLedger(t *testing.T) {
	if os.Getenv("RF_ABLATION_PLAN") == "" {
		t.Skip("dedicated live runner")
	}
	rfTestConfig(t)
	RFEndpoint = os.Getenv("RF_LIVE_ENDPOINT")
	RFTimeoutMS = 10000
	cap := uint(16384)
	low := uint(10240)
	c := ablCase{ID: "sequence", Family: "dynamic_followup", Task: "training", Memory: 4096, Compute: 25, Nodes: []ablNode{{Name: "seq-l40", Model: "NVIDIA-NVIDIA L40", Capacity: 46068, States: []ablState{{ID: "q40", Free: &cap, Compute: 40}, {ID: "q80", Free: &low, Compute: 80}, {ID: "empty", Compute: 100}}}}}
	seed := ablSeed(c)
	initial := rfHash(ablSnapshots(seed))
	records := []map[string]interface{}{}
	commit := func(name, task string, mem, cores int64) (*GPUDevices, *v1.Pod) {
		t.Helper()
		c.ID = name
		c.Task = task
		c.Memory = mem
		c.Compute = cores
		p := rfTestPod(name, task, mem, cores)
		on := ablTrialRun(t, c, seed, p.DeepCopy(), "rf_on", nil)
		off := ablTrialRun(t, c, seed, p.DeepCopy(), "rf_off", nil)
		records = append(records, map[string]interface{}{"stage": name, "on": on, "off": off})
		ablMode("rf_on")
		var n *GPUDevices
		for _, x := range seed {
			if x.Name == on.Node {
				n = x
			}
		}
		client := fake.NewSimpleClientset(p.DeepCopy())
		if e := n.Allocate(client, p); e != nil {
			t.Fatal(e)
		}
		saved, e := client.CoreV1().Pods(p.Namespace).Get(context.Background(), p.Name, metav1.GetOptions{})
		if e != nil {
			t.Fatal(e)
		}
		return n, saved
	}
	nA, a := commit("pod-A", "training", 4096, 25)
	nB, b := commit("pod-B", "inference", 1024, 10)
	before := ablSnapshots(seed)
	nA.SubResource(a)
	records = append(records, map[string]interface{}{"stage": "release-A", "before": before, "after": ablSnapshots(seed)})
	nC, cPod := commit("pod-C", "training", 4096, 25)
	nB.SubResource(b)
	nC.SubResource(cPod)
	if rfHash(ablSnapshots(seed)) != initial {
		t.Fatal("sequential full release failed")
	}
	if dir := os.Getenv("RF_ABLATION_OUTPUT"); dir != "" {
		data, _ := json.MarshalIndent(map[string]interface{}{"records": records, "all_resources_restored": true, "final": ablSnapshots(seed)}, "", "  ")
		if e := os.WriteFile(filepath.Join(dir, "sequential-ledger.json"), data, 0644); e != nil {
			t.Fatal(e)
		}
	}
}

func TestRFLiveAblationFilterControl(t *testing.T) {
	if os.Getenv("RF_ABLATION_PLAN") == "" {
		t.Skip("dedicated live runner")
	}
	rfTestConfig(t)
	RFEndpoint = os.Getenv("RF_LIVE_ENDPOINT")
	RFTimeoutMS = 10000
	n := rfTestNode("filter-control", "NVIDIA-NVIDIA GeForce RTX 4090", 2, 24564)
	seed := rfTestPod("filter-existing", "training", 24000, 90)
	seed.Annotations = map[string]string{AssignedIDsAnnotations: encodePodDevices([]ContainerDevices{{{UUID: n.Device[0].UUID, Type: NvidiaGPUDevice, Usedmem: 24000, Usedcores: 90}}})}
	n.AddResource(seed)
	p := rfTestPod("filter-control", "training", 4096, 25)
	uuids := []string{}
	for _, enabled := range []bool{true, false} {
		RFPredictionEnabled = enabled
		copy := rfClone(n)
		if _, _, e := copy.FilterNode(p, "binpack"); e != nil {
			t.Fatal(e)
		}
		client := fake.NewSimpleClientset(p.DeepCopy())
		if e := copy.Allocate(client, p); e != nil {
			t.Fatal(e)
		}
		saved, _ := client.CoreV1().Pods(p.Namespace).Get(context.Background(), p.Name, metav1.GetOptions{})
		uuid := decodePodDevices(saved.Annotations[AssignedIDsAnnotations])[0][0].UUID
		if uuid != n.Device[1].UUID {
			t.Fatal("model bypassed resource filter")
		}
		uuids = append(uuids, uuid)
		copy.SubResource(saved)
		if rfHash(rfGPUs(copy)) != rfHash(rfGPUs(n)) {
			t.Fatal("filter control release failed")
		}
	}
	if dir := os.Getenv("RF_ABLATION_OUTPUT"); dir != "" {
		data, _ := json.MarshalIndent(map[string]interface{}{"excluded_uuid": n.Device[0].UUID, "reason": "native logical memory and compute insufficient", "rf_on_allocate_uuid": uuids[0], "rf_off_allocate_uuid": uuids[1], "excluded_from_primary_ablation_counts": true}, "", "  ")
		if e := os.WriteFile(filepath.Join(dir, "filter-control.json"), data, 0644); e != nil {
			t.Fatal(e)
		}
	}
}

// Explicit coverage control, separate from the frozen 180-case denominator.
// Unknown existing workload composition is a real guard in the frozen runtime;
// we never force pending status or alter its evidence threshold/model weights.
func TestRFLiveAblationPending5090Control(t *testing.T) {
	if os.Getenv("RF_ABLATION_PLAN") == "" {
		t.Skip("dedicated live runner")
	}
	rfTestConfig(t)
	RFEndpoint = os.Getenv("RF_LIVE_ENDPOINT")
	RFTimeoutMS = 10000
	records := []ablTrial{}
	for _, cause := range []string{"unlabelled", "unrestricted"} {
		n := rfTestNode("pending-5090-"+cause, "NVIDIA-NVIDIA GeForce RTX 5090", 4, 32607)
		label, cores := "", uint(20)
		if cause == "unrestricted" {
			label, cores = "training", 0 // native zero quota means unrestricted, not zero real utilization
		}
		for i, g := range n.Device {
			p := rfTestPod(fmt.Sprintf("existing-%s-%d", cause, i), label, 4096, int64(cores))
			p.Annotations = map[string]string{AssignedIDsAnnotations: encodePodDevices([]ContainerDevices{{{UUID: g.UUID, Type: NvidiaGPUDevice, Usedmem: 4096, Usedcores: cores}}})}
			n.AddResource(p)
		}
		for _, task := range []string{"training", "inference"} {
			c := ablCase{ID: "pending-" + cause + "-5090-" + task, Family: "explicit_pending_control", Task: task, Memory: 4096, Compute: 25}
			pod := rfTestPod(c.ID, task, c.Memory, c.Compute)
			on := ablTrialRun(t, c, []*GPUDevices{n}, pod.DeepCopy(), "rf_on", nil)
			off := ablTrialRun(t, c, []*GPUDevices{n}, pod.DeepCopy(), "rf_off", nil)
			if on.UUID != off.UUID || on.SnapshotHash != off.SnapshotHash || on.PodHash != off.PodHash {
				t.Fatal("pending control changed inputs/UUID under RF switch")
			}
			byUUID := map[string]ablCandidate{}
			for _, c := range off.Candidates {
				byUUID[c.UUID] = c
			}
			for _, a := range on.Candidates {
				b := byUUID[a.UUID]
				if a.Classification["prediction_status"] != "pending_verification" {
					t.Fatal("expected actual pending guard", a.Classification)
				}
				if cause == "unrestricted" && a.Classification["raw_rf"] == nil {
					t.Fatal("unrestricted guard should retain diagnostic RF prediction")
				}
				if !reflect.DeepEqual(a.Classification, b.Classification) || a.RF != 0 || b.RF != 0 || a.Score != b.Score || a.Heuristic != b.Heuristic || a.Conservative != b.Conservative {
					t.Fatal("RF affected conservative pending ranking")
				}
			}
			records = append(records, on, off)
		}
	}
	if dir := os.Getenv("RF_ABLATION_OUTPUT"); dir != "" {
		data, _ := json.MarshalIndent(map[string]interface{}{"trials": records, "causes": []string{"existing Pod lacks gpu-lab/workload-type; composition unknown, raw RF may be null", "existing Pod has zero compute quota; unrestricted sharing retains diagnostic RF but falls back"}, "status_forced_by_test": false, "excluded_from_primary_ablation_counts": true}, "", "  ")
		if e := os.WriteFile(filepath.Join(dir, "pending-control.json"), data, 0644); e != nil {
			t.Fatal(e)
		}
	}
}
