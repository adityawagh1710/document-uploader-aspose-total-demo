package obs

import (
	"sync"
	"time"
)

const progressTTL = 30 * time.Minute

// JobProgress carries enough state for the UI to render a weighted progress bar
// across the three phases of a pool-mode conversion:
//
//	load (30%) -> render N/M chunks (65%) -> merge (5%)
//
// Ported from job_progress.py JobProgress.
type JobProgress struct {
	TotalChunks    int     `json:"total_chunks"`
	ChunksRendered int     `json:"chunks_rendered"`
	Phase          string  `json:"phase"` // init|probing|planning|loading|rendering|merging|complete
	LoadProgress   float64 `json:"load_progress"`
	MergeDone      float64 `json:"merge_done"`
	StartedAt      float64 `json:"started_at"` // unix epoch seconds

	lastTouched time.Time
}

// WeightedPercent mirrors JobProgress.weighted_percent.
func (j JobProgress) WeightedPercent() float64 {
	chunkPct := 0.0
	if j.TotalChunks > 0 {
		chunkPct = float64(j.ChunksRendered) / float64(j.TotalChunks)
		if chunkPct > 1.0 {
			chunkPct = 1.0
		}
	}
	pct := 0.30*j.LoadProgress + 0.65*chunkPct + 0.05*j.MergeDone
	if j.Phase == "complete" {
		return 1.0
	}
	if pct < 0.0 {
		pct = 0.0
	}
	if pct > 0.999 {
		pct = 0.999
	}
	return pct
}

// ToDict mirrors JobProgress.to_dict (adds weighted_percent + elapsed_s).
func (j JobProgress) ToDict() map[string]any {
	return map[string]any{
		"total_chunks":     j.TotalChunks,
		"chunks_rendered":  j.ChunksRendered,
		"phase":            j.Phase,
		"load_progress":    j.LoadProgress,
		"merge_done":       j.MergeDone,
		"started_at":       j.StartedAt,
		"weighted_percent": j.WeightedPercent(),
		"elapsed_s":        max64(0.0, nowEpoch()-j.StartedAt),
	}
}

// ProgressUpdate carries the optional fields for a progress update. Nil pointer
// means "leave unchanged"; IncrementChunks adds to chunks_rendered.
type ProgressUpdate struct {
	TotalChunks     *int
	Phase           *string
	LoadProgress    *float64
	MergeDone       *float64
	IncrementChunks int
}

// JobProgressStore is a thread-safe per-request progress state with lazy TTL
// eviction. Ported from job_progress.py JobProgressStore.
type JobProgressStore struct {
	ttl   time.Duration
	mu    sync.Mutex
	store map[string]*JobProgress
}

// NewJobProgressStore constructs the store with the default 30-min TTL.
func NewJobProgressStore() *JobProgressStore {
	return &JobProgressStore{ttl: progressTTL, store: make(map[string]*JobProgress)}
}

// Update applies a ProgressUpdate. load_progress is monotonic (never regresses).
func (s *JobProgressStore) Update(rid string, u ProgressUpdate) {
	if rid == "" || rid == "-" {
		return
	}
	now := time.Now()
	s.mu.Lock()
	defer s.mu.Unlock()
	s.evictLocked(now)
	jp := s.store[rid]
	if jp == nil {
		jp = &JobProgress{Phase: "init", StartedAt: nowEpoch(), lastTouched: now}
		s.store[rid] = jp
	}
	if u.TotalChunks != nil {
		jp.TotalChunks = *u.TotalChunks
	}
	if u.Phase != nil {
		jp.Phase = *u.Phase
	}
	if u.LoadProgress != nil && *u.LoadProgress > jp.LoadProgress {
		jp.LoadProgress = clamp01(*u.LoadProgress)
	}
	if u.MergeDone != nil {
		jp.MergeDone = clamp01(*u.MergeDone)
	}
	if u.IncrementChunks != 0 {
		jp.ChunksRendered += u.IncrementChunks
	}
	jp.lastTouched = now
}

// Get returns a copy of the progress for rid, or nil if absent.
func (s *JobProgressStore) Get(rid string) *JobProgress {
	if rid == "" {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.evictLocked(time.Now())
	jp := s.store[rid]
	if jp == nil {
		return nil
	}
	cp := *jp
	return &cp
}

// Forget drops the progress for rid.
func (s *JobProgressStore) Forget(rid string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.store, rid)
}

// ActiveEntry pairs a request id with its progress snapshot.
type ActiveEntry struct {
	RequestID string
	Progress  JobProgress
}

// Active returns a snapshot of all non-complete jobs (powers /v1/jobs/active).
func (s *JobProgressStore) Active() []ActiveEntry {
	now := time.Now()
	s.mu.Lock()
	defer s.mu.Unlock()
	s.evictLocked(now)
	var out []ActiveEntry
	for rid, jp := range s.store {
		if jp.Phase != "complete" {
			out = append(out, ActiveEntry{RequestID: rid, Progress: *jp})
		}
	}
	return out
}

func (s *JobProgressStore) evictLocked(now time.Time) {
	for rid, jp := range s.store {
		if now.Sub(jp.lastTouched) > s.ttl {
			delete(s.store, rid)
		}
	}
}

func clamp01(v float64) float64 {
	if v < 0 {
		return 0
	}
	if v > 1 {
		return 1
	}
	return v
}

func max64(a, b float64) float64 {
	if a > b {
		return a
	}
	return b
}

func nowEpoch() float64 {
	return float64(time.Now().UnixNano()) / 1e9
}
