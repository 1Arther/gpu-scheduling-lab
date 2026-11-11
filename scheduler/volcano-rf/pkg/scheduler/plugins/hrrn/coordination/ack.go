// Package coordination contains a local experimental evidence contract.
// This file does not read Kubernetes, kubelet, a plugin, or NodeLock. The
// current production device plugin does not implement this UID-bound receipt
// protocol. Synthetic signatures below are not a production trust system.
package coordination

import (
	"crypto/hmac"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"time"
)

type AckKind string

const (
	AckBinding    AckKind = "binding"
	AckPlugin     AckKind = "plugin-response"
	AckCheckpoint AckKind = "kubelet-checkpoint"
	AckQuiescence AckKind = "node-quiescence"
)

// AckArtifact is a source-authenticated assertion. The trusted source, not
// this library, must inspect the real API, device-plugin response, kubelet
// checkpoint, or NodeLock. A source must not mint UIDBound from a guessed
// pending Pod or a bind-phase annotation. RawResponse is preserved for hash
// association; matching decoded allocations are asserted by the signer.
type AckArtifact struct {
	Kind       AckKind `json:"kind"`
	Source     string  `json:"source"`
	EvidenceID string  `json:"evidence_id"`
	JobUID     string  `json:"job_uid"`
	PodUID     string  `json:"pod_uid"`
	Node       string  `json:"node"`
	AttemptID  string  `json:"attempt_id"`
	ObservedAt int64   `json:"observed_at_unix_nano"`
	ExpiresAt  int64   `json:"expires_at_unix_nano"`

	// Binding evidence: actual API Pod UID and node, with observed revision.
	Bound           bool   `json:"bound,omitempty"`
	ResourceVersion string `json:"resource_version,omitempty"`
	// Plugin/checkpoint evidence is per container; the plugin's completed raw
	// response and the checkpoint must have exactly the same SHA-256 digest.
	Container      string       `json:"container,omitempty"`
	UIDBound       bool         `json:"uid_bound,omitempty"`
	ResponseDone   bool         `json:"response_done,omitempty"`
	DecodeVerified bool         `json:"decode_verified,omitempty"`
	RawResponse    []byte       `json:"raw_response,omitempty"`
	ResponseSHA256 string       `json:"response_sha256,omitempty"`
	Allocations    []Allocation `json:"allocations,omitempty"`
	// Plugin selection must have exactly this one candidate. Quiescence is a
	// separate later observation, not a synonym for bind-phase=success.
	CandidatesOnNode int    `json:"candidates_on_node,omitempty"`
	CandidatePodUID  string `json:"candidate_pod_uid,omitempty"`
	NoInFlight       bool   `json:"no_in_flight,omitempty"`
	NodeLockUnheld   bool   `json:"node_lock_unheld,omitempty"`
	Signature        []byte `json:"signature,omitempty"`
}

type AckBundle struct {
	Binding     AckArtifact
	Plugins     []AckArtifact
	Checkpoints []AckArtifact
	Quiescence  AckArtifact
}

