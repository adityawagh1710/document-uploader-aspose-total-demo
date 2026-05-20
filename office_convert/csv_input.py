"""CSV upload normalization.

The Aspose workers handle XLSX, not CSV. To let users submit CSV through the
public /convert endpoint, the server wraps a CSV body as a minimal XLSX before
format detection runs — from then on it flows through the standard xlsx pipeline.

Stdlib only on purpose: the orchestrator's image already has heavy deps
(FastAPI, qpdf, Aspose); pulling in openpyxl/xlsxwriter just to emit a flat
table isn't worth the extra surface area.
"""

from __future__ import annotations

import csv
import io
import zipfile
from xml.sax.saxutils import escape

# Excel "character units" → inches conversion (1 char ≈ 7 px @ 96 dpi).
# Default column width 8.43 chars ≈ 64 px ≈ 0.67".
_CHAR_TO_IN = 7.0 / 96.0
_MIN_COL_W = 8.0
_MAX_COL_W = 50.0
# A4 portrait page is 8.27" wide; default Excel margins eat ~0.7" each side,
# leaving ~6.87" of printable area. Past that, portrait truncates columns
# across page breaks, so we flip the sheet's page setup to landscape.
_A4_PORTRAIT_PRINTABLE_IN = 6.87


def is_csv_filename(filename: str | None) -> bool:
    return bool(filename) and filename.lower().endswith(".csv")  # type: ignore[union-attr]


def csv_bytes_to_xlsx_bytes(csv_bytes: bytes) -> bytes:
    """Wrap CSV content as a minimal XLSX (one inline-string sheet)."""
    rows = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8-sig", errors="replace"))))
    col_widths = _estimate_col_widths(rows)
    orientation = (
        "landscape"
        if sum(w * _CHAR_TO_IN for w in col_widths) > _A4_PORTRAIT_PRINTABLE_IN
        else "portrait"
    )

    sheet_xml = _sheet_xml(rows, col_widths, orientation)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        z.writestr("_rels/.rels", _ROOT_RELS_XML)
        z.writestr("xl/workbook.xml", _WORKBOOK_XML)
        z.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS_XML)
        z.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buf.getvalue()


def _estimate_col_widths(rows: list[list[str]]) -> list[float]:
    n_cols = max((len(r) for r in rows), default=0)
    widths = []
    for ci in range(n_cols):
        max_len = max((len(r[ci]) for r in rows if ci < len(r)), default=0)
        widths.append(min(_MAX_COL_W, max(_MIN_COL_W, float(max_len + 2))))
    return widths


def _sheet_xml(rows: list[list[str]], col_widths: list[float], orientation: str) -> str:
    cols_xml = (
        "<cols>"
        + "".join(
            f'<col min="{i+1}" max="{i+1}" width="{w:.2f}" customWidth="1"/>'
            for i, w in enumerate(col_widths)
        )
        + "</cols>"
        if col_widths
        else ""
    )

    rows_xml = []
    for r_idx, row in enumerate(rows, start=1):
        cells = []
        for c_idx, val in enumerate(row):
            ref = f"{_col_letter(c_idx)}{r_idx}"
            if r_idx > 1 and _is_number(val):
                cells.append(f'<c r="{ref}"><v>{val}</v></c>')
            else:
                cells.append(
                    f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">'
                    f"{escape(val)}</t></is></c>"
                )
        rows_xml.append(f'<row r="{r_idx}">{"".join(cells)}</row>')

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"{cols_xml}"
        f'<sheetData>{"".join(rows_xml)}</sheetData>'
        f'<pageSetup paperSize="9" orientation="{orientation}"/>'
        "</worksheet>"
    )


def _col_letter(idx: int) -> str:
    s, n = "", idx
    while True:
        s = chr(ord("A") + n % 26) + s
        n = n // 26 - 1
        if n < 0:
            return s


def _is_number(v: str) -> bool:
    if not v:
        return False
    try:
        float(v)
        return True
    except ValueError:
        return False


_CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    "</Types>"
)
_ROOT_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    "</Relationships>"
)
_WORKBOOK_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
)
_WORKBOOK_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
    "</Relationships>"
)
