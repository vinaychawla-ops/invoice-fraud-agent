"""Structural tests for the ADK agent wiring (no API key needed)."""
from google.adk.agents import SequentialAgent

from fraud_agent import agent as A


def test_root_agent_is_sequential():
    assert isinstance(A.root_agent, SequentialAgent)
    assert A.root_agent.name == "invoice_fraud_agent"


def test_five_stages_in_order():
    names = [s.name for s in A.root_agent.sub_agents]
    assert names == ["intake", "extract", "validate", "verdict", "report"]


def test_each_stage_has_model_and_tools():
    for stage in A.root_agent.sub_agents:
        assert stage.model, f"{stage.name} has no model configured"
        assert stage.tools, f"{stage.name} has no tools"


def test_expected_tools_wired():
    wired = {}
    for stage in A.root_agent.sub_agents:
        for tool in stage.tools:
            wired.setdefault(tool.name, []).append(stage.name)
    assert wired["load_invoice_case"] == ["intake"]
    assert wired["extract_invoice_fields"] == ["extract"]
    assert wired["run_fraud_checks"] == ["validate"]
    assert wired["decide_invoice_verdict"] == ["verdict"]
    assert wired["write_case_report"] == ["report"]


def test_verdict_stage_instruction_mentions_no_execution():
    instruction = A.verdict_agent.instruction.lower()
    assert "do not pay" in instruction or "never" in instruction or \
        "does not pay" in instruction
