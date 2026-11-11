//go:build !linux

package uidgate

import (
	"context"
	"fmt"
)

// The secure local checkpoint reader requires Linux openat/O_NOFOLLOW.
// Unsupported operating systems fail closed.
type LocalCheckpointReader struct{}

func OpenLocalCheckpointReader(string, string, uint32) (*LocalCheckpointReader, error) {
	return nil, fmt.Errorf("secure local checkpoint reader requires Linux")
}
func (*LocalCheckpointReader) Close() error { return nil }
func (*LocalCheckpointReader) ReadCheckpoint(context.Context) (CheckpointSnapshot, error) {
	return CheckpointSnapshot{}, fmt.Errorf("secure local checkpoint reader requires Linux")
}
