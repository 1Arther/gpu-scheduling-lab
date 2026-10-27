package vgpu

// RF advice is stateless. The native Volcano ledger remains the only resource
// owner. All memory fields below are logical quota units (MiB in this MVP).
import (
	"bytes"
	"context"
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"sync"
	"time"
	"unicode"

	v1 "k8s.io/api/core/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/klog/v2"
	"volcano.sh/volcano/pkg/scheduler/plugins/util/nodelock"
)

const RFJobPolicy = "rf-job"
const RFWorkloadLabel = "gpu-lab/workload-type"
const rfSchema = "rf-job/v1"

//go:embed rf_profiles.json
var rfProfilesJSON []byte
var rfProfileOnce sync.Once
var rfProfiles map[string]struct {
	Hardware map[string]interface{} `json:"hardware"`
}

func rfHardware(model string) map[string]interface{} {
	rfProfileOnce.Do(func() { _ = json.Unmarshal(rfProfilesJSON, &rfProfiles) })
	parts := strings.FieldsFunc(strings.ToUpper(model), func(r rune) bool { return !unicode.IsLetter(r) && !unicode.IsDigit(r) })
	key := ""
	for _, p := range parts {
		if p != "NVIDIA" && p != "GEFORCE" {
			key += p
		}
	}
	return rfProfiles[key].Hardware // nil for unknown/unverified SKU, never guessed
}

var (
	RFEnabled      = false
	RFEndpoint     = "http://127.0.0.1:18081/v1/rank"
	RFModelVersion = "rf-v4-known-states-ee35cfabe30f"
	RFTimeoutMS    = 500
	rfLedgerMu     sync.RWMutex
	rfCacheMu      sync.Mutex
	rfPlans        = map[string]*rfPlan{}
	rfHTTP         = &http.Client{Transport: &http.Transport{Proxy: nil}, CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return fmt.Errorf("RF redirects disabled") }}
)

func RFActive() bool { return VGPUEnable && RFEnabled && GPUSelectPolicy == RFJobPolicy }

type rfGPU struct {
	UUID             string                 `json:"uuid"`
	Node             string                 `json:"node"`
	Model            string                 `json:"model"`
	RegisteredMemory uint                   `json:"registered_memory_mib"`
	AllocatedMemory  uint                   `json:"allocated_memory_mib"`
	AllocatedCompute uint                   `json:"allocated_compute_quota"`
	AllocatedCount   uint                   `json:"allocated_vgpu_count"`
	TrainingCount    uint                   `json:"training_vgpu_count"`
	InferenceCount   uint                   `json:"inference_vgpu_count"`
	TrainingPods     uint                   `json:"training_pod_count"`
	InferencePods    uint                   `json:"inference_pod_count"`
	Unrestricted     uint                   `json:"unrestricted_vgpu_count"`
	MaxInstances     uint                   `json:"max_instances"`
	Healthy          bool                   `json:"healthy"`
	Mode             string                 `json:"sharing_mode"`
	Hardware         map[string]interface{} `json:"hardware,omitempty"`
}
type rfRequest struct {
	Schema        string  `json:"schema_version"`
	RequestID     string  `json:"request_id"`
	SnapshotToken string  `json:"snapshot_token"`
	ModelVersion  string  `json:"model_version"`
	MemoryUnitMiB int     `json:"memory_unit_mib"`
	Pod           *v1.Pod `json:"pod"`
	GPUs          []rfGPU `json:"gpus"`
}

