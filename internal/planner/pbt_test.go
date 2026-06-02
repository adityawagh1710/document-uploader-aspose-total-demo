package planner

import (
	"testing"

	"pgregory.net/rapid"

	"github.com/opus2/office-convert-orchestrator/internal/types"
)

// Property-based parity tests (re-expressing the Python PBT invariants from
// nfr-requirements.md §9 in pgregory.net/rapid).

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
			if c.PageStart != expect {
				t.Fatalf("chunk %d starts at %d, expected %d", i, c.PageStart, expect)
			}
			if c.PageEnd < c.PageStart {
				t.Fatalf("chunk %d end %d < start %d", i, c.PageEnd, c.PageStart)
			}
			// Invariant 2: no chunk exceeds maxPages (page-range split path).
			if c.Pages() > maxPages {
				t.Fatalf("chunk %d has %d pages > maxPages %d", i, c.Pages(), maxPages)
			}
			expect = c.PageEnd + 1
		}
		if expect-1 != pages {
			t.Fatalf("cover ends at %d, expected %d", expect-1, pages)
		}
		if plan.TotalPages != pages {
			t.Fatalf("TotalPages = %d, want %d", plan.TotalPages, pages)
		}
	})
}

func TestProp_SubdivideHalvingCoversParent(t *testing.T) {
	rapid.Check(t, func(t *rapid.T) {
		start := rapid.IntRange(1, 10_000).Draw(t, "start")
		span := rapid.IntRange(1, 10_000).Draw(t, "span")
		parent := types.Chunk{Index: float64(rapid.IntRange(0, 100).Draw(t, "idx")), PageStart: start, PageEnd: start + span - 1}

		kids := Subdivide(parent)
		if span <= 1 {
			if kids != nil {
				t.Fatalf("single-page chunk should not subdivide, got %v", kids)
			}
			return
		}
		if len(kids) != 2 {
			t.Fatalf("expected 2 children, got %d", len(kids))
		}
		// Children exactly cover the parent, contiguously, no gap/overlap.
		if kids[0].PageStart != parent.PageStart {
			t.Fatalf("left start %d != parent start %d", kids[0].PageStart, parent.PageStart)
		}
		if kids[1].PageEnd != parent.PageEnd {
			t.Fatalf("right end %d != parent end %d", kids[1].PageEnd, parent.PageEnd)
		}
		if kids[1].PageStart != kids[0].PageEnd+1 {
			t.Fatalf("gap/overlap: left ends %d, right starts %d", kids[0].PageEnd, kids[1].PageStart)
		}
		// Index discipline: left keeps the parent index, right gets +0.5.
		if kids[0].Index != parent.Index || kids[1].Index != parent.Index+0.5 {
			t.Fatalf("indices = %v,%v want %v,%v", kids[0].Index, kids[1].Index, parent.Index, parent.Index+0.5)
		}
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
		if h1 != h2 {
			t.Fatalf("non-deterministic: %s != %s", h1, h2)
		}
		if len(h1) != 64 {
			t.Fatalf("expected 64 hex chars, got %d", len(h1))
		}
		// A different page range must change the key.
		if ChunkSHA256(types.Chunk{PageStart: start, PageEnd: end + 1}, sha, format) == h1 {
			t.Fatal("hash insensitive to page range")
		}
	})
}
