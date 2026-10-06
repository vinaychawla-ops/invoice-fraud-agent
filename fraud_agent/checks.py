"""Deterministic fraud checks for supplier invoices.

Every check is a pure function: (invoice, context) -> list of findings.
A finding is a dict::

    {"check": str, "severity": "high"|"medium"|"low",
     "evidence": str, "details": dict}

No LLM calls, no I/O, no side effects -- safe to unit test exhaustively
and to run offline.
"""
from __future__ import annotations

from datetime import datetime

from .config import (
    APPROVAL_THRESHOLD,
    MATH_TOLERANCE,
    NO_PO_LIMIT,
    PO_PRICE_HIGH_WATERMARK,
    PO_PRICE_TOLERANCE,
    SPLIT_WINDOW_DAYS,
)


def _parse(iso_date: str):
    return datetime.strptime(iso_date, "%Y-%m-%d").date()


def check_duplicate(invoice: dict, history: list[dict]) -> list[dict]:
    """Flag an invoice already seen: same vendor + (same invoice number or
    same amount on the same date).

    Directional: only the *later* invoice is flagged. The first occurrence
    is treated as the legitimate original; a resubmission is the duplicate.
    """
    mine = (invoice["issue_date"], invoice["invoice_id"])
    for other in history:
        if other["invoice_id"] == invoice["invoice_id"]:
            continue
        if other["vendor_id"] != invoice["vendor_id"]:
            continue
        if (other["issue_date"], other["invoice_id"]) > mine:
            continue  # the other one is the later resubmission, not us
        same_number = bool(other.get("invoice_number")) and (
            other.get("invoice_number") == invoice.get("invoice_number")
        )
        same_amount_date = (
            abs(other["total"] - invoice["total"]) <= MATH_TOLERANCE
            and other["issue_date"] == invoice["issue_date"]
        )
        if same_number or same_amount_date:
            return [
                {
                    "check": "duplicate",
                    "severity": "high",
                    "evidence": (
                        f"Possible duplicate of {other['invoice_id']}: same vendor "
                        f"({invoice['vendor_id']}), invoice number "
                        f"'{invoice.get('invoice_number')}', amount "
                        f"${invoice['total']:,.2f}."
                    ),
                    "details": {"matched_invoice_id": other["invoice_id"]},
                }
            ]
    return []


def check_split_invoicing(invoice: dict, history: list[dict]) -> list[dict]:
    """Flag clusters of invoices from one vendor that each sit under the
    approval threshold but together exceed it within the time window --
    the classic split-invoicing evasion pattern."""
    if invoice["total"] >= APPROVAL_THRESHOLD:
        return []
    day0 = _parse(invoice["issue_date"])
    cluster = [invoice]
    for other in history:
        if other["invoice_id"] == invoice["invoice_id"]:
            continue
        if other["vendor_id"] != invoice["vendor_id"]:
            continue
        if other["total"] >= APPROVAL_THRESHOLD:
            continue
        if abs((_parse(other["issue_date"]) - day0).days) <= SPLIT_WINDOW_DAYS:
            cluster.append(other)
    if len(cluster) < 2:
        return []
    cluster_total = sum(i["total"] for i in cluster)
    if cluster_total <= APPROVAL_THRESHOLD:
        return []
    members = ", ".join(
        f"{i['invoice_id']} (${i['total']:,.2f} on {i['issue_date']})"
        for i in sorted(cluster, key=lambda x: x["issue_date"])
    )
    return [
        {
            "check": "split_invoicing",
            "severity": "high",
            "evidence": (
                f"{len(cluster)} invoices from vendor {invoice['vendor_id']} within "
                f"{SPLIT_WINDOW_DAYS} days, each under the "
                f"${APPROVAL_THRESHOLD:,.0f} approval threshold but totaling "
                f"${cluster_total:,.2f}: {members}. Possible split invoicing "
                f"to evade approval."
            ),
            "details": {
                "cluster": sorted(i["invoice_id"] for i in cluster),
                "cluster_total": round(cluster_total, 2),
            },
        }
    ]