// Kubernetes quantities may marshal 10000 as "10k". The RPC contract uses
// integer MiB/quota units, not Kubernetes SI syntax. Canonicalize explicitly.
func (r rfRequest) MarshalJSON() ([]byte, error) {
	type alias rfRequest
	raw, err := json.Marshal(r.Pod)
	if err != nil {
		return nil, err
	}
	var pod map[string]interface{}
	if err = json.Unmarshal(raw, &pod); err != nil {
		return nil, err
	}
	if r.Pod != nil {
		spec, _ := pod["spec"].(map[string]interface{})
		containers, _ := spec["containers"].([]interface{})
		for i, c := range r.Pod.Spec.Containers {
			if i >= len(containers) {
				continue
			}
			cm := containers[i].(map[string]interface{})
			resources, _ := cm["resources"].(map[string]interface{})
			for name, list := range map[string]v1.ResourceList{"limits": c.Resources.Limits, "requests": c.Resources.Requests} {
				lm, _ := resources[name].(map[string]interface{})
				for key, q := range list {
					if strings.HasPrefix(string(key), "volcano.sh/vgpu-") {
						n, ok := q.AsInt64()
						if !ok || n < 0 {
							return nil, fmt.Errorf("non-integer/negative vGPU quantity")
						}
						lm[string(key)] = n
					}
				}
			}
		}
	}
	return json.Marshal(struct {
		*alias
		Pod map[string]interface{} `json:"pod"`
	}{alias: (*alias)(&r), Pod: pod})
}

type rfAssignment struct {
	Container string `json:"container"`
	Unit      int    `json:"unit"`
	UUID      string `json:"gpu_uuid"`
	Node      string `json:"node"`
	Memory    uint   `json:"memory_mib"`
	Compute   uint   `json:"compute_quota"`
	Instances int    `json:"instances"`
}
type rfResponse struct {
	Schema          string          `json:"schema_version"`
	RequestID       string          `json:"request_id"`
	SnapshotToken   string          `json:"snapshot_token"`
	ModelVersion    string          `json:"model_version"`
	Status          string          `json:"status"`
	ModelReady      bool            `json:"model_ready"`
	Score           float64         `json:"score"`
	Assignments     []rfAssignment  `json:"assignments"`
	Classifications json.RawMessage `json:"classifications"`
}
type rfPlan struct {
	Created       time.Time
	RequestID     string
	SnapshotToken string
	Devices       []ContainerDevices
	Score         float64
	Fallback      string
	Diagnostics   json.RawMessage
}

func rfHash(v interface{}) string {
	b, _ := json.Marshal(v)
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}

// Metadata is rebuilt from native PodMap/addResource events, not from a Python
// reservation service. Counts are allocated vGPU slices, plus distinct Pods.
func rfRecordPod(g *GPUDevice, pod *v1.Pod, core uint, sign int) {
	if !RFActive() {
		return
	}
	u := g.PodMap[string(pod.UID)]
	if u == nil {
		return
	}
	u.RFWorkloadType = pod.Labels[RFWorkloadLabel]
	if sign > 0 {
		u.RFInstances++
		if core == 0 {
			u.RFUnrestrictedInstances++
		}
	} else if u.RFInstances > 0 {
		u.RFInstances--
		if core == 0 && u.RFUnrestrictedInstances > 0 {
			u.RFUnrestrictedInstances--
		}
	}
}

func rfClone(gs *GPUDevices) *GPUDevices {
	copy := getGPUDeviceSnapShot(gs)
	copy.Mode = gs.Mode
	for idx, g := range gs.Device {
		if g == nil {
			continue
		}
		copy.Device[idx].PodMap = make(map[string]*GPUUsage)
		for uid, usage := range g.PodMap {
			if usage == nil {
				continue
			}
			u := *usage
			copy.Device[idx].PodMap[uid] = &u
		}
	}
	return copy
}

