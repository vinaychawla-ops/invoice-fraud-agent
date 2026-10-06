"""Invoice document ingestion: the second input path for the fraud agent.

Upload a supplier invoice as PDF, Word (.docx), plain text, or XML and it
is parsed into the same case shape as ``fraud_agent/data/invoices.json``,
registered in-memory as ``UPLOAD-####``, and audited with the unchanged
pipeline and fraud checks.

XML documents are parsed directly against the documented schema (no LLM).
PDF / DOCX / TXT go through a deterministic mock text parser by default,
or live Gemini extraction (reusing :func:`tools._live_extract`) when
``mode="live"`` and ``GOOGLE_API_KEY`` is set.

The ADK-facing tool function is :func:`ingest_invoice_document` (defined
here rather than in ``tools.py`` so the import only flows one way:
``ingest`` -> ``tools``). Every failure returns ``{"error": ...}`` --
never a traceback.
"""
from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from . import tools

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".xml"}

UPLOADS_ENV_VAR = "INVOICE_FRAUD_UPLOADS_DIR"


def _uploads_dir() -> Path:
    """Directory for persisted upload artifacts (audit trail).

    Overridable via INVOICE_FRAUD_UPLOADS_DIR (tests point it at tmp_path).
    """
    override = os.environ.get(UPLOADS_ENV_VAR)
    directory = Path(override) if override else tools.DATA_DIR / "uploads"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


# ---------------------------------------------------------------------------
# Text extraction per format
# ---------------------------------------------------------------------------


def _pdf_text(path: Path) -> str:
    """Extract text from a PDF with pypdf.

    Raises RuntimeError with a clear message for corrupt files or PDFs
    with no extractable text (e.g. scanned images -- OCR is not supported).
    """
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        parts = [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:
        raise RuntimeError(f"could not read PDF {path.name}: {exc}") from exc
    text = "\n".join(parts).strip()
    if not text:
        raise RuntimeError(
            f"no extractable text in {path.name} -- it may be a scanned "
            "image, and OCR is not supported by this tool."
        )
    return text


def _docx_text(path: Path) -> str:
    """Extract text from a .docx file (paragraphs and tables)."""
    try:
        from docx import Document

        doc = Document(str(path))
        parts = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text for cell in row.cells))
    except Exception as exc:
        raise RuntimeError(f"could not read Word document {path.name}: {exc}") from exc
    text = "\n".join(parts).strip()
    if not text:
        raise RuntimeError(f"no extractable text in {path.name}.")
    return text