def check_po_match(invoice: dict, po: dict | None) -> list[dict]:
    """Compare the invoice against its purchase order: quantities, unit
    prices, line coverage, and totals. Missing PO on a large invoice is
    itself a finding."""
    findings: list[dict] = []
    if po is None:
        if invoice["total"] >= NO_PO_LIMIT:
            findings.append(
                {
                    "check": "po_match",
                    "severity": "medium",
                    "evidence": (
                        f"No purchase order referenced for invoice "
                        f"{invoice['invoice_id']} of ${invoice['total']:,.2f} "
                        f"(PO required at or above ${NO_PO_LIMIT:,.0f})."
                    ),
                    "details": {},
                }
            )
        return findings

    po_lines = {line["description"].strip().lower(): line for line in po["lines"]}
    for line in invoice["lines"]:
        key = line["description"].strip().lower()
        po_line = po_lines.get(key)
        if po_line is None:
            findings.append(
                {
                    "check": "po_match",
                    "severity": "medium",
                    "evidence": (
                        f"Line '{line['description']}' is not on PO {po['po_id']}."
                    ),
                    "details": {"line": line["description"]},
                }
            )
            continue
        if line["qty"] != po_line["qty"]:
            findings.append(
                {
                    "check": "po_match",
                    "severity": "medium",
                    "evidence": (
                        f"Quantity mismatch on '{line['description']}': invoiced "
                        f"{line['qty']}, PO {po['po_id']} allows {po_line['qty']}."
                    ),
                    "details": {
                        "line": line["description"],
                        "invoiced_qty": line["qty"],
                        "po_qty": po_line["qty"],
                    },
                }
            )
        if po_line["unit_price"]:
            deviation = (
                abs(line["unit_price"] - po_line["unit_price"])
                / po_line["unit_price"]
            )
            if deviation > PO_PRICE_TOLERANCE:
                severity = (
                    "high" if deviation > PO_PRICE_HIGH_WATERMARK else "medium"
                )
                findings.append(
                    {
                        "check": "po_match",
                        "severity": severity,
                        "evidence": (
                            f"Unit price mismatch on '{line['description']}': "
                            f"invoiced ${line['unit_price']:,.2f} vs PO "
                            f"{po['po_id']} ${po_line['unit_price']:,.2f} "
                            f"({deviation:.1%} deviation, tolerance "
                            f"{PO_PRICE_TOLERANCE:.0%})."
                        ),
                        "details": {
                            "line": line["description"],
                            "invoiced_price": line["unit_price"],
                            "po_price": po_line["unit_price"],
                            "deviation": round(deviation, 4),
                        },
                    }
                )
    if po["total"] > 0:
        deviation = abs(invoice["subtotal"] - po["total"]) / po["total"]
        if deviation > PO_PRICE_TOLERANCE:
            findings.append(
                {
                    "check": "po_match",
                    "severity": "medium",
                    "evidence": (
                        f"Invoice subtotal ${invoice['subtotal']:,.2f} deviates "
                        f"{deviation:.1%} from PO {po['po_id']} total "
                        f"${po['total']:,.2f}."
                    ),
                    "details": {"deviation": round(deviation, 4)},
                }
            )
    return findings


def check_vendor(invoice: dict, vendor: dict | None) -> list[dict]:
    """Vendor-master checks: unknown vendor, unapproved vendor, high risk tier."""
    if vendor is None:
        return [
            {
                "check": "vendor",
                "severity": "high",
                "evidence": (
                    f"Vendor {invoice['vendor_id']} "
                    f"('{invoice.get('vendor_name', '?')}') is not in the vendor "
                    f"master. Invoices from unknown vendors must be rejected "
                    f"until the vendor is onboarded."
                ),
                "details": {},
            }
        ]
    findings: list[dict] = []
    if not vendor.get("approved", False):
        findings.append(
            {
                "check": "vendor",
                "severity": "high",
                "evidence": (
                    f"Vendor {vendor['vendor_id']} ('{vendor['name']}') is not "
                    f"approved for invoicing."
                ),
                "details": {},
            }
        )
    elif vendor.get("risk_tier") == "high":
        findings.append(
            {
                "check": "vendor",
                "severity": "medium",
                "evidence": (
                    f"Vendor {vendor['vendor_id']} ('{vendor['name']}') is "
                    f"approved but carries a high risk tier."
                ),
                "details": {},
            }
        )
    return findings