func rfGPUs(gs *GPUDevices) []rfGPU {
	rows := []rfGPU{}
	for _, g := range gs.Device {
		if g == nil {
			continue
		}
		r := rfGPU{UUID: g.UUID, Node: gs.Name, Model: g.Type, RegisteredMemory: g.Memory, AllocatedMemory: g.UsedMem,
			AllocatedCompute: g.UsedCore, AllocatedCount: g.UsedNum, MaxInstances: g.Number, Healthy: g.Health, Mode: gs.Mode}
		r.Hardware = rfHardware(g.Type)
		for _, u := range g.PodMap {
			if u == nil {
				continue
			}
			switch u.RFWorkloadType {
			case "training":
				r.TrainingCount += u.RFInstances
				if u.RFInstances > 0 {
					r.TrainingPods++
				}
			case "inference":
				r.InferenceCount += u.RFInstances
				if u.RFInstances > 0 {
					r.InferencePods++
				}
			}
			r.Unrestricted += u.RFUnrestrictedInstances
		}
		rows = append(rows, r)
	}
	sort.Slice(rows, func(i, j int) bool { return rows[i].UUID < rows[j].UUID })
	return rows
}

func rfSnapshotToken(gs *GPUDevices) string { return rfHash(rfGPUs(gs)) }

func rfOriginalScore(gs *GPUDevices, plan *rfPlan, schedulePolicy string) float64 {
	rfLedgerMu.RLock()
	snap := rfClone(gs)
	rfLedgerMu.RUnlock()
	if rfSnapshotToken(snap) != plan.SnapshotToken {
		return 0
	}
	score := 0.0
	for _, list := range plan.Devices {
		for _, d := range list {
			for _, g := range snap.Device {
				if g.UUID == d.UUID {
					_, _ = snap.Sharing.TryAddPod(g, d.Usedmem, d.Usedcores)
					score += GPUScore(schedulePolicy, g)
					break
				}
			}
		}
	}
	return score
}

func rfPodRequest(pod *v1.Pod) (*v1.Pod, error) {
	cfg := getConfig()
	task, present := pod.Labels[RFWorkloadLabel]
	if present && task != "training" && task != "inference" {
		return nil, fmt.Errorf("invalid %s=%q", RFWorkloadLabel, task)
	}
	if backend := pod.Annotations["gpu-lab/backend"]; backend != "" && backend != "CUDA" {
		return nil, fmt.Errorf("unsupported RF backend %q", backend)
	}
	for _, c := range pod.Spec.InitContainers {
		for _, list := range []v1.ResourceList{c.Resources.Limits, c.Resources.Requests} {
			for key := range list {
				if strings.HasPrefix(string(key), "volcano.sh/vgpu-") || string(key) == cfg.ResourceCountName || string(key) == cfg.ResourceMemoryName || string(key) == cfg.ResourceCoreName {
					return nil, fmt.Errorf("GPU initContainers unsupported by rf-job MVP")
				}
			}
		}
	}
	copy := pod.DeepCopy()
	copy.Kind, copy.APIVersion = "Pod", "v1"
	// These are already handled by native predicates. Do not approximate them in
	// the GPU-only adviser, or discard the native scheduler's decisions.
	copy.Spec.Affinity = nil
	copy.Spec.TopologySpreadConstraints = nil
	copy.Spec.NodeSelector = nil
	if cfg.ResourceMemoryPercentageName == "" {
		for _, c := range pod.Spec.Containers {
			for _, list := range []v1.ResourceList{c.Resources.Limits, c.Resources.Requests} {
				if _, ok := list["volcano.sh/vgpu-memory-percentage"]; ok {
					return nil, fmt.Errorf("native memory-percentage resource name not configured")
				}
			}
		}
	}
	keys := map[v1.ResourceName]v1.ResourceName{v1.ResourceName(cfg.ResourceCountName): "volcano.sh/vgpu-number",
		v1.ResourceName(cfg.ResourceMemoryName): "volcano.sh/vgpu-memory", v1.ResourceName(cfg.ResourceCoreName): "volcano.sh/vgpu-cores",
		v1.ResourceName(cfg.ResourceMemoryPercentageName): "volcano.sh/vgpu-memory-percentage"}
	for idx := range copy.Spec.Containers {
		for _, list := range []v1.ResourceList{copy.Spec.Containers[idx].Resources.Limits, copy.Spec.Containers[idx].Resources.Requests} {
			for old, canonical := range keys {
				if value, ok := list[old]; ok {
					delete(list, old)
					list[canonical] = value
				}
			}
		}
		c := copy.Spec.Containers[idx]
		for _, list := range []v1.ResourceList{c.Resources.Limits, c.Resources.Requests} {
			for key, q := range list {
				if !strings.HasPrefix(string(key), "volcano.sh/vgpu-") {
					continue
				}
				n, ok := q.AsInt64()
				if !ok || n < 0 {
					return nil, fmt.Errorf("vGPU resource must be nonnegative integer units: %s", key)
				}
				if key == "volcano.sh/vgpu-number" && (n < 1 || n > 64) {
					return nil, fmt.Errorf("invalid vGPU count")
				}
				if key == "volcano.sh/vgpu-cores" && n > 100 {
					return nil, fmt.Errorf("vGPU compute quota exceeds 100")
				}
				if key == "volcano.sh/vgpu-memory-percentage" && (n < 1 || n > 100) {
					return nil, fmt.Errorf("invalid vGPU memory percentage")
				}
				if l, yes := c.Resources.Limits[key]; yes {
					if req, yes := c.Resources.Requests[key]; yes && l.Cmp(req) != 0 {
						return nil, fmt.Errorf("vGPU requests/limits disagree")
					}
				}
			}
		}
	}
	return copy, nil
}

