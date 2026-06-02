package probe

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/opus2/office-convert-orchestrator/internal/csvinput"
	"github.com/opus2/office-convert-orchestrator/internal/types"
)

func TestDetectFormatMagic(t *testing.T) {
	cases := []struct {
		name  string
		bytes []byte
		want  types.DispatchFormat
		isErr bool
	}{
		{"pdf", []byte("%PDF-1.7\n..."), types.DispatchPDF, false},
		{"png", []byte("\x89PNG\r\n\x1a\nrest"), types.DispatchPNG, false},
		{"jpeg", []byte("\xff\xd8\xff\xe0stuff"), types.DispatchJPG, false},
		{"gif", []byte("GIF89a...."), types.DispatchGIF, false},
		{"rtf", []byte("{\\rtf1\\ansi"), types.DispatchDOCX, false},
		{"eml", []byte("From: a@b.com\r\nTo: c@d.com\r\nSubject: hi\r\n\r\nbody"), types.DispatchEML, false},
		{"empty", []byte{}, "", true},
		{"garbage", []byte("\x00\x01\x02\x03not a doc"), "", true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got, err := DetectFormat(c.bytes, "", "")
			if c.isErr {
				if err == nil {
					t.Fatalf("expected error, got %q", got)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if got != c.want {
				t.Fatalf("DetectFormat = %q, want %q", got, c.want)
			}
		})
	}
}

func TestDetectOLE2ByFilename(t *testing.T) {
	// OLE2 magic with no recognizable stream signatures falls back to the
	// uploaded filename's extension.
	ole2 := append([]byte("\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"), make([]byte, 64)...)
	got, err := DetectFormat(ole2, "", "legacy.xls")
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if got != types.DispatchXLSX {
		t.Fatalf("got %q, want xlsx (from .xls)", got)
	}
}

func TestDetectGeneratedXLSXFromDisk(t *testing.T) {
	// Cross-package: csvinput produces a real OOXML zip; probe must classify
	// it as xlsx via [Content_Types].xml — the on-disk EOCD path.
	xlsx, err := csvinput.CSVBytesToXLSXBytes([]byte("a,b,c\n1,2,3\n"))
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "gen.xlsx")
	if err := os.WriteFile(path, xlsx, 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := DetectFormat(xlsx[:4], path, "gen.xlsx")
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if got != types.DispatchXLSX {
		t.Fatalf("generated XLSX detected as %q, want xlsx", got)
	}
}

func TestParseProbeJSON(t *testing.T) {
	ok := []byte(`{"page_count": 42, "format": "docx", "size_bytes": 12345, "natural_seams": [[1,10],[11,20]]}`)
	pr, err := ParseProbeJSON(ok)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if pr.PageCount != 42 || pr.Format != types.FormatDOCX || pr.SizeBytes != 12345 {
		t.Fatalf("parsed wrong: %+v", pr)
	}
	if len(pr.NaturalSeams) != 2 || pr.NaturalSeams[1] != [2]int{11, 20} {
		t.Fatalf("seams wrong: %+v", pr.NaturalSeams)
	}

	if _, err := ParseProbeJSON([]byte(`{"format":"docx"}`)); err == nil {
		t.Fatal("expected error on missing page_count/size_bytes")
	}
	if _, err := ParseProbeJSON([]byte(`not json`)); err == nil {
		t.Fatal("expected error on invalid JSON")
	}
}
