package obs

import (
	"encoding/base64"
	"encoding/json"
	"sync"
)

// DefaultMaxRecent is the recent-conversions ring capacity.
const DefaultMaxRecent = 200

// ConversionRecord is one terminal-state conversion. Both success and failed
// are captured. Ported from recent.py ConversionRecord. JSON tags match
// asdict() field names exactly (the /v1/conversions wire contract).
type ConversionRecord struct {
	RequestID       string  `json:"request_id"`
	CompletionTS    float64 `json:"completion_ts"`
	Source          string  `json:"source"` // "ui" | "cross"
	InputFilename   *string `json:"input_filename"`
	Format          string  `json:"format"`
	PageCount       *int    `json:"page_count"`
	DurationMS      int     `json:"duration_ms"`
	Status          string  `json:"status"` // "success" | "failed"
	ErrorCode       *string `json:"error_code"`
	OutputS3URI     *string `json:"output_s3_uri"`
	OutputSizeBytes *int64  `json:"output_size_bytes"`
}

// RecentStore is a process-wide bounded, newest-first ring of completed
// conversions. Ported from recent.py RecentStore.
type RecentStore struct {
	mu     sync.Mutex
	buf    []ConversionRecord // index 0 == newest
	maxlen int
}

// NewRecentStore constructs a recent store with the given capacity.
func NewRecentStore(maxlen int) *RecentStore {
	return &RecentStore{maxlen: maxlen}
}

// Record prepends a record (newest-first), evicting the oldest past capacity.
func (s *RecentStore) Record(rec ConversionRecord) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.buf = append([]ConversionRecord{rec}, s.buf...)
	if len(s.buf) > s.maxlen {
		s.buf = s.buf[:s.maxlen]
	}
}

// Snapshot returns an atomic copy for safe iteration during pagination.
func (s *RecentStore) Snapshot() []ConversionRecord {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]ConversionRecord, len(s.buf))
	copy(out, s.buf)
	return out
}

// Size returns the current entry count.
func (s *RecentStore) Size() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.buf)
}

// Maxlen returns the configured capacity.
func (s *RecentStore) Maxlen() int { return s.maxlen }

// Clear empties the buffer (test helper).
func (s *RecentStore) Clear() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.buf = nil
}

// Matches mirrors recent.matches for the filter values all|ui|cross|failed.
func Matches(rec ConversionRecord, flt string) bool {
	switch flt {
	case "all":
		return true
	case "ui":
		return rec.Source == "ui"
	case "cross":
		return rec.Source == "cross"
	case "failed":
		return rec.Status == "failed"
	default:
		return false
	}
}

// Cursor is an opaque pagination anchor encoding (completion_ts, request_id) of
// the last entry on the previous page. Ported from recent.Cursor.
type Cursor struct {
	TS float64 `json:"ts"`
	ID string  `json:"id"`
}

// Encode produces the base64-urlsafe compact-JSON cursor token.
func (c Cursor) Encode() string {
	payload, _ := json.Marshal(c) // {"ts":...,"id":...} — field order fixed
	return base64.URLEncoding.EncodeToString(payload)
}

// DecodeCursor returns nil on any malformed input (caller treats as no cursor).
func DecodeCursor(raw string) *Cursor {
	data, err := base64.URLEncoding.DecodeString(raw)
	if err != nil {
		return nil
	}
	var c Cursor
	if err := json.Unmarshal(data, &c); err != nil {
		return nil
	}
	return &c
}

// Page is one paginated slice of conversion records.
type Page struct {
	Entries     []ConversionRecord
	NextCursor  *string
	HasMore     bool
	StaleCursor bool
	BufferSize  int
}

// Paginate applies cursor + limit. Mirrors recent.paginate, including
// stale-cursor detection and the strict (ts, id) tuple ordering.
func Paginate(items []ConversionRecord, cursor *Cursor, limit, bufferSize int) Page {
	if cursor == nil {
		return makePage(items, limit, bufferSize, false)
	}
	anchorPresent := false
	for _, r := range items {
		if r.CompletionTS == cursor.TS && r.RequestID == cursor.ID {
			anchorPresent = true
			break
		}
	}
	if !anchorPresent {
		return makePage(items, limit, bufferSize, true)
	}
	var filtered []ConversionRecord
	for _, r := range items {
		if lessTuple(r.CompletionTS, r.RequestID, cursor.TS, cursor.ID) {
			filtered = append(filtered, r)
		}
	}
	return makePage(filtered, limit, bufferSize, false)
}

func makePage(items []ConversionRecord, limit, bufferSize int, stale bool) Page {
	entries := items
	if len(entries) > limit {
		entries = entries[:limit]
	}
	hasMore := len(items) > limit
	var next *string
	if hasMore && len(entries) > 0 {
		last := entries[len(entries)-1]
		c := Cursor{TS: last.CompletionTS, ID: last.RequestID}.Encode()
		next = &c
	}
	return Page{
		Entries:     entries,
		NextCursor:  next,
		HasMore:     hasMore,
		StaleCursor: stale,
		BufferSize:  bufferSize,
	}
}

// lessTuple reports whether (aTS, aID) < (bTS, bID) under Python tuple ordering.
func lessTuple(aTS float64, aID string, bTS float64, bID string) bool {
	if aTS != bTS {
		return aTS < bTS
	}
	return aID < bID
}
