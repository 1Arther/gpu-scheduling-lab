package coordination

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"sort"
	"strconv"
	"strings"

	"google.golang.org/protobuf/encoding/protowire"
	devicecheckpoint "k8s.io/kubernetes/pkg/kubelet/cm/devicemanager/checkpoint"
)

const maxCheckpointBytes = 32 << 20

// CheckpointReceipt is a decoded, checksum-verified disk record. It is NOT a
// live device-manager query, a plugin-authenticated UID-bound response, or an
// acknowledgement that a device remains allocated. The caller must separately
// establish source identity, freshness, Pod binding, runtime state, no in-flight
// requests and NodeLock ownership. Kubelet's checksum is not an authenticity
// signature. Persistent disk entries can outlive the Pod. No method here
// advances the coordinator or releases a lock.
type CheckpointReceipt struct {
	PodUID, Container, Resource string
	CheckpointSHA256            string
	ResponseSHA256              string // hash of decoded protobuf bytes
	VisibleUUIDs                []string
	MemoryLimits                []string
	CoreLimit                   string
	RegisteredSlotIDs           []string
}

func hashBytes(data []byte) string {
	digest := sha256.Sum256(data)
	return hex.EncodeToString(digest[:])
}

// protoBytesFields reads a protobuf message without importing or executing its
// payload. The only known field requested below is a length-delimited field;
// unknown fields are skipped after their wire encoding has been validated.
func protoBytesFields(raw []byte, wanted protowire.Number) ([][]byte, error) {
	var result [][]byte
	for len(raw) > 0 {
		number, kind, n := protowire.ConsumeTag(raw)
		if n < 0 {
			return nil, protowire.ParseError(n)
		}
		raw = raw[n:]
		if number == wanted {
			if kind != protowire.BytesType {
				return nil, fmt.Errorf("unexpected wire type for field %d", wanted)
			}
			value, size := protowire.ConsumeBytes(raw)
			if size < 0 {
				return nil, protowire.ParseError(size)
			}
			result = append(result, value)
			raw = raw[size:]
			continue
		}
		size := protowire.ConsumeFieldValue(number, kind, raw)
		if size < 0 {
			return nil, protowire.ParseError(size)
		}
		raw = raw[size:]
	}
	return result, nil
}

func checkpointResponseEnvs(raw []byte) (map[string]string, error) {
	entries, err := protoBytesFields(raw, 1) // ContainerAllocateResponse.envs
	if err != nil {
		return nil, err
	}
	envs := make(map[string]string, len(entries))
	for _, entry := range entries {
		keys, err := protoBytesFields(entry, 1)
		if err != nil || len(keys) != 1 || len(keys[0]) == 0 {
			return nil, fmt.Errorf("invalid AllocateResponse environment key")
		}
		values, err := protoBytesFields(entry, 2)
		if err != nil || len(values) != 1 {
			return nil, fmt.Errorf("invalid AllocateResponse environment value")
		}
		key := string(keys[0])
		if _, duplicate := envs[key]; duplicate {
			return nil, fmt.Errorf("duplicate AllocateResponse environment key")
		}
		envs[key] = string(values[0])
	}
	return envs, nil
}

