"""ADK agent wiring for the supplier-invoice fraud detector.

A Workflow runs five stages in order, each an LlmAgent wrapped as a
graph node with one job and the FunctionTools it needs:

    intake -> extract -> validate -> verdict -> report

The tools are the same functions used by the deterministic offline
pipeline (pipeline.py), so live and mock runs share all fraud logic.

Verdicts, not execution: the agent APPROVEs, FLAGs for review, or REJECTs
with evidence. It never posts, pays, or sends anything.
"""
from __future__ import annotations

import os

from google.adk.agents import Agent
from google.adk.workflow import Workflow, node, START
from google.adk.tools import FunctionTool

from . import tools as T
from . import ingest as I

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

intake_agent = Agent(
    name="intake",
    model=MODEL,
    description="Loads the supplier invoice case file.",
    instruction=(
        "You are the intake stage of a supplier-invoice fraud audit. "
        "The user gives you an invoice ID like INV-2026-0101. "
        "Call load_invoice_case with that ID, then briefly summarize the "
        "invoice: vendor, total, PO reference, and issue date. "
        "If the user instead gives you a file path to an uploaded invoice "
        "document (PDF, Word, text, or XML), call ingest_invoice_document "
        "with that path first to create the case, then continue with the "
        "returned invoice ID. "
        "If the invoice is unknown, say so and stop."
    ),
    tools=[FunctionTool(T.load_invoice_case), FunctionTool(I.ingest_invoice_document)],
)

extract_agent = Agent(
    name="extract",
    model=MODEL,
    description="Extracts structured fields from the invoice document.",
    instruction=(
        "You are the extraction stage. Call extract_invoice_fields with the "
        "invoice ID from the conversation (use mode='live' so Gemini reads "
        "the raw document text). Then list the extracted fields: invoice "
        "number, vendor, dates, line items, subtotal, tax, total, and the "
        "extraction mode used."
    ),
    tools=[FunctionTool(T.extract_invoice_fields)],
)

validate_agent = Agent(
    name="validate",
    model=MODEL,
    description="Runs the deterministic fraud checks against the invoice.",
    instruction=(
        "You are the validation stage. Call run_fraud_checks with the invoice "
        "ID, then summarize each finding with its check name, severity, and "
        "evidence. Do not invent findings beyond what the tool returns."
    ),
    tools=[FunctionTool(T.run_fraud_checks)],
)

verdict_agent = Agent(
    name="verdict",
    model=MODEL,
    description="Aggregates findings into a verdict with a recommended action.",
    instruction=(
        "You are the verdict stage. Call decide_invoice_verdict with the "
        "invoice ID, then state the verdict (APPROVE, FLAG_FOR_REVIEW, or "
        "REJECT), the recommended human action, and a one-sentence rationale "
        "grounded in the findings. You do not pay, post, or send anything -- "
        "the verdict is a recommendation for a human reviewer."
    ),
    tools=[FunctionTool(T.decide_invoice_verdict)],
)

report_agent = Agent(
    name="report",
    model=MODEL,
    description="Writes the markdown audit case file.",
    instruction=(
        "You are the reporting stage. Call write_case_report with the invoice "
        "ID to save the audit case file, then confirm the report path. "
        "End your response with a line exactly like: "
        "FINAL VERDICT: <APPROVE|FLAG_FOR_REVIEW|REJECT>"
    ),
    tools=[FunctionTool(T.write_case_report)],
)

root_agent = Workflow(
    name="invoice_fraud_agent",
    description=(
        "Audits supplier invoices for fraud: intake, extraction, fraud "
        "checks, verdict, and case report. Verdicts are recommendations; "
        "a human makes the final call."
    ),
    edges=[
        (
            START,
            node(intake_agent),
            node(extract_agent),
            node(validate_agent),
            node(verdict_agent),
            node(report_agent),
        )
    ],
)
