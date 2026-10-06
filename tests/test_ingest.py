"""Tests for invoice document ingestion (PDF / DOCX / TXT / XML).

All four formats carry the SAME clean invoice so per-format parity is
asserted; adversarial and negative cases prove the wiring into the
existing fraud checks. No API key needed (mock parsing throughout).
"""
import json

import pytest

from fraud_agent import ingest, tools
from fraud_agent.pipeline import run_pipeline


# ---------------------------------------------------------------------------
# Isolation: never leak uploads into the shared _data() cache or the repo
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_data(tmp_path, monkeypatch):
    saved = (tools._invoices, tools._vendors, tools._pos)
    tools._invoices, tools._vendors, tools._pos = None, None, None
    monkeypatch.setenv(ingest.UPLOADS_ENV_VAR, str(tmp_path / "uploads"))
    yield
    tools._invoices, tools._vendors, tools._pos = saved


# ---------------------------------------------------------------------------
# Fixtures: the same clean invoice in all four formats
# ---------------------------------------------------------------------------

CLEAN_LINES = [
    "QUICKSHIP LOGISTICS",
    "Invoice QS-9001 | Date: 2026-09-28 | Due: 2026-10-28",
    "PO: none",
    "Express freight x4 @ $95.00 = $380.00",
    "Subtotal: $380.00",
    "Tax (5%): $19.00",
    "TOTAL: $399.00",
]
CLEAN_TEXT = "\n".join(CLEAN_LINES)

EXPECTED_FIELDS = {
    "invoice_number": "QS-9001",
    "vendor_name": "QUICKSHIP LOGISTICS",
    "issue_date": "2026-09-28",
    "due_date": "2026-10-28",
    "po_id": None,
    "lines": [{"description": "Express freight", "qty": 4, "unit_price": 95.0}],
    "subtotal": 380.0,
    "tax": 19.0,
    "total": 399.0,
}

CLEAN_XML = """<invoice>
  <invoice_number>QS-9001</invoice_number>
  <vendor_name>QUICKSHIP LOGISTICS</vendor_name>
  <issue_date>2026-09-28</issue_date>
  <due_date>2026-10-28</due_date>
  <currency>USD</currency>
  <line_items>
    <item>
      <description>Express freight</description>
      <quantity>4</quantity>
      <unit_price>95.00</unit_price>
    </item>
  </line_items>
  <subtotal>380.00</subtotal>
  <tax>19.00</tax>
  <total>399.00</total>
</invoice>
"""


def _make_pdf_bytes(lines: list[str]) -> bytes:
    """Build a minimal valid single-page PDF (hand-rolled, no new deps)."""
    content = ["BT /F1 12 Tf 72 750 Td 15 TL"]
    for ln in lines:
        esc = ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content.append(f"({esc}) Tj T*")
    content.append("ET")
    stream = "\n".join(content).encode("latin-1")
    bodies = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode("latin-1") + b" >>\nstream\n"
        + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(bodies, start=1):
        offsets.append(len(pdf))
        pdf += f"{i} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"
    xref_at = len(pdf)
    pdf += f"xref\n0 {len(bodies) + 1}\n".encode("latin-1")
    pdf += b"0000000000 65535 f \n"
    for off in offsets:
        pdf += f"{off:010d} 00000 n \n".encode("latin-1")
    pdf += (
        f"trailer\n<< /Size {len(bodies) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF"
    ).encode("latin-1")
    return pdf


def _write_clean_fixtures(tmp_path):
    """Write the clean invoice in all four formats; return {ext: path}."""
    paths = {}
    txt = tmp_path / "invoice.txt"
    txt.write_text(CLEAN_TEXT, encoding="utf-8")
    paths[".txt"] = txt
    xml = tmp_path / "invoice.xml"
    xml.write_text(CLEAN_XML, encoding="utf-8")
    paths[".xml"] = xml
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(_make_pdf_bytes(CLEAN_LINES))
    paths[".pdf"] = pdf
    from docx import Document

    doc = Document()
    for ln in CLEAN_LINES:
        doc.add_paragraph(ln)
    docx_path = tmp_path / "invoice.docx"
    doc.save(str(docx_path))
    paths[".docx"] = docx_path
    return paths


