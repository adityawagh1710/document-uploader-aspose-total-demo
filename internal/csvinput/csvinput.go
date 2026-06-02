// Package csvinput normalizes CSV uploads to a minimal XLSX.
//
// Ported from office_convert/csv_input.py. The Aspose workers handle XLSX, not
// CSV. The server wraps a CSV body as a minimal XLSX before format detection
// runs; from then on it flows through the standard xlsx pipeline.
//
// Stdlib only on purpose, matching the Python rationale (no openpyxl/xlsxwriter
// just to emit a flat table).
package csvinput

import (
	"archive/zip"
	"bytes"
	"encoding/csv"
	"fmt"
	"strconv"
	"strings"
)

// Excel "character units" -> inches (1 char ~= 7 px @ 96 dpi).
const (
	charToIn            = 7.0 / 96.0
	minColW             = 8.0
	maxColW             = 50.0
	a4PortraitPrintable = 6.87
)

// IsCSVFilename reports whether filename has a .csv extension (case-insensitive).
func IsCSVFilename(filename string) bool {
	return filename != "" && strings.HasSuffix(strings.ToLower(filename), ".csv")
}

// CSVBytesToXLSXBytes wraps CSV content as a minimal XLSX (one inline-string
// sheet). Mirrors csv_bytes_to_xlsx_bytes.
func CSVBytesToXLSXBytes(csvBytes []byte) ([]byte, error) {
	rows, err := readRows(csvBytes)
	if err != nil {
		return nil, err
	}
	colWidths := estimateColWidths(rows)
	totalIn := 0.0
	for _, w := range colWidths {
		totalIn += w * charToIn
	}
	orientation := "portrait"
	if totalIn > a4PortraitPrintable {
		orientation = "landscape"
	}

	sheetXML := sheetXML(rows, colWidths, orientation)

	var buf bytes.Buffer
	z := zip.NewWriter(&buf)
	entries := []struct{ name, body string }{
		{"[Content_Types].xml", contentTypesXML},
		{"_rels/.rels", rootRelsXML},
		{"xl/workbook.xml", workbookXML},
		{"xl/_rels/workbook.xml.rels", workbookRelsXML},
		{"xl/worksheets/sheet1.xml", sheetXML},
	}
	for _, e := range entries {
		w, err := z.CreateHeader(&zip.FileHeader{Name: e.name, Method: zip.Deflate})
		if err != nil {
			return nil, err
		}
		if _, err := w.Write([]byte(e.body)); err != nil {
			return nil, err
		}
	}
	if err := z.Close(); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func readRows(csvBytes []byte) ([][]string, error) {
	// utf-8-sig: strip a leading BOM if present, matching Python's decode.
	csvBytes = bytes.TrimPrefix(csvBytes, []byte("\xef\xbb\xbf"))
	r := csv.NewReader(bytes.NewReader(csvBytes))
	r.FieldsPerRecord = -1 // ragged rows allowed, like Python's csv.reader
	r.LazyQuotes = true
	rows, err := r.ReadAll()
	if err != nil {
		return nil, fmt.Errorf("csv parse: %w", err)
	}
	return rows, nil
}

func estimateColWidths(rows [][]string) []float64 {
	nCols := 0
	for _, r := range rows {
		if len(r) > nCols {
			nCols = len(r)
		}
	}
	widths := make([]float64, nCols)
	for ci := 0; ci < nCols; ci++ {
		maxLen := 0
		for _, r := range rows {
			if ci < len(r) && len(r[ci]) > maxLen {
				maxLen = len(r[ci])
			}
		}
		w := float64(maxLen + 2)
		if w < minColW {
			w = minColW
		}
		if w > maxColW {
			w = maxColW
		}
		widths[ci] = w
	}
	return widths
}

func sheetXML(rows [][]string, colWidths []float64, orientation string) string {
	var colsXML string
	if len(colWidths) > 0 {
		var b strings.Builder
		b.WriteString("<cols>")
		for i, w := range colWidths {
			fmt.Fprintf(&b, `<col min="%d" max="%d" width="%.2f" customWidth="1"/>`, i+1, i+1, w)
		}
		b.WriteString("</cols>")
		colsXML = b.String()
	}

	var rowsXML strings.Builder
	for ri, row := range rows {
		rIdx := ri + 1
		rowsXML.WriteString(fmt.Sprintf(`<row r="%d">`, rIdx))
		for ci, val := range row {
			ref := fmt.Sprintf("%s%d", colLetter(ci), rIdx)
			if rIdx > 1 && isNumber(val) {
				fmt.Fprintf(&rowsXML, `<c r="%s"><v>%s</v></c>`, ref, val)
			} else {
				fmt.Fprintf(&rowsXML,
					`<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>`,
					ref, xmlEscape(val))
			}
		}
		rowsXML.WriteString("</row>")
	}

	return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
		`<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">` +
		colsXML +
		`<sheetData>` + rowsXML.String() + `</sheetData>` +
		fmt.Sprintf(`<pageSetup paperSize="9" orientation="%s"/>`, orientation) +
		`</worksheet>`
}

// colLetter mirrors the bijective base-26 _col_letter (0 -> A, 25 -> Z, 26 -> AA).
func colLetter(idx int) string {
	var s string
	n := idx
	for {
		s = string(rune('A'+n%26)) + s
		n = n/26 - 1
		if n < 0 {
			return s
		}
	}
}

func isNumber(v string) bool {
	if v == "" {
		return false
	}
	_, err := strconv.ParseFloat(v, 64)
	return err == nil
}

// xmlEscape mirrors xml.sax.saxutils.escape: only &, <, > (order matters).
func xmlEscape(s string) string {
	s = strings.ReplaceAll(s, "&", "&amp;")
	s = strings.ReplaceAll(s, "<", "&lt;")
	s = strings.ReplaceAll(s, ">", "&gt;")
	return s
}

const contentTypesXML = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
	`<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">` +
	`<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>` +
	`<Default Extension="xml" ContentType="application/xml"/>` +
	`<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>` +
	`<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>` +
	`</Types>`

const rootRelsXML = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
	`<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">` +
	`<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>` +
	`</Relationships>`

const workbookXML = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
	`<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" ` +
	`xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">` +
	`<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>`

const workbookRelsXML = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
	`<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">` +
	`<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>` +
	`</Relationships>`
