"""Goal Interpreter 单测。"""
import pytest

from raft.core.goal_interpreter import GoalIntent, interpret_goal, _parse_llm_response


def test_parse_llm_response_json_block() -> None:
    text = '```json\n{"agents": ["Research Proposal"], "topic": "openclaw", "flow_type": "single_agent"}\n```'
    out = _parse_llm_response(text)
    assert out is not None
    assert out.get("agents") == ["Research Proposal"]
    assert out.get("topic") == "openclaw"


def test_parse_llm_response_bare_json() -> None:
    text = '{"agents": ["A", "B"], "topic": "x", "flow_type": "multi_agent_linear"}'
    out = _parse_llm_response(text)
    assert out is not None
    assert out.get("agents") == ["A", "B"]


def test_goal_intent_to_extra_overrides() -> None:
    intent = GoalIntent(
        agents=["Research Proposal", "Market Analysis"],
        topic="openclaw",
        flow_type="multi_agent_linear",
        scenario_id="poffices-agent",
        raw_goal="用三个 agent 写 openclaw 报告",
        office_intent="research",
        count=3,
    )
    overrides = intent.to_extra_overrides()
    assert overrides["goal"] == "用三个 agent 写 openclaw 报告"
    assert overrides["agents_to_test"] == ["Research Proposal", "Market Analysis"]
    assert overrides["topic"] == "openclaw"
    assert intent.office_intent == "research"
    assert intent.count == 3


def test_goal_intent_collaboration_mode_in_overrides() -> None:
    intent = GoalIntent(
        agents=["A", "B", "C"],
        topic="风险分析",
        flow_type="multi_agent_linear",
        raw_goal="用三个 agent 写一份报告",
        collaboration_mode=True,
    )
    overrides = intent.to_extra_overrides()
    assert overrides.get("collaboration_mode") is True


def test_goal_intent_runs_in_overrides() -> None:
    intent = GoalIntent(
        agents=["A"],
        raw_goal="跑 3 轮验证",
        runs=3,
    )
    overrides = intent.to_extra_overrides()
    assert overrides.get("runs") == 3


def test_goal_intent_runs_per_agent_in_overrides() -> None:
    """多 Agent 时 runs_per_agent 写入 extra。"""
    intent = GoalIntent(
        agents=["A", "B"],
        raw_goal="两个 agent 每个跑两轮",
        runs=1,
        runs_per_agent=2,
    )
    overrides = intent.to_extra_overrides()
    assert overrides.get("runs_per_agent") == 2


def test_interpret_goal_fallback_no_llm(monkeypatch) -> None:
    """无 LLM 时返回 fallback。
    用 monkeypatch 清除所有 LLM API Key 环境变量，防止 poffices_bootstrap 的
    模块级 load_dotenv() 把真实 key 注入 os.environ 导致本测试顺序敏感。
    """
    for var in ("OPENAI_API_KEY", "XAI_API_KEY", "AZURE_OPENAI_API_KEY", "SILICONFLOW_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    intent = interpret_goal("用三个 agent 写 openclaw 报告", api_key="")
    assert intent.raw_goal == "用三个 agent 写 openclaw 报告"
    assert intent.scenario_id == "poffices-agent"
    assert intent.agents == []


def test_fallback_agents_exist() -> None:
    """_FALLBACK_AGENTS 非空，供无 scenario 时兜底。"""
    from raft.core.goal_interpreter import _FALLBACK_AGENTS
    assert len(_FALLBACK_AGENTS) > 0
    assert "Research Proposal" in _FALLBACK_AGENTS
