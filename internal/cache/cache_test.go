package cache

import (
	"os"
	"path/filepath"
	"testing"
)

func TestDisabledCacheIsNoOp(t *testing.T) {
	m, err := NewManager("", "v1")
	if err != nil {
		t.Fatal(err)
	}
	if m.Enabled() {
		t.Fatal("empty dir should be disabled")
	}
	if got := m.GetFinal("abc"); got != "" {
		t.Fatalf("disabled GetFinal = %q, want empty", got)
	}
	if err := m.PutFinal("abc", "/nonexistent"); err != nil {
		t.Fatalf("disabled PutFinal should be no-op, got %v", err)
	}
	if r := m.Clear(); r.Enabled {
		t.Fatal("disabled Clear should report Enabled=false")
	}
}

func TestFinalRoundTripAndClear(t *testing.T) {
	dir := t.TempDir()
	m, err := NewManager(dir, "26.4")
	if err != nil {
		t.Fatal(err)
	}
	if !m.Enabled() {
		t.Fatal("should be enabled")
	}

	src := filepath.Join(dir, "src.pdf")
	if err := os.WriteFile(src, []byte("%PDF-1.7 hello"), 0o644); err != nil {
		t.Fatal(err)
	}

	sha := "deadbeef"
	if got := m.GetFinal(sha); got != "" {
		t.Fatalf("miss expected, got %q", got)
	}
	if err := m.PutFinal(sha, src); err != nil {
		t.Fatal(err)
	}
	hit := m.GetFinal(sha)
	if hit == "" {
		t.Fatal("expected cache hit after PutFinal")
	}
	got, err := os.ReadFile(hit)
	if err != nil || string(got) != "%PDF-1.7 hello" {
		t.Fatalf("cached content = %q err=%v", got, err)
	}

	// Version namespacing: a different version must miss.
	m2, _ := NewManager(dir, "26.3")
	if got := m2.GetFinal(sha); got != "" {
		t.Fatal("different aspose version should not hit")
	}

	res := m.Clear()
	if !res.Enabled || res.FilesDeleted == 0 || res.BytesFreed == 0 {
		t.Fatalf("Clear result unexpected: %+v", res)
	}
	if got := m.GetFinal(sha); got != "" {
		t.Fatal("entry should be gone after Clear")
	}
}

func TestFinalTempPathAndFinalize(t *testing.T) {
	dir := t.TempDir()
	m, _ := NewManager(dir, "v1")
	tmp, err := m.FinalTempPath("abc123")
	if err != nil || tmp == "" {
		t.Fatalf("FinalTempPath: tmp=%q err=%v", tmp, err)
	}
	if err := os.WriteFile(tmp, []byte("streamed"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := m.FinalizeFinal("abc123", tmp); err != nil {
		t.Fatal(err)
	}
	if got := m.GetFinal("abc123"); got == "" {
		t.Fatal("finalize should publish the entry")
	}
}