func rfRequestFor(gs *GPUDevices, pod *v1.Pod) (rfRequest, error) {
	p, err := rfPodRequest(pod)
	if err != nil {
		return rfRequest{}, err
	}
	p.Spec.NodeName = gs.Name
	r := rfRequest{Schema: rfSchema, ModelVersion: RFModelVersion, MemoryUnitMiB: 1, Pod: p, GPUs: rfGPUs(gs), SnapshotToken: rfSnapshotToken(gs)}
	r.RequestID = rfHash(r)
	return r, nil
}

func rfCall(r rfRequest) (rfResponse, error) {
	var result rfResponse
	u, err := url.Parse(RFEndpoint)
	if err != nil || u.Scheme != "http" || u.Host == "" || u.User != nil {
		return result, fmt.Errorf("invalid RF HTTP endpoint")
	}
	body, err := json.Marshal(r)
	if err != nil {
		return result, err
	}
	timeout := RFTimeoutMS
	if timeout < 1 || timeout > 10000 {
		timeout = 500
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(timeout)*time.Millisecond)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, RFEndpoint, bytes.NewReader(body))
	if err != nil {
		return result, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := rfHTTP.Do(req)
	if err != nil {
		return result, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		diagnostic, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return result, fmt.Errorf("RF HTTP status %d: %s", resp.StatusCode, diagnostic)
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, 4*1024*1024+1))
	if err != nil {
		return result, err
	}
	if len(data) > 4*1024*1024 {
		return result, fmt.Errorf("RF response too large")
	}
	if err = json.Unmarshal(data, &result); err != nil {
		return result, err
	}
	if result.Schema != rfSchema || result.RequestID != r.RequestID || result.SnapshotToken != r.SnapshotToken || result.ModelVersion != r.ModelVersion {
		return result, fmt.Errorf("RF response identity/version mismatch")
	}
	if result.Status != "ranked" || !result.ModelReady || math.IsNaN(result.Score) || math.IsInf(result.Score, 0) || result.Score < 0 || result.Score > 100 {
		return result, fmt.Errorf("RF response unavailable/invalid score")
	}
	return result, nil
}

func rfResolve(g *GPUDevice, req ContainerDeviceRequest) ContainerDeviceRequest {
	if req.Memreq == 0 && req.MemPercentagereq != DefaultMemPercentage {
		req.Memreq = g.Memory * uint(req.MemPercentagereq) / 100
	}
	return req
}

