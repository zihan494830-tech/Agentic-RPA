from raft.contracts.models import GoalPlan, GoalPlanStep, ToolCall
from raft.core.planner.goal_planner import _expand_compound_blocks_in_plan


def test_expand_compound_blocks_preserves_parallel_topology() -> None:
    plan = GoalPlan(
        steps=[
            GoalPlanStep(step_id="a", tool_call=ToolCall(tool_name="test_agent_block", params={"agent_name": "A", "query": "q1"})),
            GoalPlanStep(step_id="b", tool_call=ToolCall(tool_name="test_agent_block", params={"agent_name": "B", "query": "q2"})),
            GoalPlanStep(step_id="c", tool_call=ToolCall(tool_name="get_response", params={}), depends_on=["a", "b"]),
        ],
        source="llm",
    )
    compound_blocks = [
        {
            "block_id": "test_agent_block",
            "steps": [
                {"block_id": "app_ready", "params": {"options": {"agent_name": "$agent_name"}}},
                {"block_id": "send_query", "params": {"query": "$query"}},
                {"block_id": "get_response", "params": {}},
            ],
        }
    ]

    expanded = _expand_compound_blocks_in_plan(plan, compound_blocks, atomic_block_ids={"app_ready", "send_query", "get_response"})

    assert [step.tool_call.tool_name for step in expanded.steps] == [
        "app_ready", "send_query", "get_response",
        "app_ready", "send_query", "get_response",
        "get_response",
    ]
    assert expanded.steps[0].depends_on == []
    assert expanded.steps[3].depends_on == []
    assert expanded.steps[1].depends_on == ["s0"]
    assert expanded.steps[4].depends_on == ["s3"]
    assert expanded.steps[6].depends_on == ["s2", "s5"]


def test_expand_iterated_compound_iterations_are_serial() -> None:
    """迭代式复合 Block 展开后，迭代间应串行（iter[i+1][0] 依赖 iter[i][-1]），
    保证在单线程执行器中的执行顺序与 Agent 测试语义一致。"""
    plan = GoalPlan(
        steps=[
            GoalPlanStep(
                step_id="m0",
                tool_call=ToolCall(
                    tool_name="multi_agent_linear_block",
                    params={"agents": ["A", "B"], "queries": ["q1", "q2"]},
                ),
            )
        ],
        source="llm",
    )
    compound_blocks = [
        {
            "block_id": "multi_agent_linear_block",
            "step_template": [
                {"block_id": "app_ready", "params": {"options": {"agent_name": "$agent"}}},
                {"block_id": "send_query", "params": {"query": "$query"}},
                {"block_id": "get_response", "params": {}},
            ],
            "iterate": {"agent": "agents", "query": "queries"},
        }
    ]

    expanded = _expand_compound_blocks_in_plan(plan, compound_blocks, atomic_block_ids={"app_ready", "send_query", "get_response"})

    assert [step.tool_call.tool_name for step in expanded.steps] == [
        "app_ready", "send_query", "get_response",
        "app_ready", "send_query", "get_response",
    ]
    # 第一迭代内部线性依赖
    assert expanded.steps[0].depends_on == []
    assert expanded.steps[1].depends_on == ["s0"]
    assert expanded.steps[2].depends_on == ["s1"]
    # 第二迭代第一步依赖第一迭代最后一步（串行保证）
    assert expanded.steps[3].depends_on == ["s2"]
    assert expanded.steps[4].depends_on == ["s3"]
    assert expanded.steps[5].depends_on == ["s4"]
