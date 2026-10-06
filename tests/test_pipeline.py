"""End-to-end pipeline tests on the labeled sample invoices (mock mode)."""
import pytest

from fraud_agent import tools
from fraud_agent.pipeline import run_pipeline


@pytest.mark.parametrize("invoice_id", tools.labeled_invoice_ids())
def test_labeled_verdicts_match(invoice_id, tmp_path):
    invoices, _, _ = tools._data()
    expected = next(
        i["expected_verdict"] for i in invoices if i["invoice_id"] == invoice_id
    )
    result = run_pipeline(invoice_id, mode="mock", report_dir=str(tmp_path))
    assert result["verdict"] == expected, (
        f"{invoice_id}: got {result['verdict']}, expected {expected}; "
        f"findings={[ (f['check'], f['severity']) for f in result['findings']]}"
    )


def test_fraudulent_samples_have_findings(tmp_path):
    invoices, _, _ = tools._data()
    for inv in invoices:
        if inv.get("expected_verdict") in ("REJECT", "FLAG_FOR_REVIEW"):
            result = run_pipeline(inv["invoice_id"], mode="mock",
                                  report_dir=str(tmp_path))
            assert result["findings"], f"{inv['invoice_id']} should have findings"


def test_clean_samples_have_no_findings(tmp_path):
    invoices, _, _ = tools._data()
    for inv in invoices:
        if inv.get("expected_verdict") == "APPROVE":
            result = run_pipeline(inv["invoice_id"], mode="mock",
                                  report_dir=str(tmp_path))
            assert result["findings"] == [], (
                f"{inv['invoice_id']} unexpected findings: {result['findings']}"
            )


def test_each_fraud_pattern_detected(tmp_path):
    """Each of the five classic patterns must fire its named check."""
    expectations = {
        "INV-2026-0104": "duplicate",
        "INV-2026-0105": "split_invoicing",
        "INV-2026-0108": "po_match",
        "INV-2026-0109": "vendor",
        "INV-2026-0110": "math",
    }
    for invoice_id, check_name in expectations.items():
        result = run_pipeline(invoice_id, mode="mock", report_dir=str(tmp_path))
        fired = {f["check"] for f in result["findings"]}
        assert check_name in fired, (
            f"{invoice_id}: expected {check_name} to fire, got {fired}"
        )


def test_report_written(tmp_path):
    result = run_pipeline("INV-2026-0101", mode="mock", report_dir=str(tmp_path))
    path = tmp_path / "INV-2026-0101.md"
    assert path.exists()
    text = path.read_text()
    assert "APPROVE" in text
    assert result["report_path"] == str(path)


def test_unknown_invoice_errors():
    result = run_pipeline("INV-DOES-NOT-EXIST", mode="mock")
    assert "error" in result


def test_mock_extraction_is_deterministic():
    a = tools.extract_invoice_fields("INV-2026-0101", mode="mock")
    b = tools.extract_invoice_fields("INV-2026-0101", mode="mock")
    assert a == b
    assert a["extraction_mode"] == "mock"
    assert a["total"] == 3385.8
