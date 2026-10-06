# AGENTS.md — invoice-fraud-agent

## What this repo is
Supplier-invoice fraud detection on Google ADK (Python). Deterministic offline core (`checks.py`, `pipeline.py`) + ADK `Workflow` orchestration (`agent.py`) sharing the same tool functions (`tools.py`). Verdicts, not execution: APPROVE / FLAG_FOR_REVIEW / REJECT with evidence; nothing ever posts or pays.

## Build / test commands
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m py_compile $(find fraud_agent tests -name '*.py')  # syntax check
pytest -q                                                    # full suite, must be green
python -m fraud_agent.demo                                   # mock demo, no key needed
python -m fraud_agent.demo --file path/to/invoice.pdf        # ingest + audit one document
GOOGLE_API_KEY=... python -m fraud_agent.demo --live         # live ADK + Gemini demo
```

## Conventions
- `checks.py`: pure functions only — `(invoice, context) -> list[findings]`. No I/O, no network, no LLM. A finding is `{"check", "severity", "evidence", "details"}` with severity in `high|medium|low`.
- `tools.py`: module-level, annotated, docstringed functions returning JSON-serializable dicts — ADK builds `FunctionTool` schemas from them. Keep signatures stable; `agent.py` depends on them.
- `config.py`: all policy thresholds live here. Never hardcode a threshold in a check.
- `pipeline.py` and `agent.py` must stay in lockstep: same five stages (intake → extract → validate → verdict → report), same tool functions. If you add a stage, add it to both. `agent.py` wraps each stage `LlmAgent` with `workflow.node()` and chains them as a `Workflow` edge `(START, intake, extract, validate, verdict, report)`; stage definitions themselves stay plain agents so they remain readable and testable.
- The live demo constructs the runner as `Runner(node=root_agent, app_name="invoice_fraud_agent", session_service=...)` — `Runner` accepts a root node, not an agent.
- Sample data: `fraud_agent/data/*.json`. Labeled invoices carry `expected_verdict`, which only tests/demo may read — never the pipeline.
- `ingest.py`: the document-upload input path. Format handlers (`_pdf_text`, `_docx_text`, `_txt_text`, XML via stdlib) extract raw text; `parse_invoice_text` (deterministic mock) and `parse_invoice_xml` (schema-direct) produce the same fields shape as `tools._live_extract`. Every failure returns `{"error": ...}`, never raises. New cases get `UPLOAD-####` ids, are appended to the in-memory `_data()` cache, and a JSON artifact is persisted under `fraud_agent/data/uploads/` (gitignored; override with `INVOICE_FRAUD_UPLOADS_DIR` in tests). Vendor names resolve case-insensitively to the master, else a stable `V-EXT-<slug>` id so the unknown-vendor check fires. The ADK tool function `ingest_invoice_document` lives in `ingest.py` (not `tools.py`) so the import flows one way (`ingest` → `tools`) — it reuses `tools._live_extract` instead of duplicating the prompt. New deps for ingestion: `pypdf`, `python-docx` (in requirements.txt).
- Mock-first: every code path must work with no API key. Live Gemini paths degrade to mock with a clear note, never crash.
- Reports go to `fraud_agent/reports/` (gitignored); tests write to `tmp_path`.
- No secrets in code, logs, or commits. `.env` is gitignored; only `.env.example` is committed.

## Design notes
- Split-invoice detection looks at the full invoice history (including paid/historical invoices), not just the batch being audited.
- Duplicate detection keys on (vendor, invoice number) with a fallback on (vendor, amount, date).
- `decide_verdict`: worst severity wins — high → REJECT, medium → FLAG_FOR_REVIEW, clean → APPROVE.
- The `--live` demo sets a bracket-free `NO_PROXY`/`no_proxy` before creating the Gemini client (sandbox httpx quirk — see workspace AGENTS.md).
