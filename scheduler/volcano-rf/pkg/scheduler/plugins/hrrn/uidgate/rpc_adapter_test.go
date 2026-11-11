package uidgate

import (
	"context"
	"fmt"
	"net"
	"testing"
	"time"

	"github.com/gogo/protobuf/proto"
	"google.golang.org/grpc"
	"google.golang.org/grpc/test/bufconn"
	pluginapi "k8s.io/kubelet/pkg/apis/deviceplugin/v1beta1"
)

// This test exercises official generated pluginapi types and an actual gRPC
// in-memory connection. Claim source, journal, node observer, checkpoint and
// kubelet sequence are synthetic; no actual device plugin or kubelet runs.
type rpcClaimSource struct {
	value Claim
	err   error
	calls int
}

func (s *rpcClaimSource) ActiveClaim(_ context.Context, _ string, _ []string) (Claim, error) {
	s.calls++
	return s.value, s.err
}

type rpcBuilder struct {
	value *pluginapi.ContainerAllocateResponse
	err   error
	calls int
}

func (s *rpcBuilder) BuildResponse(_ context.Context, _ Claim) (*pluginapi.ContainerAllocateResponse, error) {
	s.calls++
	return s.value, s.err
}

func rpcServer(t *testing.T, adapter *RPCAdapter) pluginapi.DevicePluginClient {
	t.Helper()
	listener := bufconn.Listen(1 << 20)
	s := grpc.NewServer()
	pluginapi.RegisterDevicePluginServer(s, adapter)
	go func() { _ = s.Serve(listener) }()
	t.Cleanup(func() { s.Stop(); _ = listener.Close() })
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	conn, err := grpc.DialContext(ctx, "bufnet", grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }), grpc.WithInsecure(), grpc.WithBlock())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return pluginapi.NewDevicePluginClient(conn)
}

func rpcFixture() (*RPCAdapter, *rpcClaimSource, *rpcBuilder, *journal, *observer) {
	now := time.Unix(1770000000, 0)
	claimSource := &rpcClaimSource{value: claim()}
	b := &rpcBuilder{value: &pluginapi.ContainerAllocateResponse{
		Envs:        map[string]string{"NVIDIA_VISIBLE_DEVICES": "GPU-A", "CUDA_DEVICE_MEMORY_LIMIT_0": "4096m", "CUDA_DEVICE_SM_LIMIT": "40"},
		Mounts:      []*pluginapi.Mount{{ContainerPath: "/etc/ld.so.preload", HostPath: "/test/preload", ReadOnly: true}},
		Devices:     []*pluginapi.DeviceSpec{{ContainerPath: "/dev/test", HostPath: "/dev/test", Permissions: "rw"}},
		Annotations: map[string]string{"test.example/annotation": "value"},
		CDIDevices:  []*pluginapi.CDIDevice{{Name: "test.example/gpu=device"}},
	}}
	j, o := &journal{}, &observer{observed: now}
	gate := &Gate{Journal: j, Observer: o, Now: func() time.Time { return now }, MaxAge: 10 * time.Second}
	return &RPCAdapter{Node: "node", Resource: "volcano.sh/vgpu-number", Claims: claimSource, Builder: b, Gate: gate}, claimSource, b, j, o
}

