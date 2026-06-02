// Package planner is the pure chunk-planning algorithm.
//
// Ported verbatim from office_convert/chunk_planner.py. Implements FR-3 (chunk
// planning) and FR-4 (subdivision retry helper). Pure functions: no I/O, no
// subprocess, no Aspose. Property-based tests verify invariants per
// nfr-requirements.md §9.
//
// Memory cost formula (Functional Design Q1 = B): pro-rated by input size and
// amplified by a per-format factor. A per-page floor was rejected; outlier
// pages are caught by the subdivision-on-OOM retry path instead.
package planner

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"

	"github.com/opus2/office-convert-orchestrator/internal/types"
)

// Amplification is the per-format rendered-size multiplier.
var Amplification = map[types.FormatName]int{
	types.FormatDOCX: 5,
	types.FormatPPTX: 8,
	types.FormatXLSX: 4,
	types.FormatPDF:  2,
}

// SubprocessOverheadMB is the per-format fixed overhead in MB each subprocess
// consumes regardless of chunk size (Aspose product init + full document load
// + license validation). Used by AdaptiveMaxPages.
var SubprocessOverheadMB = map[types.FormatName]int{
	types.FormatDOCX: 200,
	types.FormatPPTX: 300,
	types.FormatXLSX: 250,
	types.FormatPDF:  100,
}

// MinChunks is the minimum number of chunks to produce (ensures some
// parallelism even for files that fit entirely in RAM).
const MinChunks = 2

// MaxPagesCeiling is the per-format ceiling on adaptive chunk size, tuned to
// each format's cost model. See chunk_planner.py for the full rationale
// (fork-after-load O(total_pages) per-chunk cost for DOCX/PPTX/PDF; XLSX is
// fork-unsafe and re-loads per subprocess).
var MaxPagesCeiling = map[types.FormatName]int{
	types.FormatDOCX: 5000,
	types.FormatPPTX: 5000,
	types.FormatPDF:  2000,
	types.FormatXLSX: 2000,
}

const (
	balanceFactor          = 1.5
	subdivisionFloorPages  = 1
	maxPagesCeilingDefault = 5000
)

// EstimateChunkMB returns the pro-rated rendered-MB estimate (functional-design
// Q1 = B).
func EstimateChunkMB(pagesInChunk, totalPages int, inputSizeBytes int64, format types.FormatName) float64 {
	if totalPages <= 0 || pagesInChunk <= 0 {
		return 0.0
	}
	perPageBytes := float64(inputSizeBytes) / float64(totalPages)
	return (perPageBytes * float64(pagesInChunk) * float64(Amplification[format])) / (1024 * 1024)
}

// AdaptiveMaxPagesOptions carries the optional overrides for AdaptiveMaxPages.
// MaxPagesCeiling <= 0 means "look up the per-format ceiling". MinPagesFloor
// defaults to 10 when <= 0 (matching the Python default).
type AdaptiveMaxPagesOptions struct {
	MaxPagesCeiling int
	MinPagesFloor   int
}

// AdaptiveMaxPages computes the largest chunk size (in pages) that fits in the
// RAM budget. Mirrors chunk_planner.adaptive_max_pages.
func AdaptiveMaxPages(probe types.ProbeResult, workerRAMBytes int64, parallel int, opts AdaptiveMaxPagesOptions) int {
	format := probe.Format
	minPagesFloor := opts.MinPagesFloor
	if minPagesFloor <= 0 {
		minPagesFloor = 10
	}
	maxPagesCeiling := opts.MaxPagesCeiling
	if maxPagesCeiling <= 0 {
		var ok bool
		maxPagesCeiling, ok = MaxPagesCeiling[format]
		if !ok {
			maxPagesCeiling = maxPagesCeilingDefault
		}
	}

	if probe.PageCount <= 0 || probe.SizeBytes <= 0 {
		return minPagesFloor
	}

	amp := Amplification[format]
	overheadMB := SubprocessOverheadMB[format]

	// Available MB for actual page rendering per worker (subtract overhead).
	workerBudgetMB := (float64(workerRAMBytes) / (1024 * 1024)) * 0.75 // 75% safety margin
	renderBudgetMB := workerBudgetMB - float64(overheadMB)
	if renderBudgetMB < 50.0 {
		renderBudgetMB = 50.0
	}

	// Per-page cost estimate.
	perPageBytes := float64(probe.SizeBytes) / float64(probe.PageCount)
	perPageRenderedMB := (perPageBytes * float64(amp)) / (1024 * 1024)
	if perPageRenderedMB <= 0 {
		return maxPagesCeiling
	}

	// How many pages fit in the render budget.
	pagesByRAM := int(renderBudgetMB / perPageRenderedMB)

	var optimal int
	if probe.PageCount > minPagesFloor {
		desiredChunks := MinChunks
		if parallel > desiredChunks {
			desiredChunks = parallel
		}
		pagesByParallelism := probe.PageCount / desiredChunks
		// Take the smaller of RAM-limited and parallelism-limited.
		optimal = pagesByRAM
		if pagesByParallelism < optimal {
			optimal = pagesByParallelism
		}
	} else {
		optimal = pagesByRAM
	}

	// Clamp into [minPagesFloor, maxPagesCeiling].
	if optimal > maxPagesCeiling {
		optimal = maxPagesCeiling
	}
	if optimal < minPagesFloor {
		optimal = minPagesFloor
	}
	return optimal
}

