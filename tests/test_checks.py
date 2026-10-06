"""Unit tests for the deterministic fraud checks (checks.py).

Every check gets positive and negative cases with small inline fixtures --
no fixtures from the sample data set, so the checks are tested
independently of the demo data.
"""
import pytest

from fraud_agent import checks


def make_invoice(**kw):
    base = {
        "invoice_id": "T-1",
        "invoice_number": "A-1",
        "vendor_id": "V-1",
        "vendor_name": "Test Vendor",
        "issue_date": "2026-09-01",
        "due_date": "2026-10-01",
        "po_id": None,
        "lines": [
            {
                "description": "Widget",
                "qty": 1,
                "unit_price": 100.0,
                "line_total": 100.0,
            }
        ],
        "subtotal": 100.0,
        "tax": 0.0,
        "total": 100.0,
    }
    base.update(kw)
    return base


def make_vendor(**kw):
    base = {
        "vendor_id": "V-1",
        "name": "Test Vendor",
        "approved": True,
        "risk_tier": "low",
    }
    base.update(kw)
    return base


# --- duplicates -----------------------------------------------------------

def test_duplicate_same_number_flagged():
    inv = make_invoice()
    other = make_invoice(invoice_id="T-0", issue_date="2026-08-20", total=100.0)
    findings = checks.check_duplicate(inv, [other])
    assert len(findings) == 1
    assert findings[0]["check"] == "duplicate"
    assert findings[0]["severity"] == "high"
    assert "T-0" in findings[0]["evidence"]


def test_duplicate_same_amount_and_date_flagged():
    inv = make_invoice(invoice_number="B-9")
    other = make_invoice(invoice_id="T-0", invoice_number="A-1", total=100.0,
                         issue_date="2026-09-01")
    findings = checks.check_duplicate(inv, [other])
    assert findings and findings[0]["severity"] == "high"


def test_duplicate_different_invoice_clean():
    inv = make_invoice()
    other = make_invoice(invoice_id="T-0", invoice_number="ZZZ", total=250.0,
                         issue_date="2026-08-20")
    assert checks.check_duplicate(inv, [other]) == []


def test_duplicate_ignores_self():
    inv = make_invoice()
    assert checks.check_duplicate(inv, [inv]) == []


# --- split invoicing ------------------------------------------------------

def _split_history():
    return [
        make_invoice(invoice_id="S-1", invoice_number="S-1", total=4000.0,
                     issue_date="2026-08-10"),
        make_invoice(invoice_id="S-2", invoice_number="S-2", total=4000.0,
                     issue_date="2026-08-20"),
    ]


def test_split_invoicing_flagged():
    inv = make_invoice(invoice_id="S-3", invoice_number="S-3", total=4000.0,
                       issue_date="2026-08-28")
    findings = checks.check_split_invoicing(inv, _split_history())
    assert len(findings) == 1
    assert findings[0]["severity"] == "high"
    assert "12,000.00" in findings[0]["evidence"]
    assert set(findings[0]["details"]["cluster"]) == {"S-1", "S-2", "S-3"}


def test_split_invoicing_single_invoice_clean():
    inv = make_invoice(total=4000.0)
    assert checks.check_split_invoicing(inv, []) == []


def test_split_invoicing_cluster_under_threshold_clean():
    inv = make_invoice(invoice_id="S-3", invoice_number="S-3", total=100.0)
    history = [make_invoice(invoice_id="S-1", invoice_number="S-1", total=100.0)]
    assert checks.check_split_invoicing(inv, history) == []


def test_split_invoicing_large_invoice_skipped():
    inv = make_invoice(total=50_000.0)
    assert checks.check_split_invoicing(inv, _split_history()) == []


def test_split_invoicing_outside_window_clean():
    inv = make_invoice(invoice_id="S-3", invoice_number="S-3", total=4000.0,
                       issue_date="2026-12-01")
    assert checks.check_split_invoicing(inv, _split_history()) == []


# --- PO matching ----------------------------------------------------------

def _po():
    return {
        "po_id": "PO-1",
        "vendor_id": "V-1",
        "lines": [{"description": "Widget", "qty": 2, "unit_price": 50.0}],
        "total": 100.0,
    }


def _po_invoice(**kw):
    base = dict(
        po_id="PO-1",
        lines=[{"description": "Widget", "qty": 2, "unit_price": 50.0,
                "line_total": 100.0}],
        subtotal=100.0,
        total=100.0,
    )
    base.update(kw)
    return make_invoice(**base)