func TestRPCAdapterUsesGeneratedMessagesAndPreStartChecksActualRecordedUID(t *testing.T) {
	adapter, _, _, journal, observer := rpcFixture()
	client := rpcServer(t, adapter)
	ctx := context.Background()
	options, err := client.GetDevicePluginOptions(ctx, &pluginapi.Empty{})
	if err != nil || !options.PreStartRequired {
		t.Fatalf("PreStart not required: %+v %v", options, err)
	}
	request := &pluginapi.AllocateRequest{ContainerRequests: []*pluginapi.ContainerAllocateRequest{{DevicesIDs: []string{"slot-1"}}}}
	response, err := client.Allocate(ctx, request)
	if err != nil || len(response.ContainerResponses) != 1 {
		t.Fatalf("actual protobuf RPC Allocate failed: %+v %v", response, err)
	}
	item := response.ContainerResponses[0]
	if item.Envs[nonceEnv] == "" || len(item.Mounts) != 1 || len(item.Devices) != 1 || len(item.CDIDevices) != 1 || item.Annotations["test.example/annotation"] != "value" {
		t.Fatalf("response fields lost over gRPC: %+v", item)
	}
	// Mirrors kubelet v1.33 pod_devices.go, which marshals the decoded gogo
	// pluginapi response into a checkpoint. The surrounding checkpoint and UID
	// are synthetic and do not prove live node provenance or ownership.
	raw, err := proto.Marshal(item)
	if err != nil {
		t.Fatal(err)
	}
	observer.raw = checkpoint(t, entry("pod-b-uid", "worker", []string{"slot-1"}, raw))
	if _, err := client.PreStartContainer(ctx, &pluginapi.PreStartContainerRequest{DevicesIDs: []string{"slot-1"}}); err == nil {
		t.Fatal("wrong kubelet-recorded Pod accepted")
	}
	if journal.confirms != 0 {
		t.Fatal("wrong Pod confirmed")
	}
	observer.raw = checkpoint(t, entry("pod-a-uid", "worker", []string{"slot-1"}, raw))
	if _, err := client.PreStartContainer(ctx, &pluginapi.PreStartContainerRequest{DevicesIDs: []string{"slot-1"}}); err != nil {
		t.Fatalf("actual protobuf response could not round trip: %v", err)
	}
	if journal.confirms != 1 {
		t.Fatal("matching synthetic checkpoint was not confirmed")
	}
}

func TestRPCAdapterFailsClosedBeforeReturningAllocation(t *testing.T) {
	for _, which := range []string{"empty", "multiple", "duplicate-device", "claim-error", "wrong-node", "wrong-resource", "wrong-quota", "supplied-nonce", "journal-error"} {
		t.Run(which, func(t *testing.T) {
			adapter, source, builder, journal, _ := rpcFixture()
			request := &pluginapi.AllocateRequest{ContainerRequests: []*pluginapi.ContainerAllocateRequest{{DevicesIDs: []string{"slot-1"}}}}
			switch which {
			case "empty":
				request.ContainerRequests = nil
			case "multiple":
				request.ContainerRequests = append(request.ContainerRequests, &pluginapi.ContainerAllocateRequest{DevicesIDs: []string{"slot-2"}})
			case "duplicate-device":
				request.ContainerRequests[0].DevicesIDs = []string{"slot-1", "slot-1"}
			case "claim-error":
				source.err = fmt.Errorf("unavailable")
			case "wrong-node":
				source.value.Node = "another"
			case "wrong-resource":
				source.value.Resource = "another"
			case "wrong-quota":
				builder.value.Envs["CUDA_DEVICE_SM_LIMIT"] = "100"
			case "supplied-nonce":
				builder.value.Envs[nonceEnv] = "forged"
			case "journal-error":
				journal.uncertain = true
			}
			client := rpcServer(t, adapter)
			if response, err := client.Allocate(context.Background(), request); err == nil || response != nil {
				t.Fatalf("unverified request produced response: %+v %v", response, err)
			}
			if journal.prepared != nil || journal.confirms != 0 {
				t.Fatal("rejected request recorded a successful handoff")
			}
		})
	}
}

func TestRPCAdapterDoesNotExposeDeviceDiscoveryOrPreferredAllocation(t *testing.T) {
	adapter, _, _, _, _ := rpcFixture()
	client := rpcServer(t, adapter)
	if _, err := client.GetPreferredAllocation(context.Background(), &pluginapi.PreferredAllocationRequest{}); err == nil {
		t.Fatal("unsupported preferred allocation was enabled")
	}
	stream, err := client.ListAndWatch(context.Background(), &pluginapi.Empty{})
	if err == nil {
		_, err = stream.Recv()
	}
	if err == nil {
		t.Fatal("unsupported device discovery was enabled")
	}
}

func TestRPCAdapterRejectsTypedNilDependencies(t *testing.T) {
	for _, which := range []string{"claims", "builder", "journal", "observer"} {
		t.Run(which, func(t *testing.T) {
			adapter, _, _, _, _ := rpcFixture()
			switch which {
			case "claims":
				var p *rpcClaimSource
				adapter.Claims = p
			case "builder":
				var p *rpcBuilder
				adapter.Builder = p
			case "journal":
				var p *journal
				adapter.Gate.Journal = p
			case "observer":
				var p *observer
				adapter.Gate.Observer = p
			}
			if _, err := adapter.GetDevicePluginOptions(context.Background(), &pluginapi.Empty{}); err == nil {
				t.Fatal("typed-nil dependency was accepted")
			}
		})
	}
}