def _txt_text(path: Path) -> str:
    """Read plain text (utf-8, falling back to latin-1)."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="latin-1")
    except OSError as exc:
        raise RuntimeError(f"could not read text file {path.name}: {exc}") from exc
    if not text.strip():
        raise RuntimeError(f"{path.name} is empty.")
    return text


def extract_text(path: str | Path) -> str:
    """Extract raw text from a supported invoice document.

    Raises RuntimeError with a clear message for unsupported formats,
    missing files, and unreadable content.
    """
    p = Path(path)
    if not p.is_file():
        raise RuntimeError(f"file not found: {path}")
    ext = p.suffix.lower()
    if ext == ".doc":
        raise RuntimeError(
            "legacy .doc format is not supported -- please convert to .docx."
        )
    if ext not in SUPPORTED_EXTENSIONS:
        raise RuntimeError(
            f"unsupported file type '{ext or '(none)'}' -- supported: "
            + ", ".join(sorted(SUPPORTED_EXTENSIONS))
        )
    if ext == ".pdf":
        return _pdf_text(p)
    if ext == ".docx":
        return _docx_text(p)
    if ext == ".txt":
        return _txt_text(p)
    return p.read_text(encoding="utf-8")  # .xml: raw text for the audit trail


# ---------------------------------------------------------------------------
# Field parsing
# ---------------------------------------------------------------------------


def _to_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw.replace(",", "").strip())
    except ValueError:
        return None


_LINE_RE = re.compile(
    r"^(.+?)\s+x(\d+)\s+@\s+\$([\d,]+\.\d{2})\s+=\s+\$([\d,]+\.\d{2})\s*$",
    re.MULTILINE,
)


def parse_invoice_text(text: str) -> dict:
    """Deterministically parse structured fields from invoice plain text.

    Expected layout (also used for text extracted from PDF/DOCX)::

        VENDOR NAME
        Invoice INV-123 | Date: 2026-09-28 | Due: 2026-10-28
        PO: PO-501            (or "PO: none")
        Widget x4 @ $95.00 = $380.00
        Subtotal: $380.00
        Tax (5%): $19.00
        TOTAL: $399.00

    Returns a fields dict, or ``{"error": ...}`` listing what is missing.
    """
    missing: list[str] = []
    num_m = re.search(r"[Ii]nvoice(?:\s+(?:no|number|#))?[\s:]*([A-Za-z0-9][A-Za-z0-9\-/]*)", text)
    date_m = re.search(r"[Dd]ate:\s*(\d{4}-\d{2}-\d{2})", text)
    due_m = re.search(r"[Dd]ue:\s*(\d{4}-\d{2}-\d{2})", text)
    po_m = re.search(r"^PO:\s*(.+?)\s*$", text, re.MULTILINE)
    sub_m = re.search(r"Subtotal:\s*\$([\d,]+\.\d{2})", text, re.IGNORECASE)
    tax_m = re.search(r"Tax(?:\s*\([^)]*\))?\s*:\s*\$([\d,]+\.\d{2})", text, re.IGNORECASE)
    tot_m = re.search(r"^TOTAL:\s*\$([\d,]+\.\d{2})", text, re.IGNORECASE | re.MULTILINE)

    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    vendor_name = first_line or None

    lines = []
    for desc, qty, unit_price, _line_total in _LINE_RE.findall(text):
        lines.append(
            {
                "description": desc.strip(),
                "qty": int(qty),
                "unit_price": float(unit_price.replace(",", "")),
            }
        )

    if not num_m:
        missing.append("invoice_number")
    if not vendor_name:
        missing.append("vendor_name")
    if not date_m:
        missing.append("issue_date (YYYY-MM-DD)")
    if not lines:
        missing.append("line_items")
    subtotal, tax, total = _to_float(sub_m.group(1) if sub_m else None), _to_float(
        tax_m.group(1) if tax_m else None
    ), _to_float(tot_m.group(1) if tot_m else None)
    if subtotal is None:
        missing.append("subtotal")
    if tax is None:
        missing.append("tax")
    if total is None:
        missing.append("total")
    if missing:
        return {"error": "could not parse invoice text; missing: " + ", ".join(missing)}

    po_raw = po_m.group(1).strip() if po_m else ""
    po_id = None if po_raw.lower() in ("", "none", "n/a", "na", "-") else po_raw

    return {
        "invoice_number": num_m.group(1),
        "vendor_name": vendor_name,
        "issue_date": date_m.group(1),
        "due_date": due_m.group(1) if due_m else None,
        "po_id": po_id,
        "lines": lines,
        "subtotal": subtotal,
        "tax": tax,
        "total": total,
    }


def parse_invoice_xml(text: str) -> dict:
    """Parse structured fields directly from invoice XML.

    Schema (all elements are direct children unless noted)::

        <invoice>
          <invoice_number>INV-9001</invoice_number>   (required)
          <vendor_name>Acme Office Supplies</vendor_name>  (required)
          <issue_date>2026-09-28</issue_date>         (required, YYYY-MM-DD)
          <due_date>2026-10-28</due_date>             (optional)
          <po_number>PO-501</po_number>               (optional)
          <currency>USD</currency>                    (optional)
          <line_items>                               (required, >=1 item)
            <item>
              <description>Widget</description>       (required)
              <quantity>4</quantity>                  (required, integer)
              <unit_price>95.00</unit_price>           (required, number)
            </item>
          </line_items>
          <subtotal>380.00</subtotal>                 (required)
          <tax>19.00</tax>                           (required)
          <total>399.00</total>                      (required)
        </invoice>

    Returns a fields dict, or ``{"error": ...}`` describing violations.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return {"error": f"invalid XML: {exc}"}
    if root.tag != "invoice":
        return {"error": f"root element must be <invoice>, found <{root.tag}>"}

    errors: list[str] = []

    def text_of(tag: str) -> str | None:
        el = root.find(tag)
        return el.text.strip() if el is not None and el.text and el.text.strip() else None

    invoice_number = text_of("invoice_number")
    vendor_name = text_of("vendor_name")
    issue_date = text_of("issue_date")
    due_date = text_of("due_date")
    po_number = text_of("po_number")
    if not invoice_number:
        errors.append("<invoice_number> is required")
    if not vendor_name:
        errors.append("<vendor_name> is required")
    if not issue_date:
        errors.append("<issue_date> is required (YYYY-MM-DD)")
    elif not re.fullmatch(r"\d{4}-\d{2}-\d{2}", issue_date):
        errors.append(f"<issue_date> must be YYYY-MM-DD, got '{issue_date}'")

    lines: list[dict] = []
    items_el = root.find("line_items")
    item_els = items_el.findall("item") if items_el is not None else []
    if not item_els:
        errors.append("<line_items> must contain at least one <item>")
    for idx, item in enumerate(item_els, start=1):
        def item_text(tag: str) -> str | None:
            el = item.find(tag)
            return el.text.strip() if el is not None and el.text and el.text.strip() else None

        desc, qty_raw, price_raw = (
            item_text("description"),
            item_text("quantity"),
            item_text("unit_price"),
        )
        qty = unit_price = None
        if not desc:
            errors.append(f"<item #{idx}>: <description> is required")
        try:
            qty = int(qty_raw) if qty_raw is not None else None
        except ValueError:
            qty = None
        if qty is None:
            errors.append(f"<item #{idx}>: <quantity> must be an integer")
        unit_price = _to_float(price_raw)
        if unit_price is None:
            errors.append(f"<item #{idx}>: <unit_price> must be a number")
        if desc and qty is not None and unit_price is not None:
            lines.append({"description": desc, "qty": qty, "unit_price": unit_price})

    subtotal = _to_float(text_of("subtotal"))
    tax = _to_float(text_of("tax"))
    total = _to_float(text_of("total"))
    for label, value in (("subtotal", subtotal), ("tax", tax), ("total", total)):
        if value is None:
            errors.append(f"<{label}> is required and must be a number")

    if errors:
        return {"error": "invoice XML failed schema validation: " + "; ".join(errors)}

    return {
        "invoice_number": invoice_number,
        "vendor_name": vendor_name,
        "issue_date": issue_date,
        "due_date": due_date,
        "po_id": po_number,
        "lines": lines,
        "subtotal": subtotal,
        "tax": tax,
        "total": total,
    }


