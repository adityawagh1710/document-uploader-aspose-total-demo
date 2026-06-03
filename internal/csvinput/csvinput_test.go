package csvinput

import (
	"archive/zip"
	"bytes"
	"io"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestColLetter(t *testing.T) {
	cases := map[int]string{0: "A", 25: "Z", 26: "AA", 27: "AB", 51: "AZ", 52: "BA", 701: "ZZ", 702: "AAA"}
	for idx, want := range cases {
		assert.Equalf(t, want, colLetter(idx), "colLetter(%d)", idx)
	}
}

func TestIsNumber(t *testing.T) {
	for _, v := range []string{"1", "1.5", "-3", "1e5", "0"} {
		assert.Truef(t, isNumber(v), "isNumber(%q) should be true", v)
	}
	for _, v := range []string{"", "abc", "1,000", "$5"} {
		assert.Falsef(t, isNumber(v), "isNumber(%q) should be false", v)
	}
}

func TestXMLEscape(t *testing.T) {
	assert.Equal(t, `a &amp; b &lt; c &gt; d`, xmlEscape(`a & b < c > d`))
}

func TestCSVToXLSXStructure(t *testing.T) {
	out, err := CSVBytesToXLSXBytes([]byte("name,score\nAda,99\nBob,fifty\n"))
	require.NoError(t, err)
	zr, err := zip.NewReader(bytes.NewReader(out), int64(len(out)))
	require.NoError(t, err, "output is not a valid zip")
	want := map[string]bool{
		"[Content_Types].xml": true, "_rels/.rels": true, "xl/workbook.xml": true,
		"xl/_rels/workbook.xml.rels": true, "xl/worksheets/sheet1.xml": true,
	}
	var sheet string
	for _, f := range zr.File {
		delete(want, f.Name)
		if f.Name == "xl/worksheets/sheet1.xml" {
			rc, _ := f.Open()
			b, _ := io.ReadAll(rc)
			rc.Close()
			sheet = string(b)
		}
	}
	require.Empty(t, want, "missing entries")
	// Header row stays string; data row "99" becomes a numeric cell; "fifty" stays string.
	assert.Contains(t, sheet, `<c r="B2"><v>99</v></c>`, "expected numeric cell for 99")
	assert.Contains(t, sheet, `t="inlineStr"`, "expected inline string cells")
	assert.Contains(t, sheet, "fifty", "expected inline string content")
}
