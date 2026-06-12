// Package license parses Aspose license XML for expiry checking.
//
// Ported from office_convert/license.py. Implements FR-8 (Go side only — XML
// parsing for expiry checking). The C++ worker is the only place that actually
// calls Aspose's SetLicense().
//
// Aspose license files are XML envelopes signed by Aspose. Two expiry-ish dates
// can appear in the License element:
//
//   - <LicenseExpiry>YYYYMMDD</LicenseExpiry>       — the hard stop for a
//     temporary license; Aspose refuses to render past this date.
//   - <SubscriptionExpiry>YYYYMMDD</SubscriptionExpiry> — how long you may use
//     product versions released before this date.
//
// The BINDING date (when rendering actually stops working) is the EARLIER of the
// two present. We surface that one, so /health and the dashboard reflect what the
// C++ Aspose engine will actually do — not a rosy SubscriptionExpiry while a past
// LicenseExpiry silently fails every conversion. If the file is well-formed but
// has neither field, the license is treated as permanent (DaysRemaining ok=false).
package license

import (
	"encoding/xml"
	"fmt"
	"io"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/opus2/office-convert-orchestrator/internal/types"
)

// License-expiry day thresholds per business-rules.md §4.
const (
	healthyMinDays = 7 // strictly more than this -> HEALTHY
	warnMinDays    = 4 // at or above this (but <= HEALTHY_MIN) -> WARN
	// Aspose expiry numeric format is YYYYMMDD (8 digits).
	asposeNumericDateLen = 8
)

// Manager parses a .lic XML file for expiry. It caches the parsed expiry but
// AUTO-REFRESHES when the file changes on disk (mtime), so an operator can renew
// the license in place with no container restart (matches the documented
// behavior + the Python original). Concurrency-safe via mu.
type Manager struct {
	mu      sync.Mutex
	path    string
	read    bool
	modTime time.Time // mtime of the file when last parsed
	expiry  time.Time // zero == no expiry field (permanent)
	hasExp  bool
}

// NewManager constructs a Manager for the given license path.
func NewManager(path string) *Manager { return &Manager{path: path} }

// Refresh forces a re-read of the license file on the next query.
func (m *Manager) Refresh() {
	m.mu.Lock()
	m.read = false
	m.mu.Unlock()
}

// ExpiryDate returns the binding (earliest) expiry date and whether one was
// present. It re-parses the file when it hasn't been read yet OR the file's
// mtime changed since the last parse (license renewed in place).
func (m *Manager) ExpiryDate() (time.Time, bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	info, statErr := os.Stat(m.path)
	changed := statErr == nil && !info.ModTime().Equal(m.modTime)
	if !m.read || changed {
		exp, has, err := parseExpiry(m.path)
		if err != nil {
			return time.Time{}, false, err
		}
		m.expiry, m.hasExp, m.read = exp, has, true
		if statErr == nil {
			m.modTime = info.ModTime()
		}
	}
	return m.expiry, m.hasExp, nil
}

// DaysRemaining returns days until expiry and whether an expiry exists.
// ok=false means a permanent license.
func (m *Manager) DaysRemaining() (int, bool, error) {
	exp, has, err := m.ExpiryDate()
	if err != nil || !has {
		return 0, false, err
	}
	today := time.Now().UTC()
	// Match Python's (expiry_date - today_date).days: whole-day difference
	// between calendar dates, ignoring intra-day time.
	expDay := time.Date(exp.Year(), exp.Month(), exp.Day(), 0, 0, 0, 0, time.UTC)
	todayDay := time.Date(today.Year(), today.Month(), today.Day(), 0, 0, 0, 0, time.UTC)
	days := int(expDay.Sub(todayDay).Hours() / 24)
	return days, true, nil
}

// IsExpired reports whether the license has a past expiry.
func (m *Manager) IsExpired() (bool, error) {
	days, has, err := m.DaysRemaining()
	if err != nil {
		return false, err
	}
	return has && days < 0, nil
}

// State classifies the current license per business-rules.md §4.
func (m *Manager) State() (types.LicenseState, error) {
	days, has, err := m.DaysRemaining()
	if err != nil {
		return types.LicenseStateExpired, err
	}
	if !has {
		return types.LicenseStatePermanent, nil
	}
	return Classify(days, true), nil
}

// Classify maps days_remaining to a LicenseState per business-rules.md §4.
// hasExpiry=false yields PERMANENT regardless of days.
func Classify(daysRemaining int, hasExpiry bool) types.LicenseState {
	if !hasExpiry {
		return types.LicenseStatePermanent
	}
	switch {
	case daysRemaining > healthyMinDays:
		return types.LicenseStateHealthy
	case daysRemaining >= warnMinDays:
		return types.LicenseStateWarn
	case daysRemaining >= 1:
		return types.LicenseStateCritical
	case daysRemaining == 0:
		return types.LicenseStateExpiringToday
	default:
		return types.LicenseStateExpired
	}
}

// parseExpiry parses the XML and returns the EARLIEST of any <LicenseExpiry> /
// <SubscriptionExpiry> dates present (the binding "rendering stops" date).
// has=false if the license is well-formed but has no expiry field (permanent).
func parseExpiry(path string) (time.Time, bool, error) {
	f, err := os.Open(path)
	if err != nil {
		return time.Time{}, false, err
	}
	defer f.Close()

	dec := xml.NewDecoder(f)
	var earliest time.Time
	found := false
	for {
		tok, err := dec.Token()
		if err == io.EOF {
			break
		}
		if err != nil {
			return time.Time{}, false, fmt.Errorf("malformed license XML: %w", err)
		}
		start, ok := tok.(xml.StartElement)
		if !ok || (start.Name.Local != "SubscriptionExpiry" && start.Name.Local != "LicenseExpiry") {
			continue
		}
		// Read the character data inside this element.
		var text string
		if err := dec.DecodeElement(&text, &start); err != nil {
			return time.Time{}, false, fmt.Errorf("malformed license XML: %w", err)
		}
		text = strings.TrimSpace(text)
		if text == "" {
			continue
		}
		d, err := parseAsposeDate(text)
		if err != nil {
			return time.Time{}, false, err
		}
		// Keep the earliest — whichever expiry comes first is what stops rendering.
		if !found || d.Before(earliest) {
			earliest, found = d, true
		}
	}
	return earliest, found, nil
}

// parseAsposeDate parses YYYYMMDD or YYYY-MM-DD (ISO) Aspose date strings.
func parseAsposeDate(raw string) (time.Time, error) {
	raw = strings.TrimSpace(raw)
	if len(raw) == asposeNumericDateLen && isAllDigits(raw) {
		return time.Parse("20060102", raw)
	}
	return time.Parse("2006-01-02", raw)
}

func isAllDigits(s string) bool {
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return len(s) > 0
}
