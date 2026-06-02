package qpdf

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/opus2/office-convert-orchestrator/internal/oerrors"
	"github.com/opus2/office-convert-orchestrator/internal/types"
)

// writeFakeQpdf writes a shell script standing in for qpdf and points the
// package Binary at it. The script writes `body` to stdout and exits `code`.
func writeFakeQpdf(t *testing.T, body string, code int) {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "fakeqpdf")
	script := "#!/bin/sh\nprintf '" + body + "'\nexit " + itoa(code) + "\n"
	if err := os.WriteFile(path, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	orig := Binary
	Binary = path
	t.Cleanup(func() { Binary = orig })
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	return string(rune('0' + i))
}

func TestConcatStreamingStreamsAndTees(t *testing.T) {
	writeFakeQpdf(t, "MERGEDPDFBYTES", 0)
	teeDir := t.TempDir()
	teePath := filepath.Join(teeDir, "cache.tmp")

	var buf bytes.Buffer
	err := ConcatStreaming(context.Background(), []string{"a.pdf", "b.pdf"}, &buf, teePath)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if buf.String() != "MERGEDPDFBYTES" {
		t.Fatalf("streamed %q, want MERGEDPDFBYTES", buf.String())
	}
	teed, err := os.ReadFile(teePath)
	if err != nil || string(teed) != "MERGEDPDFBYTES" {
		t.Fatalf("tee file = %q err=%v", teed, err)
	}
}

func TestConcatStreamingNoChunks(t *testing.T) {
	var buf bytes.Buffer
	err := ConcatStreaming(context.Background(), nil, &buf, "")
	oe, ok := err.(*oerrors.Error)
	if !ok || oe.FailureClass != types.MergeFailed {
		t.Fatalf("expected MergeFailed, got %#v", err)
	}
}

func TestConcatStreamingQpdfFailureMapsToMergeError(t *testing.T) {
	writeFakeQpdf(t, "boom", 2)
	teePath := filepath.Join(t.TempDir(), "cache.tmp")
	var buf bytes.Buffer
	err := ConcatStreaming(context.Background(), []string{"a.pdf"}, &buf, teePath)
	oe, ok := err.(*oerrors.Error)
	if !ok || oe.FailureClass != types.MergeFailed {
		t.Fatalf("expected MergeFailed, got %#v", err)
	}
	// Partial tee file must be cleaned up on failure.
	if _, statErr := os.Stat(teePath); !os.IsNotExist(statErr) {
		t.Error("tee file should be removed on qpdf failure")
	}
}
