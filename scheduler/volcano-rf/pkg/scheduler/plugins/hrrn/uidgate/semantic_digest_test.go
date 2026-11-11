package uidgate

import (
	"testing"

	"google.golang.org/protobuf/encoding/protowire"
)

func wireBytes(number protowire.Number, value []byte) []byte {
	out := protowire.AppendTag(nil, number, protowire.BytesType)
	return protowire.AppendBytes(out, value)
}
func wireString(number protowire.Number, value string) []byte {
	return wireBytes(number, []byte(value))
}
func wireBool(number protowire.Number, value uint64) []byte {
	out := protowire.AppendTag(nil, number, protowire.VarintType)
	return protowire.AppendVarint(out, value)
}
func semanticExample(reverse bool) []byte {
	a, b := pair("A", "1"), pair("B", "2")
	if reverse {
		a, b = b, a
	}
	out := append(append([]byte{}, a...), b...)
	mount := append(wireString(1, "/container"), wireString(2, "/host")...)
	mount = append(mount, wireBool(3, 1)...)
	out = append(out, wireBytes(2, mount)...)
	device := append(wireString(1, "/dev/a"), wireString(2, "/dev/b")...)
	device = append(device, wireString(3, "rw")...)
	out = append(out, wireBytes(3, device)...)
	annotation := append(wireString(1, "annotation"), wireString(2, "value")...)
	out = append(out, wireBytes(4, annotation)...)
	out = append(out, wireBytes(5, wireString(1, "vendor/gpu=one"))...)
	return out
}

func TestResponseSemanticDigestIsMapOrderIndependentAndComplete(t *testing.T) {
	a, err := responseSemanticDigest(semanticExample(false))
	if err != nil {
		t.Fatal(err)
	}
	b, err := responseSemanticDigest(semanticExample(true))
	if err != nil || a != b {
		t.Fatalf("equivalent map encodings differ: %s %s %v", a, b, err)
	}
	for name, raw := range map[string][]byte{
		"mount":      append(semanticExample(false), wireBytes(2, wireString(1, "/other"))...),
		"device":     append(semanticExample(false), wireBytes(3, wireString(1, "/other"))...),
		"annotation": append(semanticExample(false), wireBytes(4, append(wireString(1, "new"), wireString(2, "x")...))...),
		"cdi":        append(semanticExample(false), wireBytes(5, wireString(1, "vendor/gpu=two"))...),
	} {
		t.Run(name, func(t *testing.T) {
			digest, err := responseSemanticDigest(raw)
			if err != nil || digest == a {
				t.Fatalf("changed content was not covered: %s %v", digest, err)
			}
		})
	}
}

func TestResponseSemanticDigestRejectsAmbiguousOrUnknownFields(t *testing.T) {
	for name, raw := range map[string][]byte{
		"unknown":                append(semanticExample(false), wireString(6, "new-field")...),
		"duplicate-map-key":      append(semanticExample(false), pair("A", "1")...),
		"duplicate-nested-field": wireBytes(2, append(wireString(1, "a"), wireString(1, "b")...)),
		"unknown-nested-field":   wireBytes(5, wireString(2, "unsupported")),
		"bad-bool":               wireBytes(2, wireBool(3, 2)),
		"bad-wire":               []byte{0x0a, 0xff},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := responseSemanticDigest(raw); err == nil {
				t.Fatal("ambiguous or unsupported response accepted")
			}
		})
	}
}
