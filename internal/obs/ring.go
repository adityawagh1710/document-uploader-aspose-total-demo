// Package obs holds the four in-memory observability stores: heartbeats,
// timings, job progress, and recent conversions.
//
// Ported from heartbeats.py, timings.py, job_progress.py, recent.py.
//
// CRITICAL: the Python originals are lock-free, safe only because of the GIL +
// the single-uvicorn-worker assumption. Go has true goroutine parallelism, so
// EVERY store here takes an explicit mutex. The multi-replica tripwire is
// unchanged: >1 replica still needs external persistence regardless of language.
package obs

import (
	"sync"
	"time"
)

// Event is one heartbeat or timing record — a heterogeneous JSON object, like
// the Python dict[str, Any].
type Event = map[string]any

// RingStore is a per-request bounded ring buffer with lazy TTL eviction.
// Backs both the heartbeat store (cap 5000) and the timing store (cap 1000).
type RingStore struct {
	max int
	ttl time.Duration

	mu          sync.Mutex
	store       map[string][]Event
	lastTouched map[string]time.Time
}

// NewRingStore constructs a ring store with the given per-request cap and TTL.
func NewRingStore(maxPerRequest int, ttl time.Duration) *RingStore {
	return &RingStore{
		max:         maxPerRequest,
		ttl:         ttl,
		store:       make(map[string][]Event),
		lastTouched: make(map[string]time.Time),
	}
}

// HeartbeatStore matches heartbeats.py defaults (5000 entries, 30 min TTL).
func HeartbeatStore() *RingStore { return NewRingStore(5000, 30*time.Minute) }

// TimingStore matches timings.py defaults (1000 entries, 30 min TTL).
func TimingStore() *RingStore { return NewRingStore(1000, 30*time.Minute) }

// Record appends an event for requestID, copying it and adding received_at.
func (s *RingStore) Record(requestID string, ev Event) {
	if requestID == "" || requestID == "-" {
		return
	}
	now := time.Now()
	rec := make(Event, len(ev)+1)
	for k, v := range ev {
		rec[k] = v
	}
	rec["received_at"] = now.UnixNano()

	s.mu.Lock()
	defer s.mu.Unlock()
	buf := append(s.store[requestID], rec)
	if len(buf) > s.max {
		buf = buf[len(buf)-s.max:] // keep newest, like deque(maxlen=max)
	}
	s.store[requestID] = buf
	s.lastTouched[requestID] = now
	s.evictLocked(now)
}

// Get returns a snapshot copy of the events for requestID.
func (s *RingStore) Get(requestID string) []Event {
	if requestID == "" {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.evictLocked(time.Now())
	buf := s.store[requestID]
	out := make([]Event, len(buf))
	copy(out, buf)
	return out
}

// Forget drops all events for requestID.
func (s *RingStore) Forget(requestID string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.store, requestID)
	delete(s.lastTouched, requestID)
}

func (s *RingStore) evictLocked(now time.Time) {
	for rid, touched := range s.lastTouched {
		if now.Sub(touched) > s.ttl {
			delete(s.store, rid)
			delete(s.lastTouched, rid)
		}
	}
}
