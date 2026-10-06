"""Deterministic offline pipeline: intake -> extract -> validate -> verdict -> report.

This is the same stage order as the ADK SequentialAgent in agent.py, but
executed directly in Python so it runs with zero setup and no API key.
The ADK agents call the same tool functions in tools.py, so the two paths
cannot diverge.
"""
from __future__ import annotations

from . import tools


def run_pipeline(
    invoice_id: str, mode: str = "mock", report_dir: str | None = None
) -> dict:
    """Run the full fraud-audit pipeline for one invoice.

    mode="mock"  -- deterministic, no network, no API key.
    mode="live"  -- Gemini extraction (needs GOOGLE_API_KEY); falls back
                    to mock extraction if the call fails.
    """
    case = tools.load_invoice_case(invoice_id)
    if "error" in case:
        return {"invoice_id": invoice_id, "error": case["error"]}

    extracted = tools.extract_invoice_fields(invoice_id, mode=mode)
    checked = tools.run_fraud_checks(invoice_id)
    decision = tools.decide_invoice_verdict(invoice_id)
    written = tools.write_case_report(invoice_id, report_dir=report_dir)

    return {
        "invoice_id": invoice_id,
        "vendor_name": case["invoice"].get("vendor_name"),
        "total": case["invoice"]["total"],
        "stages": {
            "intake": {"ok": True},
            "extract": {
                "mode": extracted.get("extraction_mode"),
                "fields": extracted,
            },
            "validate": {"findings": checked["findings"]},
            "verdict": {
                "verdict": decision["verdict"],
                "recommended_action": decision["recommended_action"],
            },
            "report": {"report_path": written.get("report_path")},
        },
        "findings": checked["findings"],
        "verdict": decision["verdict"],
        "recommended_action": decision["recommended_action"],
        "report_path": written.get("report_path"),
        "report_markdown": written and tools.render_case_report(invoice_id)["markdown"],
    }