# ---------------------------------------------------------------------------
# Case assembly and registration
# ---------------------------------------------------------------------------


def _normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def match_vendor_id(vendor_name: str, vendors: dict) -> str:
    """Resolve a vendor name to the vendor master.

    Returns the master ``vendor_id`` on a case-insensitive name match;
    otherwise a stable ``V-EXT-<slug>`` id so the existing unknown-vendor
    check fires (and repeat uploads of the same unknown vendor keep the
    same id, so duplicate detection still works across uploads).
    """
    norm = _normalize_name(vendor_name)
    for vendor_id, vendor in vendors.items():
        if _normalize_name(vendor.get("name", "")) == norm:
            return vendor_id
    slug = re.sub(r"[^a-z0-9]+", "-", norm).strip("-") or "unknown"
    return f"V-EXT-{slug.upper()}"[:32]


def _next_upload_id(invoices: list[dict]) -> str:
    taken = set()
    for inv in invoices:
        m = re.fullmatch(r"UPLOAD-(\d+)", inv.get("invoice_id", ""))
        if m:
            taken.add(int(m.group(1)))
    n = 1
    while n in taken:
        n += 1
    return f"UPLOAD-{n:04d}"


def _register_case(fields: dict, raw_text: str, source_path: str) -> dict:
    """Build the case dict, register it in-memory, and persist an artifact."""
    invoices, vendors, _ = tools._data()
    invoice_id = _next_upload_id(invoices)
    vendor_id = match_vendor_id(fields["vendor_name"], vendors)
    lines = [
        {
            "description": ln["description"],
            "qty": ln["qty"],
            "unit_price": ln["unit_price"],
            "line_total": round(ln["qty"] * ln["unit_price"], 2),
        }
        for ln in fields["lines"]
    ]
    invoice = {
        "invoice_id": invoice_id,
        "invoice_number": fields.get("invoice_number") or invoice_id,
        "vendor_id": vendor_id,
        "vendor_name": fields["vendor_name"],
        "issue_date": fields["issue_date"],
        "due_date": fields.get("due_date"),
        "po_id": fields.get("po_id"),
        "lines": lines,
        "subtotal": fields["subtotal"],
        "tax": fields["tax"],
        "total": fields["total"],
        "raw_text": raw_text,
        "notes": (
            f"Ingested from {Path(source_path).name} "
            f"({fields.get('extraction_mode', 'mock')} extraction)."
        ),
    }
    invoices.append(invoice)
    artifact = _uploads_dir() / f"{invoice_id}.json"
    artifact.write_text(json.dumps(invoice, indent=2), encoding="utf-8")
    return invoice