// PlanChunks produces a deterministic chunk plan. Mirrors
// chunk_planner.plan_chunks. Defaults: maxPagesPerChunk=10, maxMBPerChunk=50.
func PlanChunks(probe types.ProbeResult, maxPagesPerChunk, maxMBPerChunk int) types.ChunkPlan {
	if probe.PageCount <= 0 {
		return types.ChunkPlan{Chunks: nil, TotalPages: 0, EstimatedMB: 0.0}
	}

	if len(probe.NaturalSeams) > 0 {
		seamPlan := groupSeams(probe, maxPagesPerChunk, maxMBPerChunk)
		if isBalanced(seamPlan, maxPagesPerChunk, maxMBPerChunk) {
			return seamPlan
		}
	}

	return pageRangeSplit(probe, maxPagesPerChunk, maxMBPerChunk)
}

// Subdivide performs binary halving subdivision. Returns nil at the single-page
// floor. Mirrors chunk_planner.subdivide.
func Subdivide(chunk types.Chunk) []types.Chunk {
	start, end := chunk.PageStart, chunk.PageEnd
	span := end - start + 1
	if span <= subdivisionFloorPages {
		return nil
	}
	half := (span + 1) / 2 // ceiling
	mid := start + half - 1
	return []types.Chunk{
		{Index: chunk.Index, PageStart: start, PageEnd: mid, NaturalSeam: false},
		{Index: chunk.Index + 0.5, PageStart: mid + 1, PageEnd: end, NaturalSeam: false},
	}
}

// ChunkSHA256 is the stable hash for the per-chunk cache key. Deterministic by
// construction. Mirrors chunk_planner.chunk_sha256 byte-for-byte.
func ChunkSHA256(chunk types.Chunk, sourceSHA256 string, format types.FormatName) string {
	h := sha256.New()
	h.Write([]byte(sourceSHA256))
	h.Write([]byte(":"))
	h.Write([]byte(fmt.Sprintf("%d-%d", chunk.PageStart, chunk.PageEnd)))
	h.Write([]byte(":"))
	h.Write([]byte(string(format)))
	return hex.EncodeToString(h.Sum(nil))
}

// isBalanced reports whether every chunk stays within balanceFactor.
func isBalanced(plan types.ChunkPlan, maxPages, maxMB int) bool {
	if len(plan.Chunks) == 0 {
		return true
	}
	pagesThreshold := float64(maxPages) * balanceFactor
	mbThreshold := float64(maxMB) * balanceFactor
	for _, c := range plan.Chunks {
		if float64(c.Pages()) > pagesThreshold {
			return false
		}
	}
	for _, c := range plan.Chunks {
		if chunkMB(c, plan) > mbThreshold {
			return false
		}
	}
	return true
}

// chunkMB approximates per-chunk MB by pro-rating the plan's summed estimate.
func chunkMB(chunk types.Chunk, plan types.ChunkPlan) float64 {
	if plan.TotalPages == 0 {
		return 0.0
	}
	return plan.EstimatedMB * (float64(chunk.Pages()) / float64(plan.TotalPages))
}

// groupSeams groups consecutive natural seams greedily under the bounds.
func groupSeams(probe types.ProbeResult, maxPages, maxMB int) types.ChunkPlan {
	var chunks []types.Chunk
	pendingStart := -1 // -1 == None
	pendingEnd := 0
	pendingPages := 0

	for _, seam := range probe.NaturalSeams {
		seamStart, seamEnd := seam[0], seam[1]
		seamPages := seamEnd - seamStart + 1

		if pendingStart == -1 {
			pendingStart = seamStart
			pendingEnd = seamEnd
			pendingPages = seamPages
			continue
		}

		combinedPages := pendingPages + seamPages
		combinedMB := EstimateChunkMB(combinedPages, probe.PageCount, probe.SizeBytes, probe.Format)
		if combinedPages <= maxPages && combinedMB <= float64(maxMB) {
			pendingEnd = seamEnd
			pendingPages = combinedPages
		} else {
			chunks = append(chunks, types.Chunk{
				Index:       float64(len(chunks)),
				PageStart:   pendingStart,
				PageEnd:     pendingEnd,
				NaturalSeam: true,
			})
			pendingStart = seamStart
			pendingEnd = seamEnd
			pendingPages = seamPages
		}
	}

	if pendingStart != -1 {
		chunks = append(chunks, types.Chunk{
			Index:       float64(len(chunks)),
			PageStart:   pendingStart,
			PageEnd:     pendingEnd,
			NaturalSeam: true,
		})
	}

	totalMB := 0.0
	for _, c := range chunks {
		totalMB += EstimateChunkMB(c.Pages(), probe.PageCount, probe.SizeBytes, probe.Format)
	}
	return types.ChunkPlan{Chunks: chunks, TotalPages: probe.PageCount, EstimatedMB: totalMB}
}

// pageRangeSplit is a greedy page-range split bounded by maxPages and maxMB.
func pageRangeSplit(probe types.ProbeResult, maxPages, maxMB int) types.ChunkPlan {
	var chunks []types.Chunk
	cursor := 1
	for cursor <= probe.PageCount {
		// Grow the chunk from cursor forward.
		end := cursor
		for end+1 <= probe.PageCount {
			candidatePages := end + 1 - cursor + 1
			if candidatePages > maxPages {
				break
			}
			candidateMB := EstimateChunkMB(candidatePages, probe.PageCount, probe.SizeBytes, probe.Format)
			if candidateMB > float64(maxMB) {
				break
			}
			end++
		}
		chunks = append(chunks, types.Chunk{
			Index:       float64(len(chunks)),
			PageStart:   cursor,
			PageEnd:     end,
			NaturalSeam: false,
		})
		cursor = end + 1
	}

	totalMB := 0.0
	for _, c := range chunks {
		totalMB += EstimateChunkMB(c.Pages(), probe.PageCount, probe.SizeBytes, probe.Format)
	}
	return types.ChunkPlan{Chunks: chunks, TotalPages: probe.PageCount, EstimatedMB: totalMB}
}
