package coordination

import (
	"bytes"
	"os"
	"testing"

	"google.golang.org/protobuf/encoding/protowire"
	devicecheckpoint "k8s.io/kubernetes/pkg/kubelet/cm/devicemanager/checkpoint"
)

func receiptProtoEnv(key, value string) []byte {
	var entry []byte
	entry = protowire.AppendTag(entry, 1, protowire.BytesType)
	entry = protowire.AppendString(entry, key)
	entry = protowire.AppendTag(entry, 2, protowire.BytesType)
	entry = protowire.AppendString(entry, value)
	var field []byte
	field = protowire.AppendTag(field, 1, protowire.BytesType)
	return protowire.AppendBytes(field, entry)
}

func receiptTestResponse(uuid string) []byte {
	var raw []byte
	for _, item := range [][2]string{{"NVIDIA_VISIBLE_DEVICES", uuid}, {"CUDA_DEVICE_MEMORY_LIMIT_0", "4096m"}, {"CUDA_DEVICE_SM_LIMIT", "40"}} {
		raw = append(raw, receiptProtoEnv(item[0], item[1])...)
	}
	return raw
}

func receiptCheckpoint(t *testing.T, entries []devicecheckpoint.PodDevicesEntry, ids []string) []byte {
	t.Helper()
	cp := devicecheckpoint.New(entries, map[string][]string{"volcano.sh/vgpu-number": ids})
	raw, err := cp.MarshalCheckpoint()
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func receiptEntry(uid string, response []byte) devicecheckpoint.PodDevicesEntry {
	return devicecheckpoint.PodDevicesEntry{PodUID: uid, ContainerName: "sleep", ResourceName: "volcano.sh/vgpu-number",
		DeviceIDs: devicecheckpoint.DevicesPerNUMA{-1: []string{"virtual-slot-1"}}, AllocResp: response}
}

func TestCheckpointReceiptValidatesUIDUUIDAndQuota(t *testing.T) {
	entry := receiptEntry("pod-uid", receiptTestResponse("GPU-123"))
	raw := receiptCheckpoint(t, []devicecheckpoint.PodDevicesEntry{entry}, []string{"virtual-slot-1"})
	got, err := InspectKubeletCheckpoint(raw, "pod-uid", "sleep", "volcano.sh/vgpu-number")
	if err != nil {
		t.Fatal(err)
	}
	plan := []Allocation{{Container: "sleep", Device: DeviceKey{Node: "node", GPU: "GPU-123"}, Resources: Resources{4096, 40, 1}}}
	if got.PodUID != "pod-uid" || got.CheckpointSHA256 != hashBytes(raw) || got.ResponseSHA256 != hashBytes(entry.AllocResp) || got.Matches(plan) != nil {
		t.Fatalf("unexpected receipt: %+v", got)
	}
	for _, mutation := range []func(*Allocation){
		func(p *Allocation) { p.Device.GPU = "wrong" },
		func(p *Allocation) { p.Resources.Memory++ },
		func(p *Allocation) { p.Resources.Cores++ },
		func(p *Allocation) { p.Resources.Instances++ },
		func(p *Allocation) { p.Container = "wrong" },
	} {
		changed := append([]Allocation(nil), plan...)
		mutation(&changed[0])
		if got.Matches(changed) == nil {
			t.Fatal("different UUID/quota/container accepted")
		}
	}
}

func TestCheckpointReceiptRejectsCorruptAmbiguousAndUnregistered(t *testing.T) {
	entry := receiptEntry("pod-uid", receiptTestResponse("GPU-123"))
	raw := receiptCheckpoint(t, []devicecheckpoint.PodDevicesEntry{entry}, []string{"virtual-slot-1"})
	corrupt := bytes.Replace(raw, []byte("pod-uid"), []byte("pod-uie"), 1)
	if bytes.Equal(raw, corrupt) {
		t.Fatal("fixture mutation failed")
	}
	for name, data := range map[string][]byte{
		"bad-checksum":  corrupt,
		"duplicate-row": receiptCheckpoint(t, []devicecheckpoint.PodDevicesEntry{entry, entry}, []string{"virtual-slot-1"}),
		"unregistered":  receiptCheckpoint(t, []devicecheckpoint.PodDevicesEntry{entry}, []string{"another-slot"}),
		"bad-protobuf":  receiptCheckpoint(t, []devicecheckpoint.PodDevicesEntry{receiptEntry("pod-uid", []byte{0x0a, 0xff})}, []string{"virtual-slot-1"}),
		"duplicate-env": receiptCheckpoint(t, []devicecheckpoint.PodDevicesEntry{receiptEntry("pod-uid", append(receiptTestResponse("GPU-123"), receiptProtoEnv("NVIDIA_VISIBLE_DEVICES", "GPU-123")...))}, []string{"virtual-slot-1"}),
		"extra-quota":   receiptCheckpoint(t, []devicecheckpoint.PodDevicesEntry{receiptEntry("pod-uid", append(receiptTestResponse("GPU-123"), receiptProtoEnv("CUDA_DEVICE_MEMORY_LIMIT_1", "4096m")...))}, []string{"virtual-slot-1"}),
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := InspectKubeletCheckpoint(data, "pod-uid", "sleep", "volcano.sh/vgpu-number"); err == nil {
				t.Fatal("invalid receipt accepted")
			}
		})
	}
	if _, err := InspectKubeletCheckpoint(raw, "another-uid", "sleep", "volcano.sh/vgpu-number"); err == nil {
		t.Fatal("unknown UID accepted")
	}
}

// The optional real Stage5 evidence is deliberately separate from synthetic
// fixtures and does not claim that its historical disk record is still live.
func TestCheckpointReceiptPriorRealKubeletEvidence(t *testing.T) {
	path := os.Getenv("HRRN_KUBELET_CHECKPOINT_FIXTURE")
	if path == "" {
		t.Skip("no saved Stage5 kubelet checkpoint fixture configured")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	receipt, err := InspectKubeletCheckpoint(raw, "3e58592a-6846-497a-9c48-8211517a49b2", "sleep", "volcano.sh/vgpu-number")
	if err != nil {
		t.Fatal(err)
	}
	plan := []Allocation{{Container: "sleep", Device: DeviceKey{Node: "t3dgq", GPU: "GPU-fb6aaeaf-9ca4-3c9a-7b3b-f56714c1f7cd"}, Resources: Resources{4096, 100, 1}}}
	if err := receipt.Matches(plan); err != nil {
		t.Fatal(err)
	}
}