def check_math(invoice: dict) -> list[dict]:
    """Reconcile the arithmetic: line totals, subtotal, and grand total."""
    findings: list[dict] = []
    calc_subtotal = 0.0
    for line in invoice["lines"]:
        expected = round(line["qty"] * line["unit_price"], 2)
        calc_subtotal += expected
        stated = line.get("line_total", expected)
        if abs(expected - stated) > MATH_TOLERANCE:
            findings.append(
                {
                    "check": "math",
                    "severity": "medium",
                    "evidence": (
                        f"Line '{line['description']}': {line['qty']} x "
                        f"${line['unit_price']:,.2f} = ${expected:,.2f}, but the "
                        f"invoice states ${stated:,.2f}."
                    ),
                    "details": {
                        "line": line["description"],
                        "expected": expected,
                        "stated": stated,
                    },
                }
            )
    if abs(calc_subtotal - invoice["subtotal"]) > MATH_TOLERANCE:
        findings.append(
            {
                "check": "math",
                "severity": "medium",
                "evidence": (
                    f"Subtotal mismatch: line items sum to "
                    f"${calc_subtotal:,.2f}, invoice states "
                    f"${invoice['subtotal']:,.2f}."
                ),
                "details": {
                    "calculated": round(calc_subtotal, 2),
                    "stated": invoice["subtotal"],
                },
            }
        )
    calc_total = round(invoice["subtotal"] + invoice["tax"], 2)
    if abs(calc_total - invoice["total"]) > MATH_TOLERANCE:
        findings.append(
            {
                "check": "math",
                "severity": "medium",
                "evidence": (
                    f"Total mismatch: subtotal ${invoice['subtotal']:,.2f} + tax "
                    f"${invoice['tax']:,.2f} = ${calc_total:,.2f}, invoice states "
                    f"${invoice['total']:,.2f} (difference "
                    f"${invoice['total'] - calc_total:,.2f})."
                ),
                "details": {
                    "calculated": calc_total,
                    "stated": invoice["total"],
                },
            }
        )
    return findings


def run_all_checks(
    invoice: dict,
    vendors: dict,
    purchase_orders: dict,
    history: list[dict],
) -> list[dict]:
    """Run every check against one invoice. Order is fixed so output is stable."""
    findings: list[dict] = []
    findings += check_math(invoice)
    findings += check_vendor(invoice, vendors.get(invoice["vendor_id"]))
    po = purchase_orders.get(invoice["po_id"]) if invoice.get("po_id") else None
    if invoice.get("po_id") and po is None:
        findings.append(
            {
                "check": "po_match",
                "severity": "medium",
                "evidence": (
                    f"Invoice references PO {invoice['po_id']}, which does not "
                    f"exist in the PO register."
                ),
                "details": {},
            }
        )
    else:
        findings += check_po_match(invoice, po)
    findings += check_duplicate(invoice, history)
    findings += check_split_invoicing(invoice, history)
    return findings


_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}


def decide_verdict(findings: list[dict]) -> dict:
    """Map findings to a verdict. The worst severity wins.

    Verdicts, not execution: nothing here posts, pays, or sends -- the
    verdict is a recommendation for a human.
    """
    worst = max(
        (_SEVERITY_RANK[f["severity"]] for f in findings), default=-1
    )
    if worst >= _SEVERITY_RANK["high"]:
        verdict, action = "REJECT", "Do not pay. Escalate to AP lead."
    elif worst >= _SEVERITY_RANK["medium"]:
        verdict, action = (
            "FLAG_FOR_REVIEW",
            "Hold payment. A human reviewer must clear this invoice.",
        )
    else:
        verdict, action = "APPROVE", "Clear for payment per normal AP flow."
    return {
        "verdict": verdict,
        "recommended_action": action,
        "finding_count": len(findings),
        "high_count": sum(1 for f in findings if f["severity"] == "high"),
        "medium_count": sum(1 for f in findings if f["severity"] == "medium"),
    }