// InspectKubeletCheckpoint matches a single Pod UID/container/vGPU instance
// resource in a supplied kubelet checkpoint, validates kubelet's checksum and
// the complete registration of its unique virtual device IDs, then decodes
// NVIDIA_VISIBLE_DEVICES and quota environment from its AllocateResponse.
// It never performs a filesystem, Kubernetes, kubelet or plugin call.
// Kubelet format/checksum compatibility is tied to the linked dependency;
// an unsupported or corrupt format fails closed and needs explicit validation.
func InspectKubeletCheckpoint(raw []byte, podUID, container, resource string) (*CheckpointReceipt, error) {
	if len(raw) == 0 || len(raw) > maxCheckpointBytes || podUID == "" || container == "" || resource == "" {
		return nil, fmt.Errorf("invalid checkpoint or incomplete identity")
	}
	cp := devicecheckpoint.New(nil, nil)
	if err := cp.UnmarshalCheckpoint(raw); err != nil {
		return nil, fmt.Errorf("invalid kubelet checkpoint: %w", err)
	}
	if err := cp.VerifyChecksum(); err != nil {
		return nil, fmt.Errorf("kubelet checkpoint checksum: %w", err)
	}
	entries, registered := cp.GetData()
	var selected *devicecheckpoint.PodDevicesEntry
	for i := range entries {
		e := &entries[i]
		if e.PodUID == podUID && e.ContainerName == container && e.ResourceName == resource {
			if selected != nil {
				return nil, fmt.Errorf("duplicate Pod/container/resource checkpoint entries")
			}
			selected = e
		}
	}
	if selected == nil || len(selected.AllocResp) == 0 {
		return nil, fmt.Errorf("missing Pod/container/resource allocation response")
	}
	registeredIDs := map[string]bool{}
	for _, id := range registered[resource] {
		if id == "" || registeredIDs[id] {
			return nil, fmt.Errorf("invalid or duplicate registered device ID")
		}
		registeredIDs[id] = true
	}
	seen := map[string]bool{}
	var ids []string
	for _, perNode := range selected.DeviceIDs {
		for _, id := range perNode {
			if id == "" || seen[id] || !registeredIDs[id] {
				return nil, fmt.Errorf("duplicate, empty or unregistered allocated device ID")
			}
			seen[id] = true
			ids = append(ids, id)
		}
	}
	if len(ids) == 0 {
		return nil, fmt.Errorf("empty virtual device allocation")
	}
	sort.Strings(ids) // NUMA map iteration order is not stable across processes.
	envs, err := checkpointResponseEnvs(selected.AllocResp)
	if err != nil {
		return nil, fmt.Errorf("invalid plugin AllocateResponse: %w", err)
	}
	visible := strings.Split(envs["NVIDIA_VISIBLE_DEVICES"], ",")
	if len(visible) != len(ids) {
		return nil, fmt.Errorf("visible UUID count differs from allocated instances")
	}
	for key := range envs {
		const prefix = "CUDA_DEVICE_MEMORY_LIMIT_"
		if strings.HasPrefix(key, prefix) {
			suffix := strings.TrimPrefix(key, prefix)
			index, err := strconv.Atoi(suffix)
			if err != nil || index < 0 || index >= len(visible) || strconv.Itoa(index) != suffix {
				return nil, fmt.Errorf("unexpected per-device memory limit")
			}
		}
	}
	seenUUID := map[string]bool{}
	limits := make([]string, len(visible))
	for i, uuid := range visible {
		if uuid == "" || uuid != strings.TrimSpace(uuid) || seenUUID[uuid] {
			return nil, fmt.Errorf("invalid or duplicate visible UUID")
		}
		seenUUID[uuid] = true
		limits[i] = envs["CUDA_DEVICE_MEMORY_LIMIT_"+strconv.Itoa(i)]
		if limits[i] == "" {
			return nil, fmt.Errorf("missing per-device memory limit")
		}
	}
	if envs["CUDA_DEVICE_SM_LIMIT"] == "" {
		return nil, fmt.Errorf("missing core limit")
	}
	return &CheckpointReceipt{PodUID: podUID, Container: container, Resource: resource,
		CheckpointSHA256: hashBytes(raw), ResponseSHA256: hashBytes(selected.AllocResp),
		VisibleUUIDs: visible, MemoryLimits: limits, CoreLimit: envs["CUDA_DEVICE_SM_LIMIT"],
		RegisteredSlotIDs: ids}, nil
}

// Matches checks the recorded response against one container's ordered plan.
// CUDA_* quantities use the audited hami-core plugin's logical MiB/cores
// convention. Different core limits in one container are rejected because the
// audited plugin emits only one SM limit. A match is not a live-consumption ACK.
func (r *CheckpointReceipt) Matches(plan []Allocation) error {
	if r == nil || len(plan) == 0 || len(plan) != len(r.VisibleUUIDs) || len(r.MemoryLimits) != len(plan) {
		return fmt.Errorf("checkpoint receipt and plan lengths differ")
	}
	for i, allocation := range plan {
		if allocation.Container != r.Container || allocation.Device.GPU != r.VisibleUUIDs[i] ||
			allocation.Resources.Instances != 1 || allocation.Resources.Memory < 0 || allocation.Resources.Cores < 0 ||
			r.MemoryLimits[i] != strconv.FormatInt(allocation.Resources.Memory, 10)+"m" ||
			r.CoreLimit != strconv.FormatInt(allocation.Resources.Cores, 10) {
			return fmt.Errorf("checkpoint UUID or quota differs from plan")
		}
	}
	return nil
}
