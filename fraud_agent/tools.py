"""Tool functions for the invoice fraud agent.

Two consumers share these functions, which keeps one source of truth:

* :mod:`fraud_agent.pipeline` calls them directly for the deterministic
  offline pipeline (mock mode, no API key needed).
* :mod:`fraud_agent.agent` wraps them in ADK ``FunctionTool``\\ s so the
  SequentialAgent can call them.

Every function is a module-level, annotated, docstringed function so ADK
can build a tool schema from it. All return JSON-serializable dicts.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import checks

DATA_DIR = Path(__file__).parent / "data"
REPORT_DIR = Path(__file__).parent / "reports"


def _load_json(name: str):
    with open(DATA_DIR / name, encoding="utf-8") as f:
        return json.load(f)


_invoices: list[dict] | None = None
_vendors: dict | None = None
_pos: dict | None = None


def _data():
    global _invoices, _vendors, _pos
    if _invoices is None:
        _invoices = _load_json("invoices.json")
        _vendors = {v["vendor_id"]: v for v in _load_json("vendors.json")}
        _pos = {p["po_id"]: p for p in _load_json("purchase_orders.json")}
    return _invoices, _vendors, _pos


def labeled_invoice_ids() -> list[str]:
    """IDs of the labeled sample invoices (the demo/test set)."""
    invoices, _, _ = _data()
    return [i["invoice_id"] for i in invoices if "expected_verdict" in i]


def load_invoice_case(invoice_id: str) -> dict:
    """Load the full case file for one invoice (header, lines, totals,
    raw document text, vendor and PO context)."""
    invoices, vendors, pos = _data()
    matches = [i for i in invoices if i["invoice_id"] == invoice_id]
    if not matches:
        return {"error": f"unknown invoice_id: {invoice_id}"}
    invoice = dict(matches[0])
    vendor = vendors.get(invoice["vendor_id"])
    po = pos.get(invoice["po_id"]) if invoice.get("po_id") else None
    return {
        "invoice": invoice,
        "vendor": vendor,
        "purchase_order": po,
        "history_count": len(invoices),
    }


def _mock_extract(invoice: dict) -> dict:
    """Deterministic stand-in for LLM extraction: returns the known
    structured fields, clearly labeled as mock."""
    return {
        "invoice_id": invoice["invoice_id"],
        "invoice_number": invoice.get("invoice_number"),
        "vendor_id": invoice["vendor_id"],
        "vendor_name": invoice.get("vendor_name"),
        "issue_date": invoice["issue_date"],
        "due_date": invoice.get("due_date"),
        "po_id": invoice.get("po_id"),
        "lines": invoice["lines"],
        "subtotal": invoice["subtotal"],
        "tax": invoice["tax"],
        "total": invoice["total"],
        "extraction_mode": "mock",
        "note": "Deterministic mock extraction -- fields taken from the case "
        "file. Set mode='live' with GOOGLE_API_KEY for real Gemini extraction.",
    }


def _live_extract(raw_text: str, invoice_id: str) -> dict:
    """Extract structured invoice fields from raw document text with Gemini."""
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is not set; cannot run live extraction.")
    from google import genai  # local import: google-genai only needed live

    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    client = genai.Client(api_key=api_key)
    prompt = (
        "Extract the supplier invoice below into JSON with exactly these keys: "
        "invoice_number (string), vendor_name (string), issue_date (YYYY-MM-DD), "
        "due_date (YYYY-MM-DD or null), po_id (string or null), "
        "lines (array of {description, qty, unit_price}), "
        "subtotal (number), tax (number), total (number). "
        "Return JSON only, no markdown fences.\n\nINVOICE:\n" + raw_text
    )
    response = client.models.generate_content(model=model, contents=prompt)
    text = response.text.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    data = json.loads(text)
    data["invoice_id"] = invoice_id
    data["extraction_mode"] = "live"
    return data


def extract_invoice_fields(invoice_id: str, mode: str = "mock") -> dict:
    """Extract structured fields from the invoice document.

    mode="mock" (default) returns deterministic fields with no API calls.
    mode="live" calls Gemini on the raw document text and requires
    GOOGLE_API_KEY.
    """
    case = load_invoice_case(invoice_id)
    if "error" in case:
        return case
    invoice = case["invoice"]
    if mode == "live":
        try:
            return _live_extract(invoice["raw_text"], invoice_id)
        except Exception as exc:  # never crash the pipeline on extraction
            result = _mock_extract(invoice)
            result["extraction_mode"] = "mock-fallback"
            result["note"] = f"Live extraction failed ({exc}); fell back to mock."
            return result
    return _mock_extract(invoice)


def run_fraud_checks(invoice_id: str) -> dict:
    """Run the deterministic fraud checks (duplicates, split invoicing,
    PO match, vendor, math) against one invoice. Returns findings."""
    invoices, vendors, pos = _data()
    matches = [i for i in invoices if i["invoice_id"] == invoice_id]
    if not matches:
        return {"error": f"unknown invoice_id: {invoice_id}"}
    findings = checks.run_all_checks(matches[0], vendors, pos, invoices)
    return {"invoice_id": invoice_id, "findings": findings}


def decide_invoice_verdict(invoice_id: str) -> dict:
    """Aggregate fraud-check findings into a verdict: APPROVE,
    FLAG_FOR_REVIEW, or REJECT, with a recommended human action.

    The agent never pays or posts anything -- the verdict is a
    recommendation for a human reviewer.
    """
    result = run_fraud_checks(invoice_id)
    if "error" in result:
        return result
    verdict = checks.decide_verdict(result["findings"])
    return {"invoice_id": invoice_id, **verdict, "findings": result["findings"]}


def render_case_report(invoice_id: str) -> dict:
    """Render the audit case file for one invoice as markdown (not yet saved)."""
    invoices, vendors, _ = _data()
    matches = [i for i in invoices if i["invoice_id"] == invoice_id]
    if not matches:
        return {"error": f"unknown invoice_id: {invoice_id}"}
    invoice = matches[0]
    vendor = vendors.get(invoice["vendor_id"], {})
    decision = decide_invoice_verdict(invoice_id)
    lines = [
        f"# Fraud audit: {invoice_id}",
        "",
        f"- **Vendor:** {invoice.get('vendor_name')} ({invoice['vendor_id']})",
        f"- **Invoice no.:** {invoice.get('invoice_number')}",
        f"- **Issue date:** {invoice['issue_date']} | **Due:** {invoice.get('due_date')}",
        f"- **PO:** {invoice.get('po_id') or 'none'}",
        f"- **Total:** ${invoice['total']:,.2f}",
        f"- **Vendor risk tier:** {vendor.get('risk_tier', 'unknown')}",
        "",
        f"## Verdict: {decision['verdict']}",
        "",
        f"Recommended action: {decision['recommended_action']}",
        "",
        f"## Findings ({decision['finding_count']})",
        "",
    ]
    if not decision["findings"]:
        lines.append("No findings. All checks passed.")
    for f in decision["findings"]:
        lines += [
            f"### [{f['severity'].upper()}] {f['check']}",
            "",
            f["evidence"],
            "",
        ]
    lines += [
        "---",
        "_Generated by invoice-fraud-agent. Verdicts are recommendations; "
        "a human reviewer makes the final call._",
    ]
    return {
        "invoice_id": invoice_id,
        "verdict": decision["verdict"],
        "markdown": "\n".join(lines),
    }


def write_case_report(invoice_id: str, report_dir: str | None = None) -> dict:
    """Write the audit case file for one invoice to disk as markdown."""
    report = render_case_report(invoice_id)
    if "error" in report:
        return report
    out_dir = Path(report_dir) if report_dir else REPORT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{invoice_id}.md"
    path.write_text(report["markdown"], encoding="utf-8")
    return {
        "invoice_id": invoice_id,
        "verdict": report["verdict"],
        "report_path": str(path),
    }
