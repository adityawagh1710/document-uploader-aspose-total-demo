package probe

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

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
				require.Errorf(t, err, "expected error, got %q", got)
				return
			}
			require.NoError(t, err)
			assert.Equal(t, c.want, got)
		})
	}
}

func TestDetectOLE2ByFilename(t *testing.T) {
	// OLE2 magic with no recognizable stream signatures falls back to the
	// uploaded filename's extension.
	ole2 := append([]byte("\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"), make([]byte, 64)...)
	got, err := DetectFormat(ole2, "", "legacy.xls")
	require.NoError(t, err)
	assert.Equal(t, types.DispatchXLSX, got, "should detect xlsx from .xls")
}

func TestDetectGeneratedXLSXFromDisk(t *testing.T) {
	// Cross-package: csvinput produces a real OOXML zip; probe must classify
	// it as xlsx via [Content_Types].xml — the on-disk EOCD path.
	xlsx, err := csvinput.CSVBytesToXLSXBytes([]byte("a,b,c\n1,2,3\n"))
	require.NoError(t, err)
	path := filepath.Join(t.TempDir(), "gen.xlsx")
	require.NoError(t, os.WriteFile(path, xlsx, 0o644))
	got, err := DetectFormat(xlsx[:4], path, "gen.xlsx")
	require.NoError(t, err)
	assert.Equal(t, types.DispatchXLSX, got, "generated XLSX should detect as xlsx")
}

func TestParseProbeJSON(t *testing.T) {
	ok := []byte(`{"page_count": 42, "format": "docx", "size_bytes": 12345, "natural_seams": [[1,10],[11,20]]}`)
	pr, err := ParseProbeJSON(ok)
	require.NoError(t, err)
	assert.EqualValues(t, 42, pr.PageCount)
	assert.Equal(t, types.FormatDOCX, pr.Format)
	assert.EqualValues(t, 12345, pr.SizeBytes)
	require.Len(t, pr.NaturalSeams, 2)
	assert.Equal(t, [2]int{11, 20}, pr.NaturalSeams[1])

	_, err = ParseProbeJSON([]byte(`{"format":"docx"}`))
	assert.Error(t, err, "expected error on missing page_count/size_bytes")
	_, err = ParseProbeJSON([]byte(`not json`))
	assert.Error(t, err, "expected error on invalid JSON")
}
