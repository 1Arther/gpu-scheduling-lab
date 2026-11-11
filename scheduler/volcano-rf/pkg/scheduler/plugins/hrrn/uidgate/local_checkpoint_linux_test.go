//go:build linux

package uidgate

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

func TestLocalCheckpointReaderRejectsSymlinkAndUnsafeFile(t *testing.T) {
	dir := t.TempDir()
	filename := filepath.Join(dir, "checkpoint")
	if err := os.WriteFile(filename, []byte("checkpoint"), 0600); err != nil {
		t.Fatal(err)
	}
	r, err := OpenLocalCheckpointReader(dir, "checkpoint", uint32(os.Geteuid()))
	if err != nil {
		t.Fatal(err)
	}
	snapshot, err := r.ReadCheckpoint(context.Background())
	if err != nil || string(snapshot.Bytes) != "checkpoint" || snapshot.ModTime.IsZero() {
		t.Fatalf("read failed: %+v %v", snapshot, err)
	}
	if err := r.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := r.ReadCheckpoint(context.Background()); err == nil {
		t.Fatal("closed reader accepted")
	}
	if err := r.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := (&LocalCheckpointReader{}).ReadCheckpoint(context.Background()); err == nil {
		t.Fatal("zero reader accepted")
	}
	if err := (&LocalCheckpointReader{}).Close(); err != nil {
		t.Fatal(err)
	}

	link := filepath.Join(dir, "checkpoint-link")
	if err := os.Symlink("checkpoint", link); err != nil {
		t.Fatal(err)
	}
	r, err = OpenLocalCheckpointReader(dir, "checkpoint-link", uint32(os.Geteuid()))
	if err != nil {
		t.Fatal(err)
	}
	if _, err = r.ReadCheckpoint(context.Background()); err == nil {
		t.Fatal("symlink checkpoint accepted")
	}
	r.Close()
	if err := os.Chmod(filename, 0666); err != nil {
		t.Fatal(err)
	}
	r, err = OpenLocalCheckpointReader(dir, "checkpoint", uint32(os.Geteuid()))
	if err != nil {
		t.Fatal(err)
	}
	if _, err = r.ReadCheckpoint(context.Background()); err == nil {
		t.Fatal("world-writable checkpoint accepted")
	}
	r.Close()
}

func TestLocalCheckpointReaderRejectsSymlinkRootFIFOAndTraversal(t *testing.T) {
	dir := t.TempDir()
	child := filepath.Join(dir, "trusted")
	if err := os.Mkdir(child, 0700); err != nil {
		t.Fatal(err)
	}
	alias := filepath.Join(dir, "alias")
	if err := os.Symlink(child, alias); err != nil {
		t.Fatal(err)
	}
	if _, err := OpenLocalCheckpointReader(alias, "checkpoint", uint32(os.Geteuid())); err == nil {
		t.Fatal("symlink root accepted")
	}
	if _, err := OpenLocalCheckpointReader(child, "../checkpoint", uint32(os.Geteuid())); err == nil {
		t.Fatal("path traversal accepted")
	}
	if err := unix.Mkfifo(filepath.Join(child, "pipe"), 0600); err != nil {
		t.Fatal(err)
	}
	r, err := OpenLocalCheckpointReader(child, "pipe", uint32(os.Geteuid()))
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	if _, err := r.ReadCheckpoint(context.Background()); err == nil {
		t.Fatal("FIFO accepted as checkpoint")
	}
}

func TestLocalCheckpointReaderRejectsWrongOwnerOrWritableRoot(t *testing.T) {
	dir := t.TempDir()
	if _, err := OpenLocalCheckpointReader(dir, "checkpoint", uint32(os.Geteuid())+1); err == nil {
		t.Fatal("wrong owner accepted")
	}
	if err := os.Chmod(dir, 0777); err != nil {
		t.Fatal(err)
	}
	if _, err := OpenLocalCheckpointReader(dir, "checkpoint", uint32(os.Geteuid())); err == nil {
		t.Fatal("world-writable root accepted")
	}
}

func TestLocalObservationReadsRealLinuxCheckpointWithFakeKubernetes(t *testing.T) {
	o, _, source, c, expected := localObservationFixture(t)
	dir := t.TempDir()
	path := filepath.Join(dir, "checkpoint")
	if err := os.WriteFile(path, source.snapshot.Bytes, 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(path, source.snapshot.ModTime, source.snapshot.ModTime); err != nil {
		t.Fatal(err)
	}
	r, err := OpenLocalCheckpointReader(dir, "checkpoint", uint32(os.Geteuid()))
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	o.Checkpoint = r
	if _, err = o.Observe(context.Background(), c, expected); err != nil {
		t.Fatal(err)
	}
	if err = os.Chmod(dir, 0777); err != nil {
		t.Fatal(err)
	}
	if _, err = o.Observe(context.Background(), c, expected); err == nil {
		t.Fatal("directory permission change accepted")
	}
	if err = os.Chmod(dir, 0700); err != nil {
		t.Fatal(err)
	}
	stale := o.Now().Add(-time.Hour)
	if err = os.Chtimes(path, stale, stale); err != nil {
		t.Fatal(err)
	}
	if _, err = o.Observe(context.Background(), c, expected); err == nil {
		t.Fatal("stale file accepted")
	}
}