func rfFits(pod *v1.Pod, g *GPUDevice, req ContainerDeviceRequest) bool {
	return g != nil && g.UUID != "" && g.Memory > 0 && g.Number > 0 && g.Health && g.UsedMem <= g.Memory && g.UsedCore <= 100 && req.Nums > 0 && req.Coresreq <= 100 && rfResolve(g, req).Memreq > 0 && canFitForOrder(g, rfResolve(g, req)) && checkType(pod.Annotations, *g, req)
}

func rfContainerRequests(pod *v1.Pod) map[string]ContainerDeviceRequest {
	result := map[string]ContainerDeviceRequest{}
	for _, c := range pod.Spec.Containers {
		p := pod.DeepCopy()
		p.Spec.Containers = []v1.Container{c}
		reqs := resourcereqs(p)
		if len(reqs) > 0 {
			result[c.Name] = reqs[0]
		}
	}
	return result
}

// Validate every returned UUID/slot/request against a fresh native clone. The
// model can rank feasible devices but cannot relax quotas or select alien UUIDs.
func rfValidate(gs *GPUDevices, pod *v1.Pod, assignments []rfAssignment) ([]ContainerDevices, error) {
	working := rfClone(gs)
	reqs := rfContainerRequests(pod)
	devices := make([]ContainerDevices, len(pod.Spec.Containers))
	byUUID := map[string]int{}
	for idx, g := range working.Device {
		if g != nil {
			byUUID[g.UUID] = idx
		}
	}
	slots := map[string]bool{}
	used := map[string]bool{}
	expected := 0
	for _, r := range reqs {
		if r.Nums < 1 || r.Coresreq > 100 || r.MemPercentagereq < 0 {
			return nil, fmt.Errorf("invalid GPU request")
		}
		expected += int(r.Nums)
	}
	if len(assignments) != expected {
		return nil, fmt.Errorf("RF incomplete assignment count")
	}
	for _, a := range assignments {
		r, ok := reqs[a.Container]
		if !ok || a.Unit < 0 || a.Unit >= int(r.Nums) || a.Node != gs.Name || a.Instances != 1 {
			return nil, fmt.Errorf("RF invalid container/unit/node")
		}
		slot := fmt.Sprintf("%s/%d", a.Container, a.Unit)
		pair := a.Container + "/" + a.UUID
		if slots[slot] || used[pair] {
			return nil, fmt.Errorf("RF duplicate slot or per-container UUID")
		}
		slots[slot] = true
		used[pair] = true
		idx, ok := byUUID[a.UUID]
		if !ok {
			return nil, fmt.Errorf("RF unknown GPU UUID")
		}
		g := working.Device[idx]
		r = rfResolve(g, r)
		if a.Memory != r.Memreq || a.Compute != r.Coresreq || !rfFits(pod, g, r) {
			return nil, fmt.Errorf("RF UUID violates native resource constraints")
		}
		fit, uuid := working.Sharing.TryAddPod(g, r.Memreq, r.Coresreq)
		if !fit || uuid != a.UUID {
			return nil, fmt.Errorf("RF native TryAddPod UUID mismatch")
		}
		for i, c := range pod.Spec.Containers {
			if c.Name == a.Container {
				devices[i] = append(devices[i], ContainerDevice{UUID: uuid, Type: r.Type, Usedmem: r.Memreq, Usedcores: r.Coresreq})
			}
		}
	}
	return devices, nil
}

