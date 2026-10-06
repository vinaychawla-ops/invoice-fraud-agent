"""Demo runner for the invoice fraud agent.

Mock mode (default): runs the deterministic pipeline over every labeled
sample invoice -- no API key, no network.

    python -m fraud_agent.demo

Live mode: drives the real ADK Workflow with Gemini extraction.
Requires GOOGLE_API_KEY.

    GOOGLE_API_KEY=... python -m fraud_agent.demo --live

File mode: ingest one invoice document (PDF, Word, text, or XML) as a
new case and audit it -- mock parsing by default, live with --live.

    python -m fraud_agent.demo --file path/to/invoice.pdf
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys

from . import tools
from .pipeline import run_pipeline


def _print_row(invoice_id, vendor, total, verdict, expected=None, finding=""):
    mark = ""
    if expected:
        mark = "PASS" if verdict == expected else "FAIL"
    print(
        f"{invoice_id:14} {str(vendor)[:24]:24} ${total:>10,.2f}  "
        f"{verdict:15} {mark:4} {finding}"
    )


def run_mock() -> int:
    print("invoice-fraud-agent demo -- MOCK mode (deterministic, no API key)\n")
    print(f"{'invoice':14} {'vendor':24} {'total':>11}  {'verdict':15} {'chk':4} top finding")
    print("-" * 120)
    counts = {"APPROVE": 0, "FLAG_FOR_REVIEW": 0, "REJECT": 0}
    failures = []
    invoices, _, _ = tools._data()
    by_id = {i["invoice_id"]: i for i in invoices}
    for invoice_id in tools.labeled_invoice_ids():
        result = run_pipeline(invoice_id, mode="mock")
        verdict = result["verdict"]
        counts[verdict] += 1
        expected = by_id[invoice_id].get("expected_verdict")
        top = result["findings"][0]["evidence"] if result["findings"] else "-"
        top = (top[:64] + "...") if len(top) > 64 else top
        _print_row(invoice_id, result["vendor_name"], result["total"], verdict, expected, top)
        if expected and verdict != expected:
            failures.append(invoice_id)
    print("-" * 120)
    print(
        f"summary: {counts['APPROVE']} approved, "
        f"{counts['FLAG_FOR_REVIEW']} flagged for review, "
        f"{counts['REJECT']} rejected"
    )
    print(f"reports written to: {tools.REPORT_DIR}")
    if failures:
        print(f"VERDICT MISMATCHES: {', '.join(failures)}")
        return 1
    print("all labeled verdicts match expectations")
    return 0


def _fix_proxy_env():
    # httpx-based clients (google-genai) crash on bracketed no_proxy values
    # like "[::1]" in this sandbox; use a bracket-free value instead.
    os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"
    os.environ["no_proxy"] = "localhost,127.0.0.1,::1"


async def _audit_one_live(runner, session_service, invoice_id: str) -> str:
    from google.genai import types

    session = await session_service.create_session(
        app_name="invoice_fraud_agent", user_id="demo"
    )
    text_out = []
    async for event in runner.run_async(
        user_id="demo",
        session_id=session.id,
        new_message=types.Content(
            role="user",
            parts=[types.Part(text=f"Audit supplier invoice {invoice_id} for fraud.")],
        ),
    ):
        content = getattr(event, "content", None)
        if content and getattr(content, "parts", None):
            for part in content.parts:
                if getattr(part, "text", None):
                    text_out.append(part.text)
    full = "\n".join(text_out)
    match = re.search(r"FINAL VERDICT:\s*(APPROVE|FLAG_FOR_REVIEW|REJECT)", full)
    return match.group(1) if match else "UNKNOWN"


def run_live() -> int:
    if not os.environ.get("GOOGLE_API_KEY"):
        print("error: --live requires the GOOGLE_API_KEY environment variable.", file=sys.stderr)
        return 2
    _fix_proxy_env()
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    from .agent import root_agent

    print("invoice-fraud-agent demo -- LIVE mode (ADK Workflow + Gemini)\n")
    session_service = InMemorySessionService()
    runner = Runner(
        node=root_agent,
        app_name="invoice_fraud_agent",
        session_service=session_service,
    )

    async def _all():
        results = []
        for invoice_id in tools.labeled_invoice_ids():
            print(f"auditing {invoice_id} ...", flush=True)
            verdict = await _audit_one_live(runner, session_service, invoice_id)
            results.append((invoice_id, verdict))
            print(f"  -> {verdict}")
        return results

    results = asyncio.run(_all())
    print("\nsummary:")
    for invoice_id, verdict in results:
        print(f"  {invoice_id:14} {verdict}")
    return 0


def run_file(path: str, live: bool = False) -> int:
    """Ingest one invoice document and audit it as a new case."""
    from .ingest import ingest_invoice_document

    if live:
        if not os.environ.get("GOOGLE_API_KEY"):
            print("error: --file --live requires the GOOGLE_API_KEY environment variable.",
                  file=sys.stderr)
            return 2
        _fix_proxy_env()
    mode = "live" if live else "mock"
    print(f"invoice-fraud-agent demo -- FILE mode ({mode} parsing)\n")
    print(f"ingesting {path} ...")
    result = ingest_invoice_document(path, mode=mode)
    if "error" in result:
        print(f"ingestion failed: {result['error']}", file=sys.stderr)
        return 1
    invoice_id = result["invoice_id"]
    print(
        f"ingested as {invoice_id} | vendor: {result['vendor_name']} "
        f"({result['vendor_id']}) | total: ${result['total']:,.2f} "
        f"| parse: {result['extraction_mode']}\n"
    )
    print(f"{'invoice':14} {'vendor':24} {'total':>11}  {'verdict':15} top finding")
    print("-" * 120)
    res = run_pipeline(invoice_id, mode=mode)
    if "error" in res:
        print(f"pipeline failed: {res['error']}", file=sys.stderr)
        return 1
    top = res["findings"][0]["evidence"] if res["findings"] else "-"
    top = (top[:64] + "...") if len(top) > 64 else top
    _print_row(invoice_id, res["vendor_name"], res["total"], res["verdict"], finding=top)
    print("-" * 120)
    print(f"verdict: {res['verdict']} -- {res['recommended_action']}")
    print(f"report written to: {res['report_path']}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="invoice-fraud-agent demo")
    parser.add_argument("--live", action="store_true",
                        help="run the real ADK agent with Gemini (needs GOOGLE_API_KEY)")
    parser.add_argument("--file", metavar="PATH",
                        help="ingest an invoice document (PDF/DOCX/TXT/XML) as a new "
                             "case and audit it")
    args = parser.parse_args(argv)
    if args.file:
        return run_file(args.file, live=args.live)
    return run_live() if args.live else run_mock()


if __name__ == "__main__":
    sys.exit(main())