def test_po_match_exact_clean():
    assert checks.check_po_match(_po_invoice(), _po()) == []


def test_po_price_deviation_high():
    inv = _po_invoice(lines=[{"description": "Widget", "qty": 2,
                              "unit_price": 75.0, "line_total": 150.0}],
                      subtotal=150.0, total=150.0)
    findings = checks.check_po_match(inv, _po())
    price = [f for f in findings if "Unit price mismatch" in f["evidence"]]
    assert price and price[0]["severity"] == "high"  # 50% over


def test_po_price_deviation_medium():
    inv = _po_invoice(lines=[{"description": "Widget", "qty": 2,
                              "unit_price": 55.0, "line_total": 110.0}],
                      subtotal=110.0, total=110.0)
    findings = checks.check_po_match(inv, _po())
    price = [f for f in findings if "Unit price mismatch" in f["evidence"]]
    assert price and price[0]["severity"] == "medium"  # 10% over


def test_po_quantity_mismatch():
    inv = _po_invoice(lines=[{"description": "Widget", "qty": 5,
                              "unit_price": 50.0, "line_total": 250.0}],
                      subtotal=250.0, total=250.0)
    findings = checks.check_po_match(inv, _po())
    assert any("Quantity mismatch" in f["evidence"] for f in findings)


def test_po_line_not_on_po():
    inv = _po_invoice(lines=[{"description": "Gadget", "qty": 1,
                              "unit_price": 100.0, "line_total": 100.0}])
    findings = checks.check_po_match(inv, _po())
    assert any("not on PO" in f["evidence"] for f in findings)


def test_po_missing_on_large_invoice():
    inv = make_invoice(total=5000.0, po_id=None)
    findings = checks.check_po_match(inv, None)
    assert findings and findings[0]["severity"] == "medium"


def test_po_missing_on_small_invoice_clean():
    inv = make_invoice(total=100.0, po_id=None)
    assert checks.check_po_match(inv, None) == []


# --- vendor ---------------------------------------------------------------

def test_vendor_unknown_high():
    findings = checks.check_vendor(make_invoice(vendor_id="VX"), None)
    assert findings and findings[0]["severity"] == "high"


def test_vendor_unapproved_high():
    findings = checks.check_vendor(
        make_invoice(), make_vendor(approved=False, risk_tier="high"))
    assert any(f["severity"] == "high" and "not approved" in f["evidence"]
               for f in findings)


def test_vendor_high_risk_tier_medium():
    findings = checks.check_vendor(make_invoice(), make_vendor(risk_tier="high"))
    assert findings and findings[0]["severity"] == "medium"


def test_vendor_approved_low_clean():
    assert checks.check_vendor(make_invoice(), make_vendor()) == []


# --- math -----------------------------------------------------------------

def test_math_clean():
    assert checks.check_math(make_invoice()) == []


def test_math_line_total_mismatch():
    inv = make_invoice(lines=[{"description": "Widget", "qty": 2,
                               "unit_price": 50.0, "line_total": 120.0}],
                       subtotal=100.0, total=100.0)
    findings = checks.check_math(inv)
    assert any("2 x $50.00 = $100.00" in f["evidence"] for f in findings)


def test_math_total_mismatch():
    inv = make_invoice(total=150.0)
    findings = checks.check_math(inv)
    assert any("Total mismatch" in f["evidence"] for f in findings)
    assert all(f["severity"] == "medium" for f in findings)


def test_math_subtotal_mismatch():
    inv = make_invoice(subtotal=90.0, total=90.0)
    findings = checks.check_math(inv)
    assert any("Subtotal mismatch" in f["evidence"] for f in findings)


# --- verdict --------------------------------------------------------------

def test_decide_verdict_reject_on_high():
    d = checks.decide_verdict([{"severity": "high"}])
    assert d["verdict"] == "REJECT"
    assert "Do not pay" in d["recommended_action"]


def test_decide_verdict_flag_on_medium():
    d = checks.decide_verdict([{"severity": "medium"}, {"severity": "low"}])
    assert d["verdict"] == "FLAG_FOR_REVIEW"


def test_decide_verdict_approve_when_clean():
    d = checks.decide_verdict([])
    assert d["verdict"] == "APPROVE"


def test_run_all_checks_missing_po_reference():
    inv = make_invoice(po_id="PO-NOPE")
    findings = checks.run_all_checks(inv, {"V-1": make_vendor()}, {}, [])
    assert any("does not exist" in f["evidence"] for f in findings)