def _assert_clean_fields(fields: dict):
    assert "error" not in fields, fields.get("error")
    for key, expected in EXPECTED_FIELDS.items():
        assert fields[key] == expected, f"{key}: {fields[key]!r} != {expected!r}"


# ---------------------------------------------------------------------------
# Per-format extraction + parsing
# ---------------------------------------------------------------------------


def test_txt_extract_and_parse(tmp_path):
    paths = _write_clean_fixtures(tmp_path)
    text = ingest.extract_text(paths[".txt"])
    assert "QUICKSHIP LOGISTICS" in text
    assert "QS-9001" in text
    _assert_clean_fields(ingest.parse_invoice_text(text))


def test_docx_extract_and_parse(tmp_path):
    paths = _write_clean_fixtures(tmp_path)
    text = ingest.extract_text(paths[".docx"])
    assert "QUICKSHIP LOGISTICS" in text
    assert "QS-9001" in text
    _assert_clean_fields(ingest.parse_invoice_text(text))


def test_pdf_extract_and_parse(tmp_path):
    paths = _write_clean_fixtures(tmp_path)
    text = ingest.extract_text(paths[".pdf"])
    assert "QUICKSHIP LOGISTICS" in text
    assert "QS-9001" in text
    _assert_clean_fields(ingest.parse_invoice_text(text))


def test_xml_parsed_directly_against_schema(tmp_path):
    paths = _write_clean_fixtures(tmp_path)
    text = ingest.extract_text(paths[".xml"])
    _assert_clean_fields(ingest.parse_invoice_xml(text))


# ---------------------------------------------------------------------------
# End-to-end: ingest each format -> full pipeline -> APPROVE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ext", [".txt", ".xml", ".pdf", ".docx"])
def test_ingest_each_format_audits_clean(tmp_path, ext):
    paths = _write_clean_fixtures(tmp_path)
    result = ingest.ingest_invoice_document(str(paths[ext]))
    assert "error" not in result, result.get("error")
    invoice_id = result["invoice_id"]
    assert invoice_id.startswith("UPLOAD-")
    assert result["vendor_id"] == "V-1004"  # name-matched to QuickShip
    assert result["total"] == 399.0

    audit = run_pipeline(invoice_id, mode="mock", report_dir=str(tmp_path))
    assert audit["verdict"] == "APPROVE", audit["findings"]
    assert audit["findings"] == []
    assert (tmp_path / f"{invoice_id}.md").exists()

    # upload artifact persisted for the audit trail
    artifacts = list((tmp_path / "uploads").glob(f"{invoice_id}.json"))
    assert len(artifacts) == 1
    saved = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert saved["invoice_id"] == invoice_id


def test_upload_ids_sequence_within_run(tmp_path):
    paths = _write_clean_fixtures(tmp_path)
    first = ingest.ingest_invoice_document(str(paths[".txt"]))
    second = ingest.ingest_invoice_document(str(paths[".xml"]))
    assert first["invoice_id"] == "UPLOAD-0001"
    assert second["invoice_id"] == "UPLOAD-0002"


# ---------------------------------------------------------------------------
# Adversarial: ingested fraud must trip the existing checks
# ---------------------------------------------------------------------------


def test_ingested_duplicate_is_rejected(tmp_path):
    invoices, _, _ = tools._data()
    original = next(i for i in invoices if i["invoice_id"] == "INV-2026-0101")
    dup = tmp_path / "dup.txt"
    dup.write_text(original["raw_text"], encoding="utf-8")  # same vendor/number/amount
    result = ingest.ingest_invoice_document(str(dup))
    assert "error" not in result, result.get("error")
    audit = run_pipeline(result["invoice_id"], mode="mock", report_dir=str(tmp_path))
    assert audit["verdict"] == "REJECT"
    assert "duplicate" in {f["check"] for f in audit["findings"]}


