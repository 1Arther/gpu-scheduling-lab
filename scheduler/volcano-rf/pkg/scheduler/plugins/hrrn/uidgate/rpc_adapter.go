package uidgate

import (
	"context"
	"fmt"
	"reflect"
	"strconv"
	"strings"

	pluginapi "k8s.io/kubelet/pkg/apis/deviceplugin/v1beta1"
	"volcano.sh/volcano/pkg/scheduler/plugins/hrrn/coordination"
)

// ActiveClaimSource must provide the one already-reserved and externally
// fenced claim for this node. The device plugin API supplies no Pod UID; this
// interface cannot authenticate the Pod behind AllocateRequest by itself.
// Production use requires all competing allocators and stale RPCs fenced.
type ActiveClaimSource interface {
	ActiveClaim(context.Context, string, []string) (Claim, error)
}

// ResponseBuilder constructs a response for the supplied claim without doing
// external mutations. Legacy GetPendingPod/Allocate is not a valid builder:
// it guesses identity and has side effects before identity can be confirmed.
type ResponseBuilder interface {
	BuildResponse(context.Context, Claim) (*pluginapi.ContainerAllocateResponse, error)
}

// RPCAdapter implements the official Kubernetes v1beta1 RPC message types for
// an isolated experiment. It deliberately cannot register as a usable device
// plugin: ListAndWatch and GetPreferredAllocation remain unimplemented. No
// real kubelet, HAMi plugin or production authentication is supplied here.
// Gate's Journal and Observer are mandatory and must enforce their contracts;
// this adapter adds no fencing and must not be used for live scheduling.
type RPCAdapter struct {
	pluginapi.UnimplementedDevicePluginServer
	Node, Resource string
	Claims         ActiveClaimSource
	Builder        ResponseBuilder
	Gate           *Gate
}

var _ pluginapi.DevicePluginServer = (*RPCAdapter)(nil)

func nilComponent(v interface{}) bool {
	if v == nil {
		return true
	}
	value := reflect.ValueOf(v)
	switch value.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map, reflect.Ptr, reflect.Slice:
		return value.IsNil()
	default:
		return false
	}
}

func (a *RPCAdapter) GetDevicePluginOptions(ctx context.Context, _ *pluginapi.Empty) (*pluginapi.DevicePluginOptions, error) {
	if ctx == nil || a == nil || a.Node == "" || a.Resource == "" || nilComponent(a.Claims) || nilComponent(a.Builder) || a.Gate == nil || nilComponent(a.Gate.Journal) || nilComponent(a.Gate.Observer) {
		return nil, fmt.Errorf("uid gate adapter is not fully configured")
	}
	return &pluginapi.DevicePluginOptions{PreStartRequired: true}, nil
}

// validateBuiltResponse rejects a builder that assigns different devices or
// quota before any nonce is journaled. Mounts, devices, annotations and CDI
// content are covered by Gate's response semantic digest, but checking their
// authorization remains a required responsibility of the real builder.
// The comparison assumes one MiB in the plan maps to "1m" in the response
// (GPUMemoryFactor=1). Non-unit HAMi factors require explicit conversion and
// separate validation before an actual device plugin can use this adapter.
func validateBuiltResponse(claim Claim, response *pluginapi.ContainerAllocateResponse) error {
	if response == nil || response.Envs == nil {
		return fmt.Errorf("empty device response")
	}
	if _, exists := response.Envs[nonceEnv]; exists {
		return fmt.Errorf("builder supplied a handoff nonce")
	}
	visible := strings.Split(response.Envs["NVIDIA_VISIBLE_DEVICES"], ",")
	limits := make([]string, len(visible))
	for i := range visible {
		limits[i] = response.Envs["CUDA_DEVICE_MEMORY_LIMIT_"+strconv.Itoa(i)]
	}
	seen := map[string]bool{}
	for _, id := range visible {
		if id == "" || strings.TrimSpace(id) != id || seen[id] {
			return fmt.Errorf("invalid visible GPU list")
		}
		seen[id] = true
	}
	for key := range response.Envs {
		const prefix = "CUDA_DEVICE_MEMORY_LIMIT_"
		if strings.HasPrefix(key, prefix) {
			suffix := strings.TrimPrefix(key, prefix)
			idx, err := strconv.Atoi(suffix)
			if err != nil || idx < 0 || idx >= len(visible) || strconv.Itoa(idx) != suffix {
				return fmt.Errorf("unexpected memory quota")
			}
		}
	}
	receipt := coordination.CheckpointReceipt{Container: claim.Container, VisibleUUIDs: visible, MemoryLimits: limits, CoreLimit: response.Envs["CUDA_DEVICE_SM_LIMIT"]}
	return receipt.Matches(claim.Allocations)
}

func (a *RPCAdapter) Allocate(ctx context.Context, request *pluginapi.AllocateRequest) (*pluginapi.AllocateResponse, error) {
	if _, err := a.GetDevicePluginOptions(ctx, nil); err != nil {
		return nil, err
	}
	if request == nil || len(request.ContainerRequests) != 1 || request.ContainerRequests[0] == nil {
		return nil, fmt.Errorf("exactly one container request is required")
	}
	ids, err := idsCanonical(request.ContainerRequests[0].DevicesIDs)
	if err != nil {
		return nil, err
	}
	claim, err := a.Claims.ActiveClaim(ctx, a.Node, append([]string(nil), ids...))
	if err != nil {
		return nil, fmt.Errorf("active claim: %w", err)
	}
	if !validClaim(claim) || claim.Node != a.Node || claim.Resource != a.Resource || len(claim.Allocations) != len(ids) {
		return nil, fmt.Errorf("claim does not match node, resource or device count")
	}
	// Prevent a mutable provider from changing the plan while the response is
	// built or the journal copies it.
	claim.Allocations = append([]coordination.Allocation(nil), claim.Allocations...)
	base, err := a.Builder.BuildResponse(ctx, claim)
	if err != nil {
		return nil, fmt.Errorf("build response: %w", err)
	}
	if err := validateBuiltResponse(claim, base); err != nil {
		return nil, fmt.Errorf("response differs from claim: %w", err)
	}
	raw, err := base.Marshal()
	if err != nil {
		return nil, err
	}
	withNonce, err := a.Gate.PrepareAllocate(ctx, claim, ids, raw)
	if err != nil {
		return nil, err
	}
	response := &pluginapi.ContainerAllocateResponse{}
	if err := response.Unmarshal(withNonce); err != nil {
		// The journal may have committed already; never retry or release here.
		return nil, fmt.Errorf("journaled response decode is uncertain: %w", err)
	}
	return &pluginapi.AllocateResponse{ContainerResponses: []*pluginapi.ContainerAllocateResponse{response}}, nil
}

func (a *RPCAdapter) PreStartContainer(ctx context.Context, request *pluginapi.PreStartContainerRequest) (*pluginapi.PreStartContainerResponse, error) {
	if _, err := a.GetDevicePluginOptions(ctx, nil); err != nil {
		return nil, err
	}
	if request == nil {
		return nil, fmt.Errorf("missing prestart request")
	}
	if _, err := a.Gate.CheckPreStart(ctx, a.Node, request.DevicesIDs); err != nil {
		return nil, err
	}
	return &pluginapi.PreStartContainerResponse{}, nil
}