// CheckpointArtifactFromUncheckedSource decodes a supplied checkpoint and
// creates an UNSIGNED checkpoint assertion only. It does not access or
// authenticate the node, establish freshness, prove that a Pod is live, or
// establish that the real plugin associated a request with that Pod. The
// provided node/source/time are caller assertions, not facts derived from the
// checkpoint. Even a historical record can parse correctly after release.
// A trusted observer must independently establish provenance and freshness
// before signing; the present code only supplies a synthetic signing helper.
func CheckpointArtifactFromUncheckedSource(ticket Ticket, plan PodPlan, raw []byte, container, resource, source, evidenceID string, observedAt, expiresAt time.Time) (AckArtifact, error) {
	if ticket.JobUID == "" || ticket.PodUID == "" || ticket.Node == "" || ticket.sequence == 0 ||
		plan.UID != ticket.PodUID || plan.Node != ticket.Node || container == "" || resource == "" ||
		source == "" || evidenceID == "" || observedAt.IsZero() || !expiresAt.After(observedAt) {
		return AckArtifact{}, reject("incomplete unchecked checkpoint assertion")
	}
	if _, err := allocationMap(plan); err != nil {
		return AckArtifact{}, err
	}
	var expected []Allocation
	for _, allocation := range plan.Allocations {
		if allocation.Container == container {
			expected = append(expected, allocation)
		}
	}
	if len(expected) == 0 {
		return AckArtifact{}, reject("checkpoint container is absent from plan")
	}
	receipt, err := InspectKubeletCheckpoint(raw, ticket.PodUID, container, resource)
	if err != nil {
		return AckArtifact{}, fmt.Errorf("unchecked checkpoint parse: %w", err)
	}
	if err := receipt.Matches(expected); err != nil {
		return AckArtifact{}, fmt.Errorf("unchecked checkpoint plan mismatch: %w", err)
	}
	return AckArtifact{Kind: AckCheckpoint, Source: source, EvidenceID: evidenceID,
		JobUID: ticket.JobUID, PodUID: ticket.PodUID, Node: ticket.Node, AttemptID: DurableAttemptID(ticket),
		ObservedAt: observedAt.UnixNano(), ExpiresAt: expiresAt.UnixNano(), Container: container,
		UIDBound: true, ResponseDone: true, DecodeVerified: true, ResponseSHA256: receipt.ResponseSHA256,
		Allocations: append([]Allocation(nil), expected...)}, nil
}

// AckAnchor and AckTrust simulate authenticated sources with role-specific
// keys. They cannot attest that a real node, plugin, or kubelet produced the
// assertion; production requires a separately authenticated, UID-aware agent
// and lifecycle-bound keys, and must retain replay evidence durably. Distinct
// source labels and keys do not establish independent real-world authorities.
type AckAnchor struct {
	Source string
	Key    []byte
}

type AckTrust struct{ anchors map[AckKind]AckAnchor }

func NewAckTrust(anchors map[AckKind]AckAnchor) (*AckTrust, error) {
	required := []AckKind{AckBinding, AckPlugin, AckCheckpoint, AckQuiescence}
	if len(anchors) != len(required) {
		return nil, reject("all and only the four trusted evidence roles are required")
	}
	t := &AckTrust{anchors: make(map[AckKind]AckAnchor, len(required))}
	sources := make(map[string]struct{}, len(required))
	keys := make(map[string]struct{}, len(required))
	for _, kind := range required {
		a, ok := anchors[kind]
		if !ok || a.Source == "" || len(a.Key) < 32 {
			return nil, reject("missing role trust anchor or weak key")
		}
		if _, exists := sources[a.Source]; exists {
			return nil, reject("evidence roles must use distinct source labels")
		}
		// HMAC hashes keys longer than the SHA-256 block and pads shorter keys
		// with zeroes. Reject byte-distinct keys with the same effective key.
		key := a.Key
		if len(key) > sha256.BlockSize {
			sum := sha256.Sum256(key)
			key = sum[:]
		}
		var effective [sha256.BlockSize]byte
		copy(effective[:], key)
		if _, exists := keys[string(effective[:])]; exists {
			return nil, reject("evidence roles must use distinct effective HMAC keys")
		}
		sources[a.Source] = struct{}{}
		keys[string(effective[:])] = struct{}{}
		t.anchors[kind] = AckAnchor{Source: a.Source, Key: append([]byte(nil), a.Key...)}
	}
	return t, nil
}

func ackPayload(proof AckArtifact) ([]byte, error) {
	proof.Signature = nil
	return json.Marshal(proof)
}

