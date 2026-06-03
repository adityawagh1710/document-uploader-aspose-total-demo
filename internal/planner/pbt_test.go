package planner

import (
	"testing"

	"github.com/stretchr/testify/require"
	"pgregory.net/rapid"

	"github.com/opus2/office-convert-orchestrator/internal/types"
)

// Property-based parity tests (re-expressing the Python PBT invariants from
// nfr-requirements.md §9 in pgregory.net/rapid). testify's require works against
// *rapid.T (it satisfies require.TestingT via Errorf + FailNow), so a violated
// property fails the property and feeds rapid's shrinker.

var allFormats = []types.FormatName{types.FormatDOCX, types.FormatPPTX, types.FormatXLSX, types.FormatPDF}

func TestProp_PlanChunksCompleteCover(t *testing.T) {
	rapid.Check(t, func(t *rapid.T) {
		pages := rapid.IntRange(1, 50_000).Draw(t, "pages")
		size := rapid.Int64Range(1, 10<<30).Draw(t, "size")
		format := rapid.SampledFrom(allFormats).Draw(t, "format")
		maxPages := rapid.IntRange(1, 6000).Draw(t, "maxPages")
		maxMB := rapid.IntRange(1, 1000).Draw(t, "maxMB")

		probe := types.ProbeResult{PageCount: pages, Format: format, SizeBytes: size}
		plan := PlanChunks(probe, maxPages, maxMB)

		// Invariant 1: ordered, complete, non-overlapping cover of [1..pages].
		expect := 1
		for i, c := range plan.Chunks {
			require.Equalf(t, expect, c.PageStart, "chunk %d start (gap or overlap)", i)
			require.GreaterOrEqualf(t, c.PageEnd, c.PageStart, "chunk %d end < start", i)
			// Invariant 2: no chunk exceeds maxPages (page-range split path).
			require.LessOrEqualf(t, c.Pages(), maxPages, "chunk %d pages > maxPages", i)
			expect = c.PageEnd + 1
		}
		require.Equal(t, pages, expect-1, "cover must end at pages")
		require.Equal(t, pages, plan.TotalPages, "TotalPages mismatch")
	})
}

func TestProp_SubdivideHalvingCoversParent(t *testing.T) {
	rapid.Check(t, func(t *rapid.T) {
		start := rapid.IntRange(1, 10_000).Draw(t, "start")
		span := rapid.IntRange(1, 10_000).Draw(t, "span")
		parent := types.Chunk{Index: float64(rapid.IntRange(0, 100).Draw(t, "idx")), PageStart: start, PageEnd: start + span - 1}

		kids := Subdivide(parent)
		if span <= 1 {
			require.Nil(t, kids, "single-page chunk should not subdivide")
			return
		}
		require.Len(t, kids, 2)
		// Children exactly cover the parent, contiguously, no gap/overlap.
		require.Equal(t, parent.PageStart, kids[0].PageStart, "left start != parent start")
		require.Equal(t, parent.PageEnd, kids[1].PageEnd, "right end != parent end")
		require.Equal(t, kids[0].PageEnd+1, kids[1].PageStart, "gap/overlap between children")
		// Index discipline: left keeps the parent index, right gets +0.5.
		require.Equal(t, parent.Index, kids[0].Index, "left index")
		require.Equal(t, parent.Index+0.5, kids[1].Index, "right index")
	})
}

func TestProp_ChunkSHA256Stable(t *testing.T) {
	rapid.Check(t, func(t *rapid.T) {
		start := rapid.IntRange(1, 1000).Draw(t, "start")
		end := start + rapid.IntRange(0, 1000).Draw(t, "extra")
		sha := rapid.StringMatching(`[a-f0-9]{8,64}`).Draw(t, "sha")
		format := rapid.SampledFrom(allFormats).Draw(t, "format")
		c := types.Chunk{PageStart: start, PageEnd: end}

		h1 := ChunkSHA256(c, sha, format)
		h2 := ChunkSHA256(c, sha, format)
		require.Equal(t, h1, h2, "hash must be deterministic")
		require.Len(t, h1, 64, "expected 64 hex chars")
		// A different page range must change the key.
		require.NotEqual(t, h1, ChunkSHA256(types.Chunk{PageStart: start, PageEnd: end + 1}, sha, format), "hash insensitive to page range")
	})
}
