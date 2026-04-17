from raft.agents.poffices_agent import PofficesAgent
from raft.agents.factory import resolve_agent_under_test
from raft.contracts.models import ScenarioSpec


def test_state_not_ready_calls_bootstrap() -> None:
    agent = PofficesAgent()
    calls = agent.run({"current_step_index": 3, "state": {}}, task_description="")
    assert len(calls) == 1
    assert calls[0].tool_name == "poffices_bootstrap"


def test_ready_with_query_calls_query() -> None:
    agent = PofficesAgent()
    ctx = {"current_step_index": 3, "state": {"poffices_ready": True, "query": "hello"}}
    calls = agent.run(ctx, task_description="")
    assert len(calls) == 1
    assert calls[0].tool_name == "poffices_query"
    assert calls[0].params["query"] == "hello"


def test_last_query_success_stops() -> None:
    agent = PofficesAgent()
    ctx = {
        "current_step_index": 3,
        "state": {"poffices_ready": True, "query": "hello"},
        "last_execution_result": {"success": True, "tool_name": "poffices_query"},
    }
    calls = agent.run(ctx, task_description="")
    assert calls == []


def test_last_timeout_retries_query() -> None:
    agent = PofficesAgent()
    ctx = {
        "current_step_index": 3,
        "state": {"poffices_ready": True, "query": "hello"},
        "last_execution_result": {
            "success": False,
            "tool_name": "poffices_query",
            "error_type": "timeout",
        },
    }
    calls = agent.run(ctx, task_description="")
    assert len(calls) == 1
    assert calls[0].tool_name == "poffices_query"


# ---------- default_agent_name ----------


def test_bootstrap_carries_agent_name_when_configured() -> None:
    agent = PofficesAgent(default_agent_name="Market Analysis")
    calls = agent.run({"current_step_index": 0, "state": {}}, task_description="")
    assert len(calls) == 1
    assert calls[0].tool_name == "poffices_bootstrap"
    assert calls[0].params == {"options": {"agent_name": "Market Analysis"}}


def test_bootstrap_empty_params_when_no_agent_name() -> None:
    agent = PofficesAgent()
    calls = agent.run({"current_step_index": 0, "state": {}}, task_description="")
    assert calls[0].params == {}


# ---------- resolve_agent_under_test ----------


def _make_config(extra: dict):
    from types import SimpleNamespace
    return SimpleNamespace(extra=extra)


def test_resolve_cli_overrides_config() -> None:
    config = _make_config({"agent_under_test": "A"})
    assert resolve_agent_under_test(config, cli_agent="B") == "B"


def test_resolve_uses_config_default() -> None:
    config = _make_config({"agent_under_test": "Market Analysis"})
    assert resolve_agent_under_test(config) == "Market Analysis"


def test_resolve_fallback_when_no_config() -> None:
    config = _make_config({})
    assert resolve_agent_under_test(config) == "Research Proposal"


def test_resolve_uses_scenario_suggested_agent_when_present() -> None:
    config = _make_config({})
    config.scenario_spec = ScenarioSpec(
        id="poffices-agent",
        suggested_agents=["Market Analysis"],
    )
    assert resolve_agent_under_test(config) == "Market Analysis"


def test_resolve_cli_accepts_any_name() -> None:
    """CLI 传入任意名称均可通过，不校验名单。"""
    config = _make_config({"agent_under_test": "A"})
    assert resolve_agent_under_test(config, cli_agent="完全不存在的 Agent") == "完全不存在的 Agent"
