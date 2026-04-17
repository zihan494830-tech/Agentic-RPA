from raft.contracts.models import TaskSpec
from raft.core.query_suggester import (
    _build_prompt,
    _build_prompt_multi_agent,
    _build_prompt_with_performance,
)


def _task() -> TaskSpec:
    return TaskSpec(
        task_spec_id="t-query",
        description="测试一个带场景规范的 query 生成",
        initial_state={"query": "默认 query"},
    )


def test_build_prompt_includes_scenario_context() -> None:
    prompt = _build_prompt(
        _task(),
        "Poffices 的 Research Proposal Agent",
        scenario_context="场景 ID：poffices-agent\n允许 Agent：Research Proposal\n允许 Block：app_ready, send_query, get_response",
    )
    assert "场景规范" in prompt
    assert "允许 Agent：Research Proposal" in prompt


def test_build_prompt_with_performance_includes_scenario_context() -> None:
    prompt = _build_prompt_with_performance(
        _task(),
        "Poffices 的 Research Proposal Agent",
        previous_rounds=[{"query": "介绍 AI", "success": True, "step_count": 3}],
        strategy="auto",
        scenario_context="场景 ID：poffices-agent\n流程模板步骤：app_ready -> send_query -> get_response",
    )
    assert "场景规范" in prompt
    assert "流程模板步骤：app_ready -> send_query -> get_response" in prompt


def test_build_prompt_multi_agent_includes_scenario_context() -> None:
    prompt = _build_prompt_multi_agent(
        _task(),
        "Poffices 多 Agent",
        ["Research Proposal", "Market Analysis"],
        scenario_context="允许 Agent：Research Proposal, Market Analysis",
    )
    assert "场景规范" in prompt
    assert "允许 Agent：Research Proposal, Market Analysis" in prompt
