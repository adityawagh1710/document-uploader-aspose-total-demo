package probe

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"pgregory.net/rapid"

	"github.com/opus2/office-convert-orchestrator/internal/types"
)

// --- example-based (BR-1) ---

func TestDetectHTML(t *testing.T) {
	cases := []struct {
		name  string
		bytes []byte
		fname string
		want  types.DispatchFormat
		isErr bool
	}{
		{"doctype", []byte("<!DOCTYPE html>\n<html><body>x</body></html>"), "", types.DispatchHTML, false},
		{"doctype lower", []byte("<!doctype html><html></html>"), "", types.DispatchHTML, false},
		{"html tag", []byte(`<html lang="en"><head></head></html>`), "", types.DispatchHTML, false},
		{"bom + whitespace", []byte("\xef\xbb\xbf  \r\n\t<!DOCTYPE HTML>"), "", types.DispatchHTML, false},
		{"ext fallback", []byte("<!-- comment first -->\n<div>fragment</div>"), "page.html", types.DispatchHTML, false},
		{"htm ext fallback", []byte("plain text body"), "page.HTM", types.DispatchHTML, false},
		{"not html no ext", []byte("<div>fragment</div>"), "notes.txt", "", true},
		// Binary magics must keep winning over the HTML sniff.
		{"pdf still pdf", []byte("%PDF-1.7 <html>"), "x.html", types.DispatchPDF, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got, err := DetectFormat(c.bytes, "", c.fname)
			if c.isErr {
				require.Error(t, err)
				return
			}
			require.NoError(t, err)
			assert.Equal(t, c.want, got)
		})
	}
}

func TestSVGStillWinsOverHTMLSniff(t *testing.T) {
	// An SVG document also starts with '<' — the image check runs first.
	svg := []byte(`<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>`)
	got, err := DetectFormat(svg, "", "")
	require.NoError(t, err)
	assert.Equal(t, types.DispatchSVG, got)
}

// --- property-based (PBT-01/PBT-03: sniffer invariants) ---

// Any (optional BOM) + (ASCII whitespace)* + case-permuted HTML prefix must
// detect as html, regardless of what follows.
func TestProp_HTMLSnifferDetectsPrefixPermutations(t *testing.T) {
	prefixes := []string{"<!doctype html", "<html"}
	rapid.Check(t, func(t *rapid.T) {
		withBOM := rapid.Bool().Draw(t, "bom")
		ws := rapid.SliceOfN(rapid.SampledFrom([]byte(" \t\r\n\v\f")), 0, 32).Draw(t, "ws")
		base := rapid.SampledFrom(prefixes).Draw(t, "prefix")
		// Random per-character case flip.
		var prefix []byte
		for i := 0; i < len(base); i++ {
			c := base[i]
			if rapid.Bool().Draw(t, "case") && c >= 'a' && c <= 'z' {
				c -= 32
			}
			prefix = append(prefix, c)
		}
		tail := rapid.SliceOfN(rapid.Byte(), 0, 256).Draw(t, "tail")

		input := []byte{}
		if withBOM {
			input = append(input, 0xEF, 0xBB, 0xBF)
		}
		input = append(input, ws...)
		input = append(input, prefix...)
		input = append(input, tail...)

		require.True(t, LooksLikeHTML(input), "must sniff as HTML: %q", input[:min(len(input), 64)])
	})
}

// Random byte strings that do not begin (modulo BOM/whitespace) with an HTML
// prefix must never sniff as html (no false positives).
func TestProp_HTMLSnifferNoFalsePositives(t *testing.T) {
	rapid.Check(t, func(t *rapid.T) {
		input := rapid.SliceOfN(rapid.Byte(), 0, 512).Draw(t, "input")
		// Skip inputs that genuinely start with an HTML prefix.
		head := input
		for len(head) >= 3 && head[0] == 0xEF && head[1] == 0xBB && head[2] == 0xBF {
			head = head[3:]
		}
		for len(head) > 0 {
			switch head[0] {
			case ' ', '\t', '\r', '\n', '\v', '\f':
				head = head[1:]
				continue
			}
			break
		}
		lower := toLowerASCII(head, 14)
		if len(lower) >= 5 && (string(lower[:5]) == "<html" ||
			(len(lower) >= 14 && string(lower[:14]) == "<!doctype html")) {
			t.Skip("input is genuinely HTML-prefixed")
		}
		require.False(t, LooksLikeHTML(input), "false positive on %q", input[:min(len(input), 64)])
	})
}

func toLowerASCII(b []byte, n int) []byte {
	if len(b) > n {
		b = b[:n]
	}
	out := make([]byte, len(b))
	for i, c := range b {
		if c >= 'A' && c <= 'Z' {
			c += 32
		}
		out[i] = c
	}
	return out
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
