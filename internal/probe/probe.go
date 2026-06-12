// Package probe does format detection via magic bytes and parses the C++
// worker's probe JSON.
//
// Ported from the pure parts of office_convert/probe.py: magic-byte format
// detection per business-rules.md §7.1 (FR-1 + NFR-3 fast-fail), the OOXML/ODF
// and OLE2 disambiguation, and parse_probe_json. The async probe() that spawns
// the worker lives with the orchestrator (Phase 3), since it depends on the
// worker layer.
package probe

import (
	"archive/zip"
	"bytes"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/opus2/office-convert-orchestrator/internal/oerrors"
	"github.com/opus2/office-convert-orchestrator/internal/types"
)

// AcceptedFormats is the closed set of planner-eligible formats.
var AcceptedFormats = []types.FormatName{
	types.FormatDOCX, types.FormatPPTX, types.FormatXLSX, types.FormatPDF,
}

// AcceptedUploadFormats is what a user may upload. Legacy + image + email
// formats are remapped internally. Exposed only via the UnsupportedFormat
// error detail.
// NOTE: html/htm are deliberately NOT listed here even though DetectFormat
// recognizes them — this list feeds the unsupported_format error body of the
// LEGACY /v1/convert route, which must stay wire-identical to the Python
// backend (golden parity gate). HTML callers are directed to the dedicated
// /v1/convert/html/{engine} endpoints by the D1 rejection reason instead.
var AcceptedUploadFormats = []string{
	"docx", "pptx", "xlsx", "pdf", "doc", "xls", "ppt", "csv", "rtf",
	"odt", "ods", "odp", "odg", "png", "jpg", "jpeg", "tiff", "tif",
	"gif", "bmp", "webp", "svg", "eml",
}

