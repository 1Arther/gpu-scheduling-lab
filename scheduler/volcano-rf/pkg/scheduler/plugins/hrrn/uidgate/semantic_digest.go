package uidgate

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"unicode/utf8"

	"google.golang.org/protobuf/encoding/protowire"
)

// This parser is deliberately limited to the Kubernetes v1beta1
// ContainerAllocateResponse schema: envs=1, mounts=2, devices=3,
// annotations=4, CDI devices=5. Unknown fields and ambiguous duplicate fields
// fail closed. This is not a substitute for verifying the actual kubelet's
// version and serialization in an end-to-end test.
// Schema: https://github.com/kubernetes/kubelet/blob/v0.33.2/pkg/apis/deviceplugin/v1beta1/api.proto
type wireField struct {
	number protowire.Number
	data   []byte
	value  uint64
}

func decodeFields(raw []byte, schema map[protowire.Number]protowire.Type) ([]wireField, error) {
	var fields []wireField
	for len(raw) > 0 {
		number, kind, n := protowire.ConsumeTag(raw)
		if n < 0 {
			return nil, protowire.ParseError(n)
		}
		raw = raw[n:]
		wanted, ok := schema[number]
		if !ok || wanted != kind {
			return nil, fmt.Errorf("unsupported protobuf field %d", number)
		}
		f := wireField{number: number}
		if kind == protowire.BytesType {
			value, size := protowire.ConsumeBytes(raw)
			if size < 0 {
				return nil, protowire.ParseError(size)
			}
			f.data, raw = value, raw[size:]
		} else if kind == protowire.VarintType {
			value, size := protowire.ConsumeVarint(raw)
			if size < 0 {
				return nil, protowire.ParseError(size)
			}
			f.value, raw = value, raw[size:]
		} else {
			return nil, fmt.Errorf("unsupported protobuf wire type")
		}
		fields = append(fields, f)
	}
	return fields, nil
}

func validString(b []byte) (string, error) {
	if !utf8.Valid(b) {
		return "", fmt.Errorf("invalid protobuf UTF-8")
	}
	return string(b), nil
}

func mapEntry(raw []byte) (string, string, error) {
	fields, err := decodeFields(raw, map[protowire.Number]protowire.Type{1: protowire.BytesType, 2: protowire.BytesType})
	if err != nil {
		return "", "", err
	}
	var key, value string
	seen := map[protowire.Number]bool{}
	for _, field := range fields {
		if seen[field.number] {
			return "", "", fmt.Errorf("duplicate protobuf map entry field")
		}
		seen[field.number] = true
		s, err := validString(field.data)
		if err != nil {
			return "", "", err
		}
		if field.number == 1 {
			key = s
		} else {
			value = s
		}
	}
	if key == "" {
		return "", "", fmt.Errorf("empty protobuf map key")
	}
	return key, value, nil
}

// Nested fields are scalar and have protobuf defaults when absent. Bool
// values other than 0 and 1 are rejected conservatively.
func nested(raw []byte, schema map[protowire.Number]protowire.Type, count int) ([]string, error) {
	fields, err := decodeFields(raw, schema)
	if err != nil {
		return nil, err
	}
	values := make([]string, count)
	seen := map[protowire.Number]bool{}
	for _, field := range fields {
		if seen[field.number] {
			return nil, fmt.Errorf("duplicate protobuf scalar")
		}
		seen[field.number] = true
		if schema[field.number] == protowire.VarintType {
			if field.value > 1 {
				return nil, fmt.Errorf("invalid protobuf bool")
			}
			if field.value == 1 {
				values[int(field.number)-1] = "true"
			}
		} else {
			v, err := validString(field.data)
			if err != nil {
				return nil, err
			}
			values[int(field.number)-1] = v
		}
	}
	return values, nil
}

type canonicalResponse struct {
	Envs        map[string]string `json:"envs"`
	Mounts      [][]string        `json:"mounts"`
	Devices     [][]string        `json:"devices"`
	Annotations map[string]string `json:"annotations"`
	CDIDevices  [][]string        `json:"cdi_devices"`
}

// responseSemanticDigest covers every supported field, including mount,
// device and CDI details. Protobuf maps are order-independent, but repeated
// fields retain their order. Different valid wire encodings of the same
// defaults produce the same digest. This helper makes no claim about node
// identity, kubelet liveness, freshness, or device ownership.
func responseSemanticDigest(raw []byte) (string, error) {
	if len(raw) == 0 || len(raw) > 32<<20 {
		return "", fmt.Errorf("invalid response size")
	}
	fields, err := decodeFields(raw, map[protowire.Number]protowire.Type{1: protowire.BytesType, 2: protowire.BytesType, 3: protowire.BytesType, 4: protowire.BytesType, 5: protowire.BytesType})
	if err != nil {
		return "", err
	}
	c := canonicalResponse{Envs: map[string]string{}, Annotations: map[string]string{}, Mounts: [][]string{}, Devices: [][]string{}, CDIDevices: [][]string{}}
	for _, field := range fields {
		switch field.number {
		case 1, 4:
			key, value, err := mapEntry(field.data)
			if err != nil {
				return "", err
			}
			target := c.Envs
			if field.number == 4 {
				target = c.Annotations
			}
			if _, exists := target[key]; exists {
				return "", fmt.Errorf("duplicate protobuf map key")
			}
			target[key] = value
		case 2:
			v, err := nested(field.data, map[protowire.Number]protowire.Type{1: protowire.BytesType, 2: protowire.BytesType, 3: protowire.VarintType}, 3)
			if err != nil {
				return "", err
			}
			c.Mounts = append(c.Mounts, v)
		case 3:
			v, err := nested(field.data, map[protowire.Number]protowire.Type{1: protowire.BytesType, 2: protowire.BytesType, 3: protowire.BytesType}, 3)
			if err != nil {
				return "", err
			}
			c.Devices = append(c.Devices, v)
		case 5:
			v, err := nested(field.data, map[protowire.Number]protowire.Type{1: protowire.BytesType}, 1)
			if err != nil {
				return "", err
			}
			c.CDIDevices = append(c.CDIDevices, v)
		}
	}
	encoded, err := json.Marshal(c)
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256(encoded)
	return hex.EncodeToString(digest[:]), nil
}
