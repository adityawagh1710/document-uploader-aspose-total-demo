package obs

import (
	"math"
	"testing"
)

func TestWeightedPercent(t *testing.T) {
	// load fully done, half the chunks rendered, no merge:
	// 0.30*1 + 0.65*0.5 + 0.05*0 = 0.625
	jp := JobProgress{TotalChunks: 4, ChunksRendered: 2, LoadProgress: 1.0, Phase: "rendering"}
	if got := jp.WeightedPercent(); math.Abs(got-0.625) > 1e-9 {
		t.Fatalf("weighted = %v, want 0.625", got)
	}
	// complete short-circuits to 1.0.
	jp.Phase = "complete"
	if got := jp.WeightedPercent(); got != 1.0 {
		t.Fatalf("complete weighted = %v, want 1.0", got)
	}
	// never exceeds 0.999 while in-flight.
	jp = JobProgress{TotalChunks: 1, ChunksRendered: 1, LoadProgress: 1.0, MergeDone: 1.0, Phase: "merging"}
	if got := jp.WeightedPercent(); got != 0.999 {
		t.Fatalf("in-flight cap = %v, want 0.999", got)
	}
}

func TestProgressUpdateMonotonicLoad(t *testing.T) {
	s := NewJobProgressStore()
	p := func(v float64) *float64 { return &v }
	s.Update("r1", ProgressUpdate{LoadProgress: p(0.8)})
	s.Update("r1", ProgressUpdate{LoadProgress: p(0.3)}) // must not regress
	if got := s.Get("r1").LoadProgress; got != 0.8 {
		t.Fatalf("load_progress regressed to %v, want 0.8", got)
	}
}

func TestRecentPaginationCursor(t *testing.T) {
	s := NewRecentStore(200)
	// Record oldest->newest; store keeps newest-first.
	for i := 0; i < 5; i++ {
		s.Record(ConversionRecord{RequestID: id(i), CompletionTS: float64(i), Status: "success", Source: "ui"})
	}
	snap := s.Snapshot()
	if snap[0].RequestID != "r4" {
		t.Fatalf("newest-first broken: head=%s", snap[0].RequestID)
	}

	page1 := Paginate(snap, nil, 2, s.Size())
	if len(page1.Entries) != 2 || !page1.HasMore || page1.NextCursor == nil {
		t.Fatalf("page1 unexpected: %+v", page1)
	}
	cur := DecodeCursor(*page1.NextCursor)
	if cur == nil {
		t.Fatal("cursor failed to decode")
	}
	page2 := Paginate(snap, cur, 2, s.Size())
	if len(page2.Entries) != 2 || page2.Entries[0].RequestID != "r2" {
		t.Fatalf("page2 unexpected: %+v", page2)
	}

	// A cursor whose anchor isn't present is stale.
	stale := Paginate(snap, &Cursor{TS: 999, ID: "ghost"}, 2, s.Size())
	if !stale.StaleCursor {
		t.Fatal("expected stale cursor")
	}
}

func TestRingStoreCapAndForget(t *testing.T) {
	s := NewRingStore(3, 0) // ttl=0 means everything is immediately expired on the next op
	_ = s
	s2 := NewRingStore(3, 1<<62) // effectively no TTL
	for i := 0; i < 5; i++ {
		s2.Record("r", Event{"i": i})
	}
	got := s2.Get("r")
	if len(got) != 3 {
		t.Fatalf("cap not enforced: %d entries", len(got))
	}
	if got[0]["i"] != 2 { // oldest two evicted
		t.Fatalf("kept wrong window: first=%v", got[0]["i"])
	}
	s2.Forget("r")
	if len(s2.Get("r")) != 0 {
		t.Fatal("forget failed")
	}
}

func id(i int) string { return "r" + string(rune('0'+i)) }