var (
	pdfMagic   = []byte("%PDF-")
	zipMagic   = []byte("PK\x03\x04")
	ole2Magic  = []byte("\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
	rtfMagic   = []byte("{\\rtf")
	pngMagic   = []byte("\x89PNG\r\n\x1a\n")
	jpegMagic  = []byte("\xff\xd8\xff")
	gif87Magic = []byte("GIF87a")
	gif89Magic = []byte("GIF89a")
	bmpMagic   = []byte("BM")
	tiffLE     = []byte("II*\x00")
	tiffBE     = []byte("MM\x00*")
	riffMagic  = []byte("RIFF")
	webpTag    = []byte("WEBP")
)

var emlHeaderPrefixes = []string{
	"received:", "return-path:", "delivered-to:", "message-id:",
	"date:", "from:", "to:", "subject:", "mime-version:", "x-",
}

var ooxmlContentTypeToFormat = []struct {
	ctype string
	fmt   types.FormatName
}{
	{"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml", types.FormatDOCX},
	{"application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml", types.FormatPPTX},
	{"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml", types.FormatXLSX},
}

var odfMimetypeToFormat = map[string]types.DispatchFormat{
	"application/vnd.oasis.opendocument.text":         types.DispatchDOCX,
	"application/vnd.oasis.opendocument.spreadsheet":  types.DispatchXLSX,
	"application/vnd.oasis.opendocument.presentation": types.DispatchPPTX,
	"application/vnd.oasis.opendocument.graphics":     types.DispatchODG,
}

var unrenderableODFSubtypes = map[string]string{
	"application/vnd.oasis.opendocument.formula": "OpenDocument Formula (.odf)",
	"application/vnd.oasis.opendocument.base":    "OpenDocument Base (.odb)",
}

// ole2StreamSignatures — order matters: longer/more-specific first.
var ole2StreamSignatures = []struct {
	sig []byte
	fmt types.FormatName
}{
	{[]byte("P\x00o\x00w\x00e\x00r\x00P\x00o\x00i\x00n\x00t\x00 \x00D\x00o\x00c\x00u\x00m\x00e\x00n\x00t"), types.FormatPPTX},
	{[]byte("W\x00o\x00r\x00d\x00D\x00o\x00c\x00u\x00m\x00e\x00n\x00t"), types.FormatDOCX},
	{[]byte("W\x00o\x00r\x00k\x00b\x00o\x00o\x00k"), types.FormatXLSX},
	{[]byte("B\x00o\x00o\x00k"), types.FormatXLSX},
}

var ole2ExtToFormat = map[string]types.FormatName{
	"doc": types.FormatDOCX, "dot": types.FormatDOCX,
	"xls": types.FormatXLSX, "xlt": types.FormatXLSX, "xlm": types.FormatXLSX,
	"ppt": types.FormatPPTX, "pot": types.FormatPPTX, "pps": types.FormatPPTX,
}

// DetectFormat detects format by magic bytes plus OOXML/OLE2 disambiguation.
// sourcePath may be "" (then OOXML uses the in-memory prefix path, kept for
// unit tests); filename may be "" (OLE2 extension fallback).
func DetectFormat(magicBytes []byte, sourcePath, filename string) (types.DispatchFormat, error) {
	if len(magicBytes) == 0 {
		return "", oerrors.NewUnsupportedFormat("(empty)", AcceptedUploadFormats, "")
	}

	switch {
	case bytes.HasPrefix(magicBytes, pdfMagic):
		return types.DispatchPDF, nil
	case bytes.HasPrefix(magicBytes, zipMagic):
		if sourcePath != "" {
			return inspectOOXMLPath(sourcePath)
		}
		f, err := inspectOOXMLPrefix(magicBytes)
		return types.DispatchFormat(f), err
	case bytes.HasPrefix(magicBytes, ole2Magic):
		f, err := classifyOLE2(sourcePath, filename)
		return types.DispatchFormat(f), err
	case bytes.HasPrefix(magicBytes, rtfMagic):
		return types.DispatchDOCX, nil
	}

	if img := detectImageFormat(magicBytes); img != "" {
		return img, nil
	}

	// HTML sniff (BR-1) sits after the image check (the SVG text sniff must win
	// for <svg> documents) and before EML.
	if LooksLikeHTML(magicBytes) {
		return types.DispatchHTML, nil
	}

	if looksLikeEML(magicBytes) {
		return types.DispatchEML, nil
	}

	// Extension fallback for HTML files that start with neither doctype nor
	// <html (e.g. leading comments or partial fragments saved as .html).
	if hasHTMLExtension(filename) {
		return types.DispatchHTML, nil
	}

	n := 8
	if len(magicBytes) < n {
		n = len(magicBytes)
	}
	return "", oerrors.NewUnsupportedFormat(hex.EncodeToString(magicBytes[:n]), AcceptedUploadFormats, "")
}

// LooksLikeHTML implements the BR-1 content sniff: within the first 1024
// bytes, after stripping a UTF-8 BOM and ASCII whitespace, the input starts
// with "<!doctype html" or "<html" (case-insensitive).
func LooksLikeHTML(magicBytes []byte) bool {
	head := first(magicBytes, 1024)
	head = bytes.TrimPrefix(head, []byte("\xef\xbb\xbf"))
	head = bytes.TrimLeft(head, " \t\r\n\v\f")
	lower := bytes.ToLower(first(head, 64))
	return bytes.HasPrefix(lower, []byte("<!doctype html")) ||
		bytes.HasPrefix(lower, []byte("<html"))
}

func hasHTMLExtension(filename string) bool {
	i := strings.LastIndex(filename, ".")
	if i < 0 {
		return false
	}
	ext := strings.ToLower(filename[i+1:])
	return ext == "html" || ext == "htm"
}

// IsHTMLUpload is the BR-2 endpoint-level validation used by the
// /v1/convert/html/{engine} handlers: content sniff with extension fallback.
func IsHTMLUpload(head []byte, filename string) bool {
	return LooksLikeHTML(head) || hasHTMLExtension(filename)
}

func looksLikeEML(magicBytes []byte) bool {
	head := magicBytes
	if len(head) > 1024 {
		head = head[:1024]
	}
	head = bytes.TrimLeft(head, "\xef\xbb\xbf")
	head = bytes.TrimLeft(head, " \t\r\n\v\f")
	scan := head
	if len(scan) > 200 {
		scan = scan[:200]
	}
	if !bytes.Contains(scan, []byte(": ")) {
		return false
	}
	firstLine := head
	if i := bytes.IndexByte(head, '\n'); i >= 0 {
		firstLine = head[:i]
	}
	lower := strings.ToLower(string(firstLine))
	for _, p := range emlHeaderPrefixes {
		if strings.HasPrefix(lower, p) {
			return true
		}
	}
	return false
}

func detectImageFormat(magicBytes []byte) types.DispatchFormat {
	switch {
	case bytes.HasPrefix(magicBytes, pngMagic):
		return types.DispatchPNG
	case bytes.HasPrefix(magicBytes, jpegMagic):
		return types.DispatchJPG
	case bytes.HasPrefix(magicBytes, gif87Magic), bytes.HasPrefix(magicBytes, gif89Magic):
		return types.DispatchGIF
	case bytes.HasPrefix(magicBytes, bmpMagic):
		return types.DispatchBMP
	case bytes.HasPrefix(magicBytes, tiffLE), bytes.HasPrefix(magicBytes, tiffBE):
		return types.DispatchTIFF
	case bytes.HasPrefix(magicBytes, riffMagic) && bytes.Contains(first(magicBytes, 16), webpTag):
		return types.DispatchWEBP
	}
	// SVG: tolerant text sniff in the first ~512 bytes.
	head := first(magicBytes, 512)
	trimmed := bytes.TrimLeft(head, "\xef\xbb\xbf")
	trimmed = bytes.TrimLeft(trimmed, " \t\r\n\v\f")
	if (bytes.HasPrefix(trimmed, []byte("<?xml")) || bytes.HasPrefix(trimmed, []byte("<svg"))) &&
		bytes.Contains(bytes.ToLower(head), []byte("<svg")) {
		return types.DispatchSVG
	}
	return ""
}

func classifyOLE2(sourcePath, filename string) (types.FormatName, error) {
	if sourcePath != "" {
		if head, err := readHead(sourcePath, 524288); err == nil {
			var found []types.FormatName
			for _, s := range ole2StreamSignatures {
				if bytes.Contains(head, s.sig) {
					found = append(found, s.fmt)
				}
			}
			if len(found) > 0 {
				// Priority: docx > pptx > xlsx (Word docs embed Excel objects).
				if containsFmt(found, types.FormatDOCX) {
					return types.FormatDOCX, nil
				}
				if containsFmt(found, types.FormatPPTX) {
					return types.FormatPPTX, nil
				}
				if containsFmt(found, types.FormatXLSX) {
					return types.FormatXLSX, nil
				}
				return found[0], nil
			}
		}
	}
	if filename != "" {
		if i := strings.LastIndex(filename, "."); i >= 0 {
			suffix := strings.ToLower(filename[i+1:])
			if f, ok := ole2ExtToFormat[suffix]; ok {
				return f, nil
			}
		}
	}
	return "", oerrors.NewUnsupportedFormat(hex.EncodeToString(ole2Magic), AcceptedUploadFormats, "")
}

func inspectOOXMLPath(path string) (types.DispatchFormat, error) {
	zr, err := zip.OpenReader(path)
	if err != nil {
		return types.DispatchDOCX, nil // could not classify; permissive default
	}
	defer zr.Close()

	var contentTypes string
	for _, f := range zr.File {
		switch f.Name {
		case "mimetype":
			mimetype := strings.TrimSpace(readZipEntry(f))
			if fm, ok := odfMimetypeToFormat[mimetype]; ok {
				return fm, nil
			}
			if reason, ok := unrenderableODFSubtypes[mimetype]; ok {
				return "", oerrors.NewUnsupportedFormat(
					mimetype, AcceptedUploadFormats,
					reason+" is not supported by Aspose.Total C++")
			}
		case "[Content_Types].xml":
			contentTypes = readZipEntry(f)
		}
	}
	if contentTypes != "" {
		return types.DispatchFormat(classifyByContentTypes(contentTypes)), nil
	}
	return types.DispatchDOCX, nil
}

func inspectOOXMLPrefix(prefix []byte) (types.FormatName, error) {
	zr, err := zip.NewReader(bytes.NewReader(prefix), int64(len(prefix)))
	if err != nil {
		return types.FormatDOCX, nil
	}
	for _, f := range zr.File {
		if f.Name == "[Content_Types].xml" {
			return classifyByContentTypes(readZipEntry(f)), nil
		}
	}
	return types.FormatDOCX, nil
}

func classifyByContentTypes(content string) types.FormatName {
	for _, e := range ooxmlContentTypeToFormat {
		if strings.Contains(content, e.ctype) {
			return e.fmt
		}
	}
	return types.FormatDOCX // permissive default
}

// probeJSON is the wire shape the C++ worker writes in --mode=probe.
type probeJSON struct {
	PageCount    *int    `json:"page_count"`
	Format       string  `json:"format"`
	NaturalSeams [][]int `json:"natural_seams"`
	SizeBytes    *int64  `json:"size_bytes"`
}

// ParseProbeJSON parses the JSON ProbeResult the C++ worker writes to stdout.
func ParseProbeJSON(stdout []byte) (types.ProbeResult, error) {
	var data probeJSON
	if err := json.Unmarshal(stdout, &data); err != nil {
		return types.ProbeResult{}, oerrors.NewInputUnprocessable(
			fmt.Sprintf("worker returned invalid JSON: %v", err))
	}
	if data.PageCount == nil || data.SizeBytes == nil || data.Format == "" {
		return types.ProbeResult{}, oerrors.NewInputUnprocessable("worker probe JSON missing field")
	}
	seams := make([][2]int, 0, len(data.NaturalSeams))
	for _, s := range data.NaturalSeams {
		if len(s) >= 2 {
			seams = append(seams, [2]int{s[0], s[1]})
		}
	}
	return types.ProbeResult{
		PageCount:    *data.PageCount,
		Format:       types.FormatName(data.Format),
		NaturalSeams: seams,
		SizeBytes:    *data.SizeBytes,
	}, nil
}

// --- helpers ---

func first(b []byte, n int) []byte {
	if len(b) < n {
		return b
	}
	return b[:n]
}

func containsFmt(fs []types.FormatName, want types.FormatName) bool {
	for _, f := range fs {
		if f == want {
			return true
		}
	}
	return false
}

func readHead(path string, n int) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	buf := make([]byte, n)
	read, err := io.ReadFull(f, buf)
	if err != nil && err != io.EOF && err != io.ErrUnexpectedEOF {
		return nil, err
	}
	return buf[:read], nil
}

func readZipEntry(f *zip.File) string {
	rc, err := f.Open()
	if err != nil {
		return ""
	}
	defer rc.Close()
	b, err := io.ReadAll(rc)
	if err != nil {
		return ""
	}
	return string(b)
}
