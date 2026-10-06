# invoice-fraud-agent

A supplier-invoice fraud detection agent built on [Google ADK](https://google.github.io/adk-docs/) (Python).

Give it an invoice; it runs a five-stage audit pipeline — **intake → extract → validate → verdict → report** — and returns a verdict of **APPROVE**, **FLAG_FOR_REVIEW**, or **REJECT**, with evidence for every finding.

**Verdicts, not execution.** The agent never posts, pays, or sends anything. A REJECT or FLAG means "a human must look at this," not "the money moved."

## The five fraud patterns

| # | Pattern | How it's caught | Severity |
|---|---------|-----------------|----------|
| 1 | Duplicate invoices | Same vendor + invoice number (or same amount + date) already seen | high → REJECT |
| 2 | Split invoicing | ≥2 invoices from one vendor within 30 days, each under the $10,000 approval threshold, summing above it | high → REJECT |
| 3 | Invoice-vs-PO mismatch | Quantity, unit-price (>5% tolerance), or subtotal deviation from the purchase order | medium/high → FLAG/REJECT |
| 4 | Unknown / high-risk vendor | Vendor missing from the vendor master, unapproved, or high risk tier | high/medium → REJECT/FLAG |
| 5 | Math errors | Line totals, subtotal, and grand total don't reconcile | medium → FLAG_FOR_REVIEW |

Severity decides the verdict: any **high** → REJECT, else any **medium** → FLAG_FOR_REVIEW, else APPROVE. Thresholds live in `fraud_agent/config.py`.

## Architecture

```
fraud_agent/
├── data/                 # vendors.json, purchase_orders.json, invoices.json (mock, offline)
├── checks.py             # deterministic fraud checks -- pure functions, no I/O
├── tools.py              # data loading + extraction (mock/live) + ADK tool functions
├── pipeline.py           # deterministic offline driver: intake→extract→validate→verdict→report
├── agent.py              # real ADK Workflow wiring the same tools
└── demo.py               # demo runner (mock default, --live for Gemini)
tests/                    # pytest: checks, pipeline, agent wiring
```

Two paths, one source of truth: `pipeline.py` calls the tool functions directly (mock mode, no key needed), while `agent.py` wraps the *same* functions in ADK `FunctionTool`s inside a `Workflow` chain (`START → intake → extract → validate → verdict → report`). The fraud logic can't diverge between them.

**Orchestration.** `agent.py` keeps the five stage definitions as plain `LlmAgent`s and turns each into a graph node with `workflow.node()`; `root_agent` is the `Workflow` built from the single chain edge. The live demo drives it with `Runner(node=root_agent, app_name="invoice_fraud_agent", session_service=...)` — `Runner` takes a root *node*, not an agent.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # only needed for --live; add your GOOGLE_API_KEY
```

## Run

```bash
# Mock mode: deterministic pipeline over all 8 labeled sample invoices (no key needed)
python -m fraud_agent.demo

# Live mode: drives the real ADK Workflow with Gemini extraction
GOOGLE_API_KEY=... python -m fraud_agent.demo --live

# Tests
pytest -q
```

The demo prints a per-invoice verdict table plus a summary, and writes markdown case files to `fraud_agent/reports/`. In mock mode it also checks every verdict against the labeled expectations.

## Mock vs live

- **Mock** (default): extraction returns the known structured fields deterministically. Everything — tests, demo, CI — runs offline.
- **Live**: `extract_invoice_fields(mode="live")` calls Gemini (`GEMINI_MODEL`, default `gemini-2.5-flash`) on the invoice's raw document text. If the call fails, it falls back to mock extraction and says so. The fraud checks themselves are always deterministic.

## Sample data

`fraud_agent/data/invoices.json` holds 8 labeled invoices (3 clean, 5 fraudulent — one per pattern) plus 2 historical invoices that form the split-invoicing cluster. Labels are in `expected_verdict`; the pipeline never reads them.

## Extending

- New check: add a pure function in `checks.py`, call it from `run_all_checks`, add positive/negative tests in `tests/test_checks.py`.
- New sample: append to `invoices.json` with an `expected_verdict`; the demo and pipeline tests pick it up automatically.
- New policy: edit `fraud_agent/config.py` — no code changes needed.