def test_ingested_math_error_is_flagged(tmp_path):
    bad = tmp_path / "bad.txt"
    bad.write_text(
        "\n".join(
            [
                "QUICKSHIP LOGISTICS",
                "Invoice QS-9002 | Date: 2026-09-28 | Due: 2026-10-28",
                "PO: none",
                "Express freight x4 @ $95.00 = $380.00",
                "Subtotal: $380.00",
                "Tax (5%): $19.00",
                "TOTAL: $429.00",  # 380 + 19 = 399, not 429
            ]
        ),
        encoding="utf-8",
    )
    result = ingest.ingest_invoice_document(str(bad))
    assert "error" not in result, result.get("error")
    audit = run_pipeline(result["invoice_id"], mode="mock", report_dir=str(tmp_path))
    assert audit["verdict"] == "FLAG_FOR_REVIEW"
    assert "math" in {f["check"] for f in audit["findings"]}


def test_ingested_unknown_vendor_is_rejected(tmp_path):
    mystery = tmp_path / "mystery.txt"
    mystery.write_text(
        "\n".join(
            [
                "FLY-BY-NIGHT TRADERS",
                "Invoice FBN-1 | Date: 2026-09-28 | Due: 2026-10-28",
                "PO: none",
                "Consulting x1 @ $100.00 = $100.00",
                "Subtotal: $100.00",
                "Tax (0%): $0.00",
                "TOTAL: $100.00",
            ]
        ),
        encoding="utf-8",
    )
    result = ingest.ingest_invoice_document(str(mystery))
    assert "error" not in result, result.get("error")
    assert result["vendor_id"].startswith("V-EXT-")
    audit = run_pipeline(result["invoice_id"], mode="mock", report_dir=str(tmp_path))
    assert audit["verdict"] == "REJECT"
    assert "vendor" in {f["check"] for f in audit["findings"]}


# ---------------------------------------------------------------------------
# Negative cases: clean error dicts, never tracebacks
# ---------------------------------------------------------------------------


def test_unsupported_extension_rejected(tmp_path):
    p = tmp_path / "invoice.csv"
    p.write_text("a,b,c", encoding="utf-8")
    result = ingest.ingest_invoice_document(str(p))
    assert "error" in result and "unsupported file type" in result["error"]


def test_legacy_doc_rejected_with_hint(tmp_path):
    p = tmp_path / "invoice.doc"
    p.write_bytes(b"\xd0\xcf\x11\xe0" + b"\x00" * 64)
    result = ingest.ingest_invoice_document(str(p))
    assert "error" in result and ".docx" in result["error"]


def test_missing_file_rejected(tmp_path):
    result = ingest.ingest_invoice_document(str(tmp_path / "nope.pdf"))
    assert "error" in result and "not found" in result["error"]


def test_corrupt_pdf_rejected(tmp_path):
    p = tmp_path / "broken.pdf"
    p.write_bytes(b"%PDF-1.4\nthis is not a real pdf body")
    result = ingest.ingest_invoice_document(str(p))
    assert "error" in result


def test_empty_txt_rejected(tmp_path):
    p = tmp_path / "empty.txt"
    p.write_text("", encoding="utf-8")
    result = ingest.ingest_invoice_document(str(p))
    assert "error" in result and "empty" in result["error"]


def test_garbled_txt_rejected(tmp_path):
    p = tmp_path / "garbled.txt"
    p.write_text("hello world\nno invoice here", encoding="utf-8")
    result = ingest.ingest_invoice_document(str(p))
    assert "error" in result and "could not parse" in result["error"]


def test_malformed_xml_rejected(tmp_path):
    p = tmp_path / "bad.xml"
    p.write_text("<invoice><invoice_number>X", encoding="utf-8")
    result = ingest.ingest_invoice_document(str(p))
    assert "error" in result and "invalid XML" in result["error"]


def test_xml_schema_violation_rejected(tmp_path):
    p = tmp_path / "incomplete.xml"
    p.write_text(
        "<invoice><invoice_number>X-1</invoice_number>"
        "<vendor_name>Acme</vendor_name>"
        "<issue_date>2026-09-28</issue_date>"
        "<line_items><item><description>w</description>"
        "<quantity>1</quantity><unit_price>10.00</unit_price></item></line_items>"
        "<subtotal>10.00</subtotal><tax>0.00</tax></invoice>",
        encoding="utf-8",
    )
    result = ingest.ingest_invoice_document(str(p))
    assert "error" in result and "total" in result["error"]