func rfFallback(gs *GPUDevices, pod *v1.Pod, original bool) ([]rfAssignment, float64, error) {
	working := rfClone(gs)
	reqs := rfContainerRequests(pod)
	result := []rfAssignment{}
	score := 0.0
	for _, c := range pod.Spec.Containers {
		r, ok := reqs[c.Name]
		if !ok {
			continue
		}
		used := map[int]bool{}
		for unit := 0; unit < int(r.Nums); unit++ {
			indexes := []int{}
			for idx, g := range working.Device {
				if !used[idx] && rfFits(pod, g, r) {
					indexes = append(indexes, idx)
				}
			}
			if len(indexes) == 0 {
				return nil, 0, fmt.Errorf("no complete native binpack plan")
			}
			sort.Slice(indexes, func(i, j int) bool {
				a, b := working.Device[indexes[i]], working.Device[indexes[j]]
				if original {
					return a.ID > b.ID
				}
				if a.UsedMem != b.UsedMem {
					return a.UsedMem > b.UsedMem
				}
				if a.UsedCore != b.UsedCore {
					return a.UsedCore > b.UsedCore
				}
				if a.UsedNum != b.UsedNum {
					return a.UsedNum > b.UsedNum
				}
				return a.ID > b.ID
			})
			idx := indexes[0]
			g := working.Device[idx]
			resolved := rfResolve(g, r)
			fit, uuid := working.Sharing.TryAddPod(g, resolved.Memreq, resolved.Coresreq)
			if !fit || uuid != g.UUID {
				return nil, 0, fmt.Errorf("fallback native UUID mismatch")
			}
			used[idx] = true
			result = append(result, rfAssignment{Container: c.Name, Unit: unit, UUID: uuid, Node: gs.Name, Memory: resolved.Memreq, Compute: resolved.Coresreq, Instances: 1})
			score += 100 * (1 - (.5*float64(g.Memory-g.UsedMem)/float64(g.Memory) + .4*float64(100-g.UsedCore)/100 + .1*float64(g.Number-g.UsedNum)/float64(g.Number)))
		}
	}
	if len(result) == 0 {
		return result, 0, nil
	}
	return result, score / float64(len(result)), nil
}

func rfPlanFor(gs *GPUDevices, pod *v1.Pod) (*rfPlan, error) {
	rfLedgerMu.RLock()
	snap := rfClone(gs)
	rfLedgerMu.RUnlock()
	if snap.Mode != vGPUControllerHAMICore {
		return nil, fmt.Errorf("rf-job MVP supports hami-core only")
	}
	if mode, ok := pod.Annotations[GPUModeAnnotation]; ok && mode != snap.Mode {
		return nil, fmt.Errorf("RF sharing mode mismatch")
	}
	if pod.Spec.NodeName != "" && pod.Spec.NodeName != snap.Name {
		return nil, fmt.Errorf("RF native node binding mismatch")
	}
	r, err := rfRequestFor(snap, pod)
	if err != nil {
		return nil, err
	}
	// Native feasibility comes first, before any RPC. Only candidates that fit
	// at least one requested container are exposed to the adviser.
	original := pod.Labels[RFWorkloadLabel] == ""
	fallbackAssignments, fallbackScore, err := rfFallback(snap, pod, original)
	if err != nil {
		return nil, err
	}
	reqs := rfContainerRequests(pod)
	eligible := map[string]bool{}
	for _, g := range snap.Device {
		for _, req := range reqs {
			if rfFits(pod, g, req) {
				eligible[g.UUID] = true
			}
		}
	}
	filtered := []rfGPU{}
	for _, g := range r.GPUs {
		if eligible[g.UUID] {
			filtered = append(filtered, g)
		}
	}
	r.GPUs = filtered
	r.RequestID = ""
	r.RequestID = rfHash(r)
	rfCacheMu.Lock()
	cached := rfPlans[r.RequestID]
	rfCacheMu.Unlock()
	ttl := 30 * time.Second
	if cached != nil && cached.Fallback != "" {
		ttl = 500 * time.Millisecond
	}
	if cached != nil && time.Since(cached.Created) < ttl {
		return cached, nil
	}
	plan := &rfPlan{Created: time.Now(), RequestID: r.RequestID, SnapshotToken: r.SnapshotToken}
	var response rfResponse
	if original {
		err = fmt.Errorf("missing workload label: original deterministic ordering")
	} else if getConfig().GPUMemoryFactor > 1 {
		err = fmt.Errorf("RF MVP requires memory factor=1")
	} else {
		response, err = rfCall(r)
	}
	if err == nil {
		plan.Devices, err = rfValidate(snap, pod, response.Assignments)
		plan.Score = response.Score
		plan.Diagnostics = response.Classifications
	}
	if err != nil {
		plan.Fallback = err.Error()
		plan.Devices, err = rfValidate(snap, pod, fallbackAssignments)
		if err != nil {
			return nil, err
		}
		plan.Score = fallbackScore
		klog.V(3).InfoS("rf-job deterministic fallback", "pod", pod.Name, "node", gs.Name, "reason", plan.Fallback)
	}
	rfCacheMu.Lock()
	if len(rfPlans) >= 512 {
		rfPlans = map[string]*rfPlan{}
	}
	rfPlans[r.RequestID] = plan
	rfCacheMu.Unlock()
	return plan, nil
}

