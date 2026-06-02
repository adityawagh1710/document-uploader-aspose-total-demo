package worker

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"testing"

	"github.com/opus2/office-convert-orchestrator/internal/config"
	"github.com/opus2/office-convert-orchestrator/internal/obs"
	"github.com/opus2/office-convert-orchestrator/internal/oerrors"
	"github.com/opus2/office-convert-orchestrator/internal/types"
)

// buildFakeWorker compiles the fake worker binary to <dir>/worker-<fmt> for the
// requested formats and returns the prefix path. Skips the test if `go` or the
// build is unavailable.
func buildFakeWorker(t *testing.T, formats ...types.FormatName) string {
	t.Helper()
	dir := t.TempDir()
	base := filepath.Join(dir, "worker")
	// Build once, then copy to each per-format name.
	first := base + "-" + string(formats[0])
	cmd := exec.Command("go", "build", "-o", first, "./testdata/fakeworker")
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		t.Skipf("cannot build fake worker: %v", err)
	}
	for _, f := range formats[1:] {
		b, err := os.ReadFile(first)
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(base+"-"+string(f), b, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	return base
}

func testSettings(prefix string) *config.Settings {
	return &config.Settings{
		WorkerBinaryPrefix:  prefix,
		WorkerRAMBytes:      16 << 30, // generous so prlimit --as doesn't choke the Go fake worker
		ChunkTimeoutSeconds: 30,
		LicensePath:         "/tmp/fake.lic",
		Parallel:            2,
	}
}

func testStores() Stores {
	return Stores{
		Heartbeats: obs.HeartbeatStore(),
		Timings:    obs.TimingStore(),
		Progress:   obs.NewJobProgressStore(),
	}
}

func mkInput(t *testing.T) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "input.docx")
	if err := os.WriteFile(p, []byte("fake document bytes"), 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

func renderChunks(t *testing.T, pool Pool, scratch string, n int) []string {
	t.Helper()
	paths := make([]string, n)
	var wg sync.WaitGroup
	var mu sync.Mutex
	var firstErr error
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			c := types.Chunk{Index: float64(i), PageStart: i*10 + 1, PageEnd: i*10 + 10}
			p, err := pool.RenderChunk(context.Background(), c, scratch)
			mu.Lock()
			defer mu.Unlock()
			if err != nil && firstErr == nil {
				firstErr = err
			}
			paths[i] = p
		}(i)
	}
	wg.Wait()
	if firstErr != nil {
		t.Fatalf("render error: %v", firstErr)
	}
	return paths
}

func TestForkedWorkerPoolRendersConcurrently(t *testing.T) {
	prefix := buildFakeWorker(t, types.FormatDOCX)
	s := testSettings(prefix)
	stores := testStores()
	scratch := t.TempDir()

	pool, err := NewForkedWorkerPool(s, types.FormatDOCX, mkInput(t), "rid-fork", stores, 2)
	if err != nil {
		t.Fatalf("spawn: %v", err)
	}
	defer pool.Close(context.Background())

	if pc, ok := pool.ActualPageCount(); !ok || pc != 5 {
		t.Fatalf("page count = %d, ok=%v, want 5", pc, ok)
	}

	paths := renderChunks(t, pool, scratch, 3)
	for i, p := range paths {
		if p == "" {
			t.Fatalf("chunk %d returned empty path", i)
		}
		if b, err := os.ReadFile(p); err != nil || len(b) == 0 {
			t.Fatalf("chunk %d output missing: %v", i, err)
		}
	}

	// The fake worker emits one load heartbeat; the stderr tailer must have
	// recorded it under the request id (the GIL->mutex store path).
	if hbs := stores.Heartbeats.Get("rid-fork"); len(hbs) == 0 {
		t.Error("expected at least one heartbeat recorded for rid-fork")
	}
}

func TestWorkerPoolLegacyRenders(t *testing.T) {
	prefix := buildFakeWorker(t, types.FormatXLSX)
	s := testSettings(prefix)
	scratch := t.TempDir()

	pool, err := NewWorkerPool(s, types.FormatXLSX, mkInput(t), "rid-legacy", testStores(), 2)
	if err != nil {
		t.Fatalf("spawn: %v", err)
	}
	defer pool.Close(context.Background())

	if pc, ok := pool.ActualPageCount(); !ok || pc != 5 {
		t.Fatalf("page count = %d ok=%v, want 5", pc, ok)
	}
	paths := renderChunks(t, pool, scratch, 4)
	for i, p := range paths {
		if b, err := os.ReadFile(p); err != nil || len(b) == 0 {
			t.Fatalf("chunk %d output missing: %v", i, err)
		}
	}
}

func TestLegacyPoolHeartbeatPoolIndexDistinct(t *testing.T) {
	// The fake worker emits pool_index=0 in every heartbeat (each legacy worker
	// is its own process). The legacy pool MUST override with each worker's
	// assigned index, so the dashboard shows N distinct workers — not all
	// collapsed onto pool_index 0 (the bug the UI surfaced).
	prefix := buildFakeWorker(t, types.FormatXLSX)
	s := testSettings(prefix)
	stores := testStores()
	scratch := t.TempDir()

	pool, err := NewWorkerPool(s, types.FormatXLSX, mkInput(t), "rid-idx", stores, 3)
	if err != nil {
		t.Fatalf("spawn: %v", err)
	}
	defer pool.Close(context.Background())
	renderChunks(t, pool, scratch, 3)

	idxs := map[int]bool{}
	for _, hb := range stores.Heartbeats.Get("rid-idx") {
		if v, ok := hb["pool_index"]; ok {
			idxs[toInt(v, -1)] = true
		}
	}
	if len(idxs) < 2 {
		t.Fatalf("legacy workers collapsed onto pool_index %v — expected distinct indices", idxs)
	}
}

func TestForkedPoolMapsOOMError(t *testing.T) {
	prefix := buildFakeWorker(t, types.FormatDOCX)
	t.Setenv("FAKE_EXIT_CODE", "137") // worker replies error code 137 on render
	s := testSettings(prefix)
	scratch := t.TempDir()

	pool, err := NewForkedWorkerPool(s, types.FormatDOCX, mkInput(t), "rid-oom", testStores(), 1)
	if err != nil {
		t.Fatalf("spawn: %v", err)
	}
	defer pool.Close(context.Background())

	_, rerr := pool.RenderChunk(context.Background(), types.Chunk{Index: 0, PageStart: 1, PageEnd: 10}, scratch)
	if rerr == nil {
		t.Fatal("expected render error")
	}
	oe, ok := rerr.(*oerrors.Error)
	if !ok || !oe.OOM {
		t.Fatalf("expected OOM error, got %#v", rerr)
	}
}
