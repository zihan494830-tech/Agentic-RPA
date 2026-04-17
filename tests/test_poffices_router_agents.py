"""Poffices /plan：agents_planned（仅 app_ready）与 selected_agents（通用抽取）。"""
from raft.api.poffices_router import (
    _coerce_context_agents_to_test,
    _extract_agents_planned,
    _extract_selected_agents_from_planned_calls,
)
from raft.contracts.models import ToolCall


def test_agents_planned_only_app_ready_options() -> None:
    calls = [
        ToolCall(tool_name="invoke_agent", params={"agent": "Market Analysis"}),
        ToolCall(
            tool_name="app_ready",
            params={"options": {"agent_name": "Research Proposal"}},
        ),
    ]
    assert _extract_agents_planned(calls) == ["Research Proposal"]


def test_selected_agents_from_generic_params_ordered_dedup() -> None:
    calls = [
        ToolCall(tool_name="web_search", params={}),
        ToolCall(tool_name="invoke_agent", params={"agent": "Market Analysis"}),
        ToolCall(tool_name="invoke_agent", params={"agent_name": "Competitive Analysis"}),
        ToolCall(tool_name="merge", params={"agent": "Market Analysis"}),
    ]
    assert _extract_selected_agents_from_planned_calls(calls) == [
        "Market Analysis",
        "Competitive Analysis",
    ]


def test_selected_agents_app_ready_and_agents_list() -> None:
    calls = [
        ToolCall(
            tool_name="app_ready",
            params={"options": {"agent_name": "A"}},
        ),
        ToolCall(
            tool_name="multi_agent_linear_block",
            params={"agents": ["B", "C"], "queries": ["q1", "q2"]},
        ),
    ]
    assert _extract_selected_agents_from_planned_calls(calls) == ["A", "B", "C"]


def test_coerce_context_agents_json_string() -> None:
    ctx = _coerce_context_agents_to_test({"agents_to_test": '["Market Analysis", "Research Proposal"]'})
    assert ctx["agents_to_test"] == ["Market Analysis", "Research Proposal"]


def test_coerce_context_agents_plain_name_string() -> None:
    ctx = _coerce_context_agents_to_test({"agents_to_test": "Market Analysis"})
    assert ctx["agents_to_test"] == ["Market Analysis"]


def test_coerce_context_agents_list() -> None:
    ctx = _coerce_context_agents_to_test({"agents_to_test": [" A ", "B"]})
    assert ctx["agents_to_test"] == ["A", "B"]


def test_selected_agents_options_nested() -> None:
    calls = [
        ToolCall(
            tool_name="custom_block",
            params={"options": {"agent": "Research Proposal"}},
        ),
    ]
    assert _extract_selected_agents_from_planned_calls(calls) == ["Research Proposal"]