// SignSyntheticAck exists for local simulations only. It is not evidence that
// the current real plugin emitted a UID-bound receipt or that any trusted
// source is installed. Callers must protect keys and authenticate issuers.
func SignSyntheticAck(key []byte, proof AckArtifact) (AckArtifact, error) {
	if len(key) < 32 {
		return AckArtifact{}, reject("weak synthetic signing key")
	}
	payload, err := ackPayload(proof)
	if err != nil {
		return AckArtifact{}, err
	}
	mac := hmac.New(sha256.New, key)
	_, _ = mac.Write([]byte("hrrn-local-ack-v1\x00"))
	_, _ = mac.Write(payload)
	proof.Signature = mac.Sum(nil)
	return proof, nil
}

func (t *AckTrust) verify(proof AckArtifact, kind AckKind, ticket Ticket, attemptID string, now time.Time, maxAge time.Duration) error {
	if t == nil {
		return reject("no configured evidence trust")
	}
	a, ok := t.anchors[kind]
	if !ok || proof.Kind != kind || proof.Source != a.Source || proof.EvidenceID == "" ||
		proof.JobUID != ticket.JobUID || proof.PodUID != ticket.PodUID || proof.Node != ticket.Node || proof.AttemptID != attemptID {
		return reject("evidence role, source, identity or attempt mismatch")
	}
	if proof.ObservedAt <= 0 || proof.ExpiresAt < proof.ObservedAt || proof.ObservedAt > now.UnixNano() ||
		proof.ExpiresAt < now.UnixNano() || now.Sub(time.Unix(0, proof.ObservedAt)) > maxAge {
		return reject("stale, future or expired evidence")
	}
	payload, err := ackPayload(proof)
	if err != nil {
		return err
	}
	mac := hmac.New(sha256.New, a.Key)
	_, _ = mac.Write([]byte("hrrn-local-ack-v1\x00"))
	_, _ = mac.Write(payload)
	if subtle.ConstantTimeCompare(mac.Sum(nil), proof.Signature) != 1 {
		return reject("evidence signature mismatch")
	}
	return nil
}

func sameContainerAllocations(plan PodPlan, container string, got []Allocation) bool {
	want, err := allocationMap(plan)
	if err != nil {
		return false
	}
	actual, err := allocationMap(PodPlan{UID: plan.UID, Node: plan.Node, Allocations: got})
	if err != nil {
		return false
	}
	count := 0
	for key, value := range want {
		if key.container == container {
			count++
			if actual[key] != value {
				return false
			}
		}
	}
	return count > 0 && len(actual) == count
}

