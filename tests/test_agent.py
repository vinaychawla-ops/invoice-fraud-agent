"""Structural tests for the ADK Workflow wiring (no API key needed)."""
import subprocess
import sys
from pathlib import Path

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.workflow import Workflow

from fraud_agent import agent as A

EXPECTED_STAGES = ["intake", "extract", "validate", "verdict", "report"]
EXPECTED_TOOLS = {
    "intake": ["load_invoice_case"],
    "extract": ["extract_invoice_fields"],
    "validate": ["run_fraud_checks"],
    "verdict": ["decide_invoice_verdict"],
    "report": ["write_case_report"],
}
REPO_ROOT = Path(__file__).resolve().parent.parent


def _chain_nodes():
    edges = A.root_agent.edges
    assert len(edges) == 1, f"expected a single chain edge, got {len(edges)}"
    return edges[0]


def test_root_agent_is_workflow():
    assert isinstance(A.root_agent, Workflow)
    assert A.root_agent.name == "invoice_fraud_agent"


def test_chain_edges_start_to_report_in_order():
    chain = _chain_nodes()
    assert [n.name for n in chain] == ["__START__"] + EXPECTED_STAGES


def test_each_node_wraps_its_stage_with_tools():
    chain = _chain_nodes()
    for graph_node, stage_name in zip(chain[1:], EXPECTED_STAGES):
        stage = getattr(A, f"{stage_name}_agent")
        assert graph_node.name == stage.name == stage_name
        assert graph_node.model == stage.model
        assert graph_node.description == stage.description
        assert graph_node.instruction == stage.instruction
        assert [t.name for t in graph_node.tools] == EXPECTED_TOOLS[stage_name]


def test_verdict_stage_instruction_mentions_no_execution():
    instruction = A.verdict_agent.instruction.lower()
    assert "do not pay" in instruction or "never" in instruction or \
        "does not pay" in instruction


def test_runner_accepts_workflow_as_root_node():
    Runner(
        node=A.root_agent,
        app_name="invoice_fraud_agent",
        session_service=InMemorySessionService(),
    )


def test_import_raises_no_deprecation_warning():
    proc = subprocess.run(
        [sys.executable, "-W", "error::DeprecationWarning", "-c",
         "import fraud_agent.agent"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stderr


def test_pipeline_and_workflow_stay_in_lockstep(tmp_path):
    from fraud_agent import pipeline

    result = pipeline.run_pipeline(
        "INV-2026-0101", mode="mock", report_dir=str(tmp_path)
    )
    assert list(result["stages"].keys()) == EXPECTED_STAGES
    chain = _chain_nodes()
    for graph_node, stage_name in zip(chain[1:], EXPECTED_STAGES):
        assert graph_node.name == getattr(A, f"{stage_name}_agent").name
