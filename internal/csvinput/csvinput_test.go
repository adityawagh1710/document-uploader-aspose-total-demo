package csvinput

import (
	"archive/zip"
	"bytes"
	"io"
	"strings"
	"testing"
)

func TestColLetter(t *testing.T) {
	cases := map[int]string{0: "A", 25: "Z", 26: "AA", 27: "AB", 51: "AZ", 52: "BA", 701: "ZZ", 702: "AAA"}
	for idx, want := range cases {
		if got := colLetter(idx); got != want {
			t.Errorf("colLetter(%d) = %q, want %q", idx, got, want)
		}
	}
}

func TestIsNumber(t *testing.T) {
	for _, v := range []string{"1", "1.5", "-3", "1e5", "0"} {
		if !isNumber(v) {
			t.Errorf("isNumber(%q) should be true", v)
		}
	}
	for _, v := range []string{"", "abc", "1,000", "$5"} {
		if isNumber(v) {
			t.Errorf("isNumber(%q) should be false", v)
		}
	}
}

func TestXMLEscape(t *testing.T) {
	if got := xmlEscape(`a & b < c > d`); got != `a &amp; b &lt; c &gt; d` {
		t.Fatalf("escape = %q", got)
	}
}

func TestCSVToXLSXStructure(t *testing.T) {
	out, err := CSVBytesToXLSXBytes([]byte("name,score\nAda,99\nBob,fifty\n"))
	if err != nil {
		t.Fatal(err)
	}
	zr, err := zip.NewReader(bytes.NewReader(out), int64(len(out)))
	if err != nil {
		t.Fatalf("output is not a valid zip: %v", err)
	}
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
	if len(want) != 0 {
		t.Fatalf("missing entries: %v", want)
	}
	// Header row stays string; data row "99" becomes a numeric cell; "fifty" stays string.
	if !strings.Contains(sheet, `<c r="B2"><v>99</v></c>`) {
		t.Errorf("expected numeric cell for 99, sheet=%s", sheet)
	}
	if !strings.Contains(sheet, `t="inlineStr"`) || !strings.Contains(sheet, "fifty") {
		t.Errorf("expected inline string cells, sheet=%s", sheet)
	}
}