// VerifyDeviceAcknowledgement verifies a *simulated*, source-authenticated
// bundle and produces the observation accepted by the coordination model.
// It never mutates scheduler state or releases anything. The caller must pass
// its result to the durable store via a separate ticket/CAS-checked transition;
// if verification or that transition fails, retain all locks/reservations.
// DurableAttemptID identifies this coordinator attempt only; it is not a
// real plugin or cross-scheduler fencing mechanism. Persisted evidence IDs,
// fresh node claims, immutable workload membership, and a real UID-aware
// trusted evidence producer are still required for a production adapter.
func VerifyDeviceAcknowledgement(ticket Ticket, plan PodPlan, bundle AckBundle, trust *AckTrust, now time.Time, maxAge time.Duration) (ConsumptionObservation, error) {
	if ticket.JobUID == "" || ticket.PodUID == "" || ticket.Node == "" || ticket.sequence == 0 ||
		plan.UID != ticket.PodUID || plan.Node != ticket.Node || now.IsZero() || maxAge <= 0 || maxAge > 5*time.Minute {
		return ConsumptionObservation{}, reject("invalid ticket, plan, clock or maximum evidence age")
	}
	if _, err := allocationMap(plan); err != nil {
		return ConsumptionObservation{}, err
	}
	attempt := DurableAttemptID(ticket)
	if attempt == "" {
		return ConsumptionObservation{}, reject("missing attempt identity")
	}
	seen := map[string]bool{}
	verify := func(proof AckArtifact, kind AckKind) error {
		if err := trust.verify(proof, kind, ticket, attempt, now, maxAge); err != nil {
			return err
		}
		if seen[proof.EvidenceID] {
			return reject("duplicate evidence ID")
		}
		seen[proof.EvidenceID] = true
		return nil
	}
	if err := verify(bundle.Binding, AckBinding); err != nil {
		return ConsumptionObservation{}, err
	}
	if !bundle.Binding.Bound || bundle.Binding.ResourceVersion == "" {
		return ConsumptionObservation{}, reject("actual Pod binding is not established")
	}
	containers := map[string]bool{}
	for _, a := range plan.Allocations {
		containers[a.Container] = true
	}
	if len(bundle.Plugins) != len(containers) || len(bundle.Checkpoints) != len(containers) {
		return ConsumptionObservation{}, reject("missing or extra container evidence")
	}
	plugins := map[string]AckArtifact{}
	for _, proof := range bundle.Plugins {
		if err := verify(proof, AckPlugin); err != nil {
			return ConsumptionObservation{}, err
		}
		if !containers[proof.Container] || plugins[proof.Container].Container != "" || !proof.UIDBound ||
			!proof.ResponseDone || !proof.DecodeVerified || len(proof.RawResponse) == 0 || proof.CandidatesOnNode != 1 ||
			proof.CandidatePodUID != ticket.PodUID || proof.ObservedAt < bundle.Binding.ObservedAt ||
			!sameContainerAllocations(plan, proof.Container, proof.Allocations) {
			return ConsumptionObservation{}, reject("UID-bound plugin response is incomplete or differs from plan")
		}
		digest := sha256.Sum256(proof.RawResponse)
		if proof.ResponseSHA256 != hex.EncodeToString(digest[:]) {
			return ConsumptionObservation{}, reject("plugin response digest mismatch")
		}
		plugins[proof.Container] = proof
	}
	checked := map[string]bool{}
	var latestCheckpoint int64
	for _, proof := range bundle.Checkpoints {
		if err := verify(proof, AckCheckpoint); err != nil {
			return ConsumptionObservation{}, err
		}
		plugin, ok := plugins[proof.Container]
		if !ok || checked[proof.Container] || !proof.UIDBound || !proof.ResponseDone || !proof.DecodeVerified ||
			proof.ResponseSHA256 != plugin.ResponseSHA256 || proof.ObservedAt < plugin.ObservedAt ||
			!sameContainerAllocations(plan, proof.Container, proof.Allocations) {
			return ConsumptionObservation{}, reject("checkpoint is incomplete or does not match plugin response")
		}
		checked[proof.Container] = true
		if proof.ObservedAt > latestCheckpoint {
			latestCheckpoint = proof.ObservedAt
		}
	}
	if err := verify(bundle.Quiescence, AckQuiescence); err != nil {
		return ConsumptionObservation{}, err
	}
	q := bundle.Quiescence
	if !q.NoInFlight || !q.NodeLockUnheld || q.CandidatesOnNode != 0 || q.CandidatePodUID != "" || q.ObservedAt < latestCheckpoint {
		return ConsumptionObservation{}, reject("node quiescence or lock release not established")
	}
	// Evidence IDs are recorded in the result identity. No actual consumption
	// is inferred without the authenticated claims checked above.
	identity, err := json.Marshal(bundle)
	if err != nil {
		return ConsumptionObservation{}, err
	}
	digest := sha256.Sum256(identity)
	return ConsumptionObservation{PodUID: ticket.PodUID, Node: ticket.Node,
		ObservationID: fmt.Sprintf("simulated-verified-ack:%x", digest[:]), Allocations: append([]Allocation(nil), plan.Allocations...),
		BoundUIDVerified: true, PluginResponseVerified: true, KubeletCheckpointVerified: true,
		NoInFlightAllocate: true, NodeLockUnheld: true}, nil
}
