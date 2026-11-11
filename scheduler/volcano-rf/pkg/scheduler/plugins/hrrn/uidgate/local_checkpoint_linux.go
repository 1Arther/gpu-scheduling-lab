//go:build linux

package uidgate

import (
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"golang.org/x/sys/unix"
)

// LocalCheckpointReader pins a caller-selected directory and opens the exact
// basename with O_NOFOLLOW. It is a read-only primitive, NOT proof that the
// directory is the real kubelet mount or that device IDs are fenced. The
// directory and file must be owned by expectedOwner and not group/world
// writable. Callers must protect the configured root path and mount namespace.
type LocalCheckpointReader struct {
	mu     sync.Mutex
	dir    int
	name   string
	owner  uint32
	opened bool
}

func checkOwned(stat *unix.Stat_t, owner uint32, typ uint32) error {
	if stat.Mode&unix.S_IFMT != typ || stat.Uid != owner || stat.Mode&0022 != 0 {
		return fmt.Errorf("checkpoint path type, owner or permissions unsafe")
	}
	return nil
}

// OpenLocalCheckpointReader traverses an absolute directory without following
// symbolic links in any path component, then pins the final trusted directory.
// This does not authenticate bind mounts or protect against privileged node
// compromise. Use expectedOwner=0 only where kubelet writes as root.
func OpenLocalCheckpointReader(directory, basename string, expectedOwner uint32) (*LocalCheckpointReader, error) {
	if !filepath.IsAbs(directory) || filepath.Clean(directory) != directory || basename == "" || basename == "." || basename == ".." || filepath.Base(basename) != basename || strings.ContainsAny(basename, "/\\") {
		return nil, fmt.Errorf("invalid checkpoint directory or basename")
	}
	fd, err := unix.Open("/", unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC|unix.O_NOFOLLOW, 0)
	if err != nil {
		return nil, err
	}
	parts := strings.Split(strings.TrimPrefix(directory, "/"), "/")
	for _, part := range parts {
		if part == "" {
			continue
		}
		if part == "." || part == ".." {
			unix.Close(fd)
			return nil, fmt.Errorf("invalid checkpoint path component")
		}
		next, err := unix.Openat(fd, part, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC|unix.O_NOFOLLOW, 0)
		unix.Close(fd)
		if err != nil {
			return nil, err
		}
		fd = next
	}
	var stat unix.Stat_t
	if err := unix.Fstat(fd, &stat); err != nil {
		unix.Close(fd)
		return nil, err
	}
	if err := checkOwned(&stat, expectedOwner, unix.S_IFDIR); err != nil {
		unix.Close(fd)
		return nil, err
	}
	return &LocalCheckpointReader{dir: fd, name: basename, owner: expectedOwner, opened: true}, nil
}

func (r *LocalCheckpointReader) Close() error {
	if r == nil {
		return nil
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if !r.opened {
		return nil
	}
	err := unix.Close(r.dir)
	r.dir = -1
	r.opened = false
	return err
}

func sameFile(a, b unix.Stat_t) bool {
	return a.Dev == b.Dev && a.Ino == b.Ino && a.Mode == b.Mode && a.Uid == b.Uid && a.Gid == b.Gid &&
		a.Size == b.Size && a.Mtim == b.Mtim && a.Ctim == b.Ctim && a.Nlink == b.Nlink
}

// ReadCheckpoint uses a bounded read and validates file identity before/after
// reading, including a final no-follow directory entry check. The root FD is
// pinned across reads. No method here authenticates the kubelet or proves that
// the record is current; successful reads must not advance the scheduler.
func (r *LocalCheckpointReader) ReadCheckpoint(ctx context.Context) (CheckpointSnapshot, error) {
	var zero CheckpointSnapshot
	if ctx == nil || r == nil {
		return zero, fmt.Errorf("missing checkpoint reader or context")
	}
	if err := ctx.Err(); err != nil {
		return zero, err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if !r.opened {
		return zero, fmt.Errorf("checkpoint reader is closed")
	}
	var rootstat unix.Stat_t
	if err := unix.Fstat(r.dir, &rootstat); err != nil {
		return zero, err
	}
	if err := checkOwned(&rootstat, r.owner, unix.S_IFDIR); err != nil {
		return zero, err
	}
	fd, err := unix.Openat(r.dir, r.name, unix.O_RDONLY|unix.O_CLOEXEC|unix.O_NOFOLLOW|unix.O_NONBLOCK, 0)
	if err != nil {
		return zero, err
	}
	file := os.NewFile(uintptr(fd), r.name)
	if file == nil {
		unix.Close(fd)
		return zero, fmt.Errorf("invalid checkpoint file descriptor")
	}
	defer file.Close()
	var before, after, entry unix.Stat_t
	if err = unix.Fstat(fd, &before); err != nil {
		return zero, err
	}
	if err = checkOwned(&before, r.owner, unix.S_IFREG); err != nil {
		return zero, err
	}
	if before.Nlink != 1 || before.Size <= 0 || before.Size > 32<<20 {
		return zero, fmt.Errorf("invalid checkpoint size or link count")
	}
	data, err := io.ReadAll(io.LimitReader(file, (32<<20)+1))
	if err != nil {
		return zero, err
	}
	if len(data) != int(before.Size) || len(data) > 32<<20 {
		return zero, fmt.Errorf("checkpoint size changed during read")
	}
	if err = unix.Fstat(fd, &after); err != nil {
		return zero, err
	}
	if err = unix.Fstatat(r.dir, r.name, &entry, unix.AT_SYMLINK_NOFOLLOW); err != nil {
		return zero, err
	}
	if !sameFile(before, after) || !sameFile(after, entry) {
		return zero, fmt.Errorf("checkpoint file changed during read")
	}
	if err = unix.Fstat(r.dir, &rootstat); err != nil {
		return zero, err
	}
	if err = checkOwned(&rootstat, r.owner, unix.S_IFDIR); err != nil {
		return zero, err
	}
	if err = ctx.Err(); err != nil {
		return zero, err
	}
	return CheckpointSnapshot{Bytes: data, ModTime: time.Unix(before.Mtim.Sec, before.Mtim.Nsec)}, nil
}
