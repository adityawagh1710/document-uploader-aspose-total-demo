package netpolicy

import (
	"fmt"
	"regexp"
	"testing"

	"github.com/stretchr/testify/require"
	"pgregory.net/rapid"
)

// PBT-03 invariants for the BR-4 deny policy, plus the consistency oracle that
// keeps the two enforcement points (Denied() and the Chromium deny-list regex)
// from drifting apart.

var denyRe = regexp.MustCompile(ChromiumDenyListRegex)

// Every http(s) URL whose host sits in a private/loopback/link-local IPv4
// range or is a single-label hostname must be denied by BOTH the Go matcher
// and the Chromium regex.
func TestProp_PrivateTargetsAlwaysDenied(t *testing.T) {
	rapid.Check(t, func(t *rapid.T) {
		scheme := rapid.SampledFrom([]string{"http", "https"}).Draw(t, "scheme")
		kind := rapid.SampledFrom([]string{"loopback", "ten", "one72", "one92", "linklocal", "single"}).Draw(t, "kind")
		var host string
		switch kind {
		case "loopback":
			host = fmt.Sprintf("127.%d.%d.%d",
				rapid.IntRange(0, 255).Draw(t, "a"),
				rapid.IntRange(0, 255).Draw(t, "b"),
				rapid.IntRange(0, 255).Draw(t, "c"))
		case "ten":
			host = fmt.Sprintf("10.%d.%d.%d",
				rapid.IntRange(0, 255).Draw(t, "a"),
				rapid.IntRange(0, 255).Draw(t, "b"),
				rapid.IntRange(0, 255).Draw(t, "c"))
		case "one72":
			host = fmt.Sprintf("172.%d.%d.%d",
				rapid.IntRange(16, 31).Draw(t, "a"),
				rapid.IntRange(0, 255).Draw(t, "b"),
				rapid.IntRange(0, 255).Draw(t, "c"))
		case "one92":
			host = fmt.Sprintf("192.168.%d.%d",
				rapid.IntRange(0, 255).Draw(t, "a"),
				rapid.IntRange(0, 255).Draw(t, "b"))
		case "linklocal":
			host = fmt.Sprintf("169.254.%d.%d",
				rapid.IntRange(0, 255).Draw(t, "a"),
				rapid.IntRange(0, 255).Draw(t, "b"))
		case "single":
			host = rapid.StringMatching(`[a-z][a-z0-9-]{0,20}[a-z0-9]`).Draw(t, "name")
		}
		path := rapid.StringMatching(`(/[a-z0-9._-]{0,12}){0,3}`).Draw(t, "path")
		u := fmt.Sprintf("%s://%s%s", scheme, host, path)

		denied, reason := Denied(u)
		require.Truef(t, denied, "Denied() must deny %s", u)
		require.NotEmpty(t, reason)
		require.Truef(t, denyRe.MatchString(u), "ChromiumDenyListRegex must deny %s", u)
	})
}

// Public dotted hostnames and public IPv4 literals must be allowed by BOTH
// enforcement points (no over-blocking).
func TestProp_PublicTargetsAllowed(t *testing.T) {
	rapid.Check(t, func(t *rapid.T) {
		scheme := rapid.SampledFrom([]string{"http", "https"}).Draw(t, "scheme")
		var host string
		if rapid.Bool().Draw(t, "ipHost") {
			// Public IPv4: first octet outside every denied range.
			a := rapid.SampledFrom([]int{1, 8, 23, 52, 93, 104, 151, 203}).Draw(t, "a")
			host = fmt.Sprintf("%d.%d.%d.%d", a,
				rapid.IntRange(0, 255).Draw(t, "b"),
				rapid.IntRange(0, 255).Draw(t, "c"),
				rapid.IntRange(1, 254).Draw(t, "d"))
		} else {
			host = rapid.StringMatching(`[a-z][a-z0-9-]{0,12}\.(com|net|org|dev)`).Draw(t, "name")
		}
		u := fmt.Sprintf("%s://%s/asset.css", scheme, host)

		denied, _ := Denied(u)
		require.Falsef(t, denied, "Denied() must allow %s", u)
		require.Falsef(t, denyRe.MatchString(u), "ChromiumDenyListRegex must allow %s", u)
	})
}

// Idempotence/determinism: same URL, same verdict (the matcher is pure).
func TestProp_DeniedDeterministic(t *testing.T) {
	rapid.Check(t, func(t *rapid.T) {
		u := rapid.StringMatching(`https?://[a-z0-9.:-]{1,30}(/[a-z0-9]{0,8}){0,2}`).Draw(t, "url")
		d1, r1 := Denied(u)
		d2, r2 := Denied(u)
		require.Equal(t, d1, d2)
		require.Equal(t, r1, r2)
	})
}
