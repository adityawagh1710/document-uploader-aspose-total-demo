package planner

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/opus2/office-convert-orchestrator/internal/types"
)

// assertCompleteCover checks the core invariant from nfr-requirements.md §9:
// chunks are an ordered, complete, non-overlapping cover of [1..TotalPages].
func assertCompleteCover(t *testing.T, plan types.ChunkPlan) {
	t.Helper()
	if plan.TotalPages == 0 {
		require.Empty(t, plan.Chunks, "zero total pages should yield no chunks")
		return
	}
	expect := 1
	for i, c := range plan.Chunks {
		require.Equalf(t, expect, c.PageStart, "chunk %d start (gap or overlap)", i)
		require.GreaterOrEqualf(t, c.PageEnd, c.PageStart, "chunk %d end < start", i)
		expect = c.PageEnd + 1
	}
	require.Equal(t, plan.TotalPages, expect-1, "cover must end at TotalPages")
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
	assert.Empty(t, plan.Chunks)
	assert.Zero(t, plan.TotalPages)
}

func TestSubdivideHalvingAndFloor(t *testing.T) {
	// 10-page chunk halves into [1..5] and [6..10] with fractional index.
	parent := types.Chunk{Index: 3, PageStart: 1, PageEnd: 10}
	kids := Subdivide(parent)
	require.Len(t, kids, 2)
	assert.Equal(t, 1, kids[0].PageStart)
	assert.Equal(t, 5, kids[0].PageEnd)
	assert.Equal(t, 6, kids[1].PageStart)
	assert.Equal(t, 10, kids[1].PageEnd)
	assert.Equal(t, 3.0, kids[0].Index)
	assert.Equal(t, 3.5, kids[1].Index)
	// Single-page chunk is at the floor: no further subdivision.
	assert.Nil(t, Subdivide(types.Chunk{Index: 0, PageStart: 7, PageEnd: 7}), "single-page subdivide should return nil")
}

func TestChunkSHA256Deterministic(t *testing.T) {
	c := types.Chunk{Index: 0, PageStart: 1, PageEnd: 10}
	h1 := ChunkSHA256(c, "abc123", types.FormatDOCX)
	h2 := ChunkSHA256(c, "abc123", types.FormatDOCX)
	assert.Equal(t, h1, h2, "hash must be deterministic")
	// Page range participates in the key.
	other := ChunkSHA256(types.Chunk{PageStart: 1, PageEnd: 11}, "abc123", types.FormatDOCX)
	assert.NotEqual(t, h1, other, "hash ignored page range")
	// Format participates in the key.
	assert.NotEqual(t, h1, ChunkSHA256(c, "abc123", types.FormatPDF), "hash ignored format")
	assert.Len(t, h1, 64, "expected 64 hex chars")
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
		assert.Truef(t, c.NaturalSeam, "chunk %d should be marked as a natural seam", i)
	}
}
