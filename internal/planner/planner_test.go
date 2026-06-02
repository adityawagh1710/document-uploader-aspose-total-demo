package planner

import (
	"testing"

	"github.com/opus2/office-convert-orchestrator/internal/types"
)

// assertCompleteCover checks the core invariant from nfr-requirements.md §9:
// chunks are an ordered, complete, non-overlapping cover of [1..TotalPages].
func assertCompleteCover(t *testing.T, plan types.ChunkPlan) {
	t.Helper()
	if plan.TotalPages == 0 {
		if len(plan.Chunks) != 0 {
			t.Fatalf("zero total pages but %d chunks", len(plan.Chunks))
		}
		return
	}
	expect := 1
	for i, c := range plan.Chunks {
		if c.PageStart != expect {
			t.Fatalf("chunk %d starts at %d, expected %d (gap or overlap)", i, c.PageStart, expect)
		}
		if c.PageEnd < c.PageStart {
			t.Fatalf("chunk %d has end %d < start %d", i, c.PageEnd, c.PageStart)
		}
		expect = c.PageEnd + 1
	}
	if expect-1 != plan.TotalPages {
		t.Fatalf("cover ends at %d, expected %d", expect-1, plan.TotalPages)
	}
}

func TestPlanChunksCompleteCover(t *testing.T) {
	cases := []struct {
		name      string
		pageCount int
		sizeBytes int64
		format    types.FormatName
		maxPages  int
		maxMB     int
	}{
		{"tiny docx", 1, 50_000, types.FormatDOCX, 10, 50},
		{"medium pptx", 100, 8_500_000, types.FormatPPTX, 10, 50},
		{"large xlsx", 23_637, 98_000_000, types.FormatXLSX, 500, 50},
		{"pdf even split", 40, 5_000_000, types.FormatPDF, 10, 50},
		{"single big page", 1, 200_000_000, types.FormatDOCX, 10, 50},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			probe := types.ProbeResult{PageCount: tc.pageCount, Format: tc.format, SizeBytes: tc.sizeBytes}
			plan := PlanChunks(probe, tc.maxPages, tc.maxMB)
			assertCompleteCover(t, plan)
		})
	}
}

func TestPlanChunksEmpty(t *testing.T) {
	plan := PlanChunks(types.ProbeResult{PageCount: 0, Format: types.FormatDOCX}, 10, 50)
	if len(plan.Chunks) != 0 || plan.TotalPages != 0 {
		t.Fatalf("expected empty plan, got %+v", plan)
	}
}

func TestSubdivideHalvingAndFloor(t *testing.T) {
	// 10-page chunk halves into [1..5] and [6..10] with fractional index.
	parent := types.Chunk{Index: 3, PageStart: 1, PageEnd: 10}
	kids := Subdivide(parent)
	if len(kids) != 2 {
		t.Fatalf("expected 2 children, got %d", len(kids))
	}
	if kids[0].PageStart != 1 || kids[0].PageEnd != 5 {
		t.Fatalf("left child = [%d..%d], want [1..5]", kids[0].PageStart, kids[0].PageEnd)
	}
	if kids[1].PageStart != 6 || kids[1].PageEnd != 10 {
		t.Fatalf("right child = [%d..%d], want [6..10]", kids[1].PageStart, kids[1].PageEnd)
	}
	if kids[0].Index != 3 || kids[1].Index != 3.5 {
		t.Fatalf("child indices = %v,%v want 3,3.5", kids[0].Index, kids[1].Index)
	}
	// Single-page chunk is at the floor: no further subdivision.
	if got := Subdivide(types.Chunk{Index: 0, PageStart: 7, PageEnd: 7}); got != nil {
		t.Fatalf("single-page subdivide should return nil, got %+v", got)
	}
}

func TestChunkSHA256Deterministic(t *testing.T) {
	c := types.Chunk{Index: 0, PageStart: 1, PageEnd: 10}
	h1 := ChunkSHA256(c, "abc123", types.FormatDOCX)
	h2 := ChunkSHA256(c, "abc123", types.FormatDOCX)
	if h1 != h2 {
		t.Fatalf("non-deterministic hash: %s != %s", h1, h2)
	}
	// Page range participates in the key.
	other := ChunkSHA256(types.Chunk{PageStart: 1, PageEnd: 11}, "abc123", types.FormatDOCX)
	if h1 == other {
		t.Fatal("hash ignored page range")
	}
	// Format participates in the key.
	if ChunkSHA256(c, "abc123", types.FormatPDF) == h1 {
		t.Fatal("hash ignored format")
	}
	if len(h1) != 64 {
		t.Fatalf("expected 64 hex chars, got %d", len(h1))
	}
}

func TestSeamPlanUsedWhenBalanced(t *testing.T) {
	// Three small natural seams within bounds -> seam plan honored, marked natural.
	probe := types.ProbeResult{
		PageCount:    9,
		Format:       types.FormatDOCX,
		SizeBytes:    900_000,
		NaturalSeams: [][2]int{{1, 3}, {4, 6}, {7, 9}},
	}
	plan := PlanChunks(probe, 5, 50)
	assertCompleteCover(t, plan)
	for i, c := range plan.Chunks {
		if !c.NaturalSeam {
			t.Fatalf("chunk %d should be marked as a natural seam", i)
		}
	}
}
