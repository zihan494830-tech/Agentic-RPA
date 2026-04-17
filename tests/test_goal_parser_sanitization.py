import json

from raft.core.planner.goal_parser import parse_goal


def test_goal_parser_sanitizes_execution_constraints_when_goal_has_no_hard_constraints(monkeypatch) -> None:
    """
    当 goal 中没有明确出现“只使用/不得使用/最多调用/不超过 … 步”等硬约束表述时，
    即使 LLM 错把示例约束“默认填入”，也不应写入 intent.execution_constraints。
    """
    fake_intent_json = {
        "execution_constraints": [
            "只使用 Agent: research proposal",
            "只使用 Agent: market analysis",
            "最多调用 2 步",
        ],
        "content_intent": ["测试 research proposal 和 market analysis 两个 agent，每个 agent 一轮"],
        "quality_requirements": [],
        "scope_constraints": [],
        "temporal_relationships": [],
        "ambiguities": ["测试语义不明"],
        "confidence": 0.8,
    }
    fake_content = json.dumps(fake_intent_json, ensure_ascii=False)

    class _FakeMessage:
        def __init__(self, content: str) -> None:
            self.content = content

    class _FakeChoice:
        def __init__(self, content: str) -> None:
            self.message = _FakeMessage(content)

    class _FakeResp:
        def __init__(self, content: str) -> None:
            self.choices = [_FakeChoice(content)]

    monkeypatch.setattr("raft.core.planner.goal_parser.OpenAI", object())
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    monkeypatch.setattr(
        "raft.core.planner.goal_parser.chat_completion_with_retry",
        lambda **kwargs: _FakeResp(fake_content),
    )

    goal = "测试research proposal和market analysis这两个agent，每个agent一轮"
    intent = parse_goal(goal)

    assert intent.execution_constraints == []