func rfAlreadyAllocated(gs *GPUDevices, pod *v1.Pod) bool {
	for _, g := range gs.Device {
		if g != nil {
			if u := g.PodMap[string(pod.UID)]; u != nil && (u.UsedMem > 0 || u.UsedCore > 0 || u.RFInstances > 0) {
				return true
			}
		}
	}
	return false
}

func (gs *GPUDevices) allocateRF(client kubernetes.Interface, pod *v1.Pod) error {
	rfLedgerMu.RLock()
	present := rfAlreadyAllocated(gs, pod)
	rfLedgerMu.RUnlock()
	if present {
		return fmt.Errorf("rf-job Pod already accounted; refusing duplicate Allocate")
	}
	plan, err := rfPlanFor(gs, pod)
	if err != nil {
		return err
	}
	rfLedgerMu.Lock()
	defer rfLedgerMu.Unlock()
	if rfSnapshotToken(gs) != plan.SnapshotToken || rfAlreadyAllocated(gs, pod) {
		return fmt.Errorf("rf-job stale plan; retry scheduling")
	}
	// A plan is never an authority: recheck native UUIDs and quotas at commit.
	assignments := []rfAssignment{}
	for i, list := range plan.Devices {
		for unit, d := range list {
			assignments = append(assignments, rfAssignment{Container: pod.Spec.Containers[i].Name, Unit: unit, UUID: d.UUID, Node: gs.Name, Memory: d.Usedmem, Compute: d.Usedcores, Instances: 1})
		}
	}
	devices, err := rfValidate(gs, pod, assignments)
	if err != nil {
		return err
	}
	if NodeLockEnable {
		nodelock.UseClient(client)
		if err = nodelock.LockNode(gs.Name, DeviceName); err != nil {
			return err
		}
	}
	annotations := map[string]string{AssignedNodeAnnotations: gs.Name, AssignedTimeAnnotations: fmt.Sprint(time.Now().Unix()), AssignedIDsAnnotations: encodePodDevices(devices),
		AssignedIDsToAllocateAnnotations: encodePodDevices(devices), DeviceBindPhase: "allocating", BindTimeAnnotations: fmt.Sprint(time.Now().Unix())}
	gs.addResource(annotations, pod)
	if err = patchPodAnnotations(client, pod, annotations); err != nil {
		copy := pod.DeepCopy()
		copy.Annotations = annotations
		gs.subResource(copy)
		if NodeLockEnable {
			_ = nodelock.ReleaseNodeLock(gs.Name, DeviceName)
		}
		return err
	}
	if pod.Annotations == nil {
		pod.Annotations = map[string]string{}
	}
	for key, value := range annotations {
		pod.Annotations[key] = value
	}
	klog.InfoS("rf-job UUID allocation", "pod", pod.Namespace+"/"+pod.Name, "node", gs.Name, "requestID", plan.RequestID, "devices", annotations[AssignedIDsAnnotations], "score", plan.Score, "fallback", plan.Fallback)
	return nil
}