def ingest_invoice_document(file_path: str, mode: str = "mock") -> dict:
    """Ingest a supplier invoice document as a new audit case.

    Supported formats: PDF (.pdf), Word (.docx), plain text (.txt), and
    XML (.xml, parsed against the documented invoice schema).

    mode="mock" (default) parses PDF/DOCX/TXT deterministically with no
    API calls. mode="live" uses Gemini extraction (needs GOOGLE_API_KEY),
    falling back to the mock parser with a clear note on failure.

    Returns ``{"invoice_id", "vendor_name", "total", ...}`` on success or
    ``{"error": ...}`` on any failure. The new case immediately flows
    through the existing pipeline and fraud checks unchanged.
    """
    try:
        raw_text = extract_text(file_path)
    except RuntimeError as exc:
        return {"error": str(exc)}

    ext = Path(file_path).suffix.lower()
    if ext == ".xml":
        fields = parse_invoice_xml(raw_text)
        fields["extraction_mode"] = "xml-schema"
    elif mode == "live":
        try:
            fields = tools._live_extract(raw_text, invoice_id="UPLOAD-pending")
        except Exception as exc:  # never crash the pipeline on extraction
            fields = parse_invoice_text(raw_text)
            fields["extraction_mode"] = "mock-fallback"
            fields["note"] = f"Live extraction failed ({exc}); fell back to mock parser."
        else:
            fields["extraction_mode"] = "live"
    else:
        fields = parse_invoice_text(raw_text)
        fields["extraction_mode"] = "mock"

    if "error" in fields:
        return {"error": fields["error"]}

    invoice = _register_case(fields, raw_text, file_path)
    return {
        "invoice_id": invoice["invoice_id"],
        "invoice_number": invoice["invoice_number"],
        "vendor_name": invoice["vendor_name"],
        "vendor_id": invoice["vendor_id"],
        "total": invoice["total"],
        "po_id": invoice["po_id"],
        "extraction_mode": fields["extraction_mode"],
        "source_file": str(file_path),
    }
