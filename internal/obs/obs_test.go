package obs

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestWeightedPercent(t *testing.T) {
	// load fully done, half the chunks rendered, no merge:
	// 0.30*1 + 0.65*0.5 + 0.05*0 = 0.625
	jp := JobProgress{TotalChunks: 4, ChunksRendered: 2, LoadProgress: 1.0, Phase: "rendering"}
	assert.InDelta(t, 0.625, jp.WeightedPercent(), 1e-9)
	// complete short-circuits to 1.0.
	jp.Phase = "complete"
	assert.Equal(t, 1.0, jp.WeightedPercent(), "complete should weight to 1.0")
	// never exceeds 0.999 while in-flight.
	jp = JobProgress{TotalChunks: 1, ChunksRendered: 1, LoadProgress: 1.0, MergeDone: 1.0, Phase: "merging"}
	assert.Equal(t, 0.999, jp.WeightedPercent(), "in-flight should cap at 0.999")
}

func TestProgressUpdateMonotonicLoad(t *testing.T) {
	s := NewJobProgressStore()
	p := func(v float64) *float64 { return &v }
	s.Update("r1", ProgressUpdate{LoadProgress: p(0.8)})
	s.Update("r1", ProgressUpdate{LoadProgress: p(0.3)}) // must not regress
	assert.Equal(t, 0.8, s.Get("r1").LoadProgress, "load_progress must not regress")
}

func TestRecentPaginationCursor(t *testing.T) {
	s := NewRecentStore(200)
	// Record oldest->newest; store keeps newest-first.
	for i := 0; i < 5; i++ {
		s.Record(ConversionRecord{RequestID: id(i), CompletionTS: float64(i), Status: "success", Source: "ui"})
	}
	snap := s.Snapshot()
	require.Equal(t, "r4", snap[0].RequestID, "newest-first broken")

	page1 := Paginate(snap, nil, 2, s.Size())
	require.Len(t, page1.Entries, 2)
	require.True(t, page1.HasMore)
	require.NotNil(t, page1.NextCursor)
	cur := DecodeCursor(*page1.NextCursor)
	require.NotNil(t, cur, "cursor failed to decode")
	page2 := Paginate(snap, cur, 2, s.Size())
	require.Len(t, page2.Entries, 2)
	assert.Equal(t, "r2", page2.Entries[0].RequestID)

	// A cursor whose anchor isn't present is stale.
	stale := Paginate(snap, &Cursor{TS: 999, ID: "ghost"}, 2, s.Size())
	assert.True(t, stale.StaleCursor, "expected stale cursor")
}

func TestRingStoreCapAndForget(t *testing.T) {
	s := NewRingStore(3, 0) // ttl=0 means everything is immediately expired on the next op
	_ = s
	s2 := NewRingStore(3, 1<<62) // effectively no TTL
	for i := 0; i < 5; i++ {
		s2.Record("r", Event{"i": i})
	}
	got := s2.Get("r")
	require.Len(t, got, 3, "cap not enforced")
	assert.Equal(t, 2, got[0]["i"], "kept wrong window (oldest two should be evicted)")
	s2.Forget("r")
	assert.Empty(t, s2.Get("r"), "forget failed")
}

func id(i int) string { return "r" + string(rune('0'+i)) }
