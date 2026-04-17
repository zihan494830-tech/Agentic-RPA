"""最小 L3 验收：基于现有 block_catalog 的目标驱动编排。"""

from raft.core.query_suggester import synthesize_collaboration_query
from raft.contracts.models import (
    ExperimentConfig,
    GoalPlan,
    GoalPlanStep,
    ScenarioConstraints,
    ScenarioFlowTemplate,
    ScenarioSpec,
    TaskSpec,
    ToolCall,
)
from raft.core.planner.goal_planner import _hydrate_plan_with_initial_state
from raft.orchestrator.runner import Orchestrator
from raft.rpa.mock_rpa import MockRPA


def test_synthesize_collaboration_query_keeps_agent_specific_sections() -> None:
    merged = synthesize_collaboration_query(
        ["Research Proposal", "Literature Review"],
        ["设计调查方案与抽样量表", "综述近五年相关文献与研究空白"],
        fallback_query="关于青年抑郁做研究",
    )
    assert "Research Proposal" in merged
    assert "Literature Review" in merged
    assert "设计调查方案与抽样量表" in merged
    assert "综述近五年相关文献与研究空白" in merged


def test_goal_driven_collaboration_mode_generates_queries_per_agent(monkeypatch) -> None:
    config = ExperimentConfig(
        experiment_id="exp-collab-query",
        scenario="goal-driven",
        task_spec_ids=["t-collab-query"],
        extra={
            "use_llm_query": True,
            "collaboration_mode": True,
            "agents_to_test": ["Research Proposal", "Literature Review"],
        },
    )
    task = TaskSpec(
        task_spec_id="t-collab-query",
        description="测试协作模式每个 agent 独立 query",
        initial_state={"query": "默认问题"},
    )

    def _fake_suggest_queries_for_agents(*args, **kwargs):  # noqa: ANN002, ANN003
        return ["设计调查方案与抽样量表", "综述近五年相关文献与研究空白"]

    monkeypatch.setattr(
        "raft.core.query_suggester.suggest_queries_for_agents",
        _fake_suggest_queries_for_agents,
    )

    orch = Orchestrator(max_steps=5, orchestration_mode="goal_driven", mock_rpa=MockRPA())
    initial_state, _, _ = orch._get_initial_state_for_run(config, task)

    assert initial_state["queries_per_agent"] == [
        "设计调查方案与抽样量表",
        "综述近五年相关文献与研究空白",
    ]
    assert "Research Proposal" in initial_state["query"]
    assert "Literature Review" in initial_state["query"]
    assert "设计调查方案与抽样量表" in initial_state["query"]
    assert "综述近五年相关文献与研究空白" in initial_state["query"]


def test_hydrate_plan_passes_queries_per_agent_to_agent_master_run_flow_once() -> None:
    plan = GoalPlan(
        steps=[
            GoalPlanStep(
                step_id="s0",
                tool_call=ToolCall(tool_name="agent_master_run_flow_once", params={}),
                depends_on=[],
            )
        ],
        source="rule_fallback",
    )
    hydrated = _hydrate_plan_with_initial_state(
        plan,
        {
            "query": "兼容总 query",
            "agents_to_test": ["Research Proposal", "Literature Review"],
            "queries_per_agent": ["设计调查方案与抽样量表", "综述近五年相关文献与研究空白"],
        },
    )
    params = hydrated.steps[0].tool_call.params
    assert params["agents"] == ["Research Proposal", "Literature Review"]
    assert params["queries"] == ["设计调查方案与抽样量表", "综述近五年相关文献与研究空白"]
    assert params["query"] == "兼容总 query"


def test_goal_driven_mode_produces_planned_trajectory() -> None:
    config = ExperimentConfig(
        experiment_id="exp-goal",
        scenario="goal-driven",
        task_spec_ids=["t-goal"],
        extra={
            "orchestration_mode": "goal_driven",
            "block_catalog": [
                {"block_id": "app_ready", "params": {"options": "optional"}},
                {"block_id": "send_query", "params": {"query": "required"}},
                {"block_id": "get_response", "params": {}},
            ],
            "use_llm_planner": False,
        },
    )
    task = TaskSpec(
        task_spec_id="t-goal",
        description="请测试目标驱动编排",
        initial_state={"query": "给我一个市场进入建议"},
    )
    orch = Orchestrator(
        max_steps=5,
        orchestration_mode="goal_driven",
        mock_rpa=MockRPA(),
    )
    result = orch.run_until_done(config, task)

    assert result["orchestration_mode"] == "goal_driven"
    assert result["plan_source"] == "rule_fallback"
    planned = result["planned_tool_calls"]
    assert [x["tool_name"] for x in planned[:3]] == ["app_ready", "send_query", "get_response"]
    traj = result["trajectory"]
    assert [e["step_result"]["tool_calls"][0]["tool_name"] for e in traj] == [
        "app_ready",
        "send_query",
        "get_response",
    ]


def test_goal_driven_replan_after_failure() -> None:
    config = ExperimentConfig(
        experiment_id="exp-goal-replan",
        scenario="goal-driven",
        task_spec_ids=["t-goal-replan"],
        extra={
            "orchestration_mode": "goal_driven",
            "block_catalog": [
                {"block_id": "app_ready", "params": {"options": "optional"}},
                {"block_id": "send_query", "params": {"query": "required"}},
                {"block_id": "get_response", "params": {}},
            ],
            "use_llm_planner": False,
            "replan_on_failure": True,
            "max_replans": 1,
        },
    )
    task = TaskSpec(
        task_spec_id="t-goal-replan",
        description="请测试失败后重规划",
        initial_state={"query": "帮我生成产品策略"},
    )
    # 仅第 1 步失败（send_query 首次失败），重规划后应能恢复执行
    orch = Orchestrator(
        max_steps=6,
        orchestration_mode="goal_driven",
        mock_rpa=MockRPA(fail_steps={1}),
    )
    result = orch.run_until_done(config, task)
    tools = [e["step_result"]["tool_calls"][0]["tool_name"] for e in result["trajectory"]]
    assert result["replan_count"] == 1
    assert tools[0] == "app_ready"
    assert tools[1] == "send_query"
    assert tools[2] == "send_query"  # replan 重试
    assert "get_response" in tools


def test_goal_driven_supports_agent_outside_available_agents() -> None:
    custom_agent = "My Custom Agent Outside List"
    config = ExperimentConfig(
        experiment_id="exp-goal-custom-agent",
        scenario="goal-driven",
        task_spec_ids=["t-goal-custom-agent"],
        extra={
            "orchestration_mode": "goal_driven",
            "agent_under_test": custom_agent,
            "block_catalog": [
                {"block_id": "app_ready", "params": {"options": "optional"}},
                {"block_id": "send_query", "params": {"query": "required"}},
                {"block_id": "get_response", "params": {}},
            ],
            "available_agents": ["Research Proposal", "Market Analysis"],
            "use_llm_planner": False,
        },
    )
    task = TaskSpec(
        task_spec_id="t-goal-custom-agent",
        description="测试自定义待测 Agent",
        initial_state={"query": "hello"},
    )
    orch = Orchestrator(
        max_steps=5,
        orchestration_mode="goal_driven",
        mock_rpa=MockRPA(),
    )
    result = orch.run_until_done(config, task)
    first_call = result["planned_tool_calls"][0]
    assert first_call["tool_name"] == "app_ready"
    assert first_call["params"]["options"]["agent_name"] == custom_agent


def test_goal_driven_multi_agent_plan_expands_to_sequence() -> None:
    """多 Agent 目标：agents_to_test 为列表时，规划器展开为对每个 Agent 的 app_ready→send_query→get_response。"""
    config = ExperimentConfig(
        experiment_id="exp-multi-agent",
        scenario="goal-driven",
        task_spec_ids=["t-multi"],
        extra={
            "orchestration_mode": "goal_driven",
            "agents_to_test": ["Research Proposal", "Market Analysis"],
            "block_catalog": [
                {"block_id": "app_ready", "params": {"options": "optional"}},
                {"block_id": "send_query", "params": {"query": "required"}},
                {"block_id": "get_response", "params": {}},
            ],
            "use_llm_planner": False,
        },
    )
    task = TaskSpec(
        task_spec_id="t-multi",
        description="依次测试 Research Proposal 和 Market Analysis",
        initial_state={"query": "简要介绍 AI"},
    )
    orch = Orchestrator(
        max_steps=10,
        orchestration_mode="goal_driven",
        mock_rpa=MockRPA(),
    )
    result = orch.run_until_done(config, task)
    planned = result["planned_tool_calls"]
    assert len(planned) == 6
    assert planned[0]["tool_name"] == "app_ready"
    assert planned[0]["params"]["options"]["agent_name"] == "Research Proposal"
    assert planned[1]["tool_name"] == "send_query"
    assert planned[2]["tool_name"] == "get_response"
    assert planned[3]["tool_name"] == "app_ready"
    assert planned[3]["params"]["options"]["agent_name"] == "Market Analysis"
    assert planned[4]["tool_name"] == "send_query"
    assert planned[5]["tool_name"] == "get_response"
    traj = result["trajectory"]
    assert len(traj) == 6
    assert [e["step_result"]["tool_calls"][0]["tool_name"] for e in traj] == [
        "app_ready", "send_query", "get_response",
        "app_ready", "send_query", "get_response",
    ]


def test_goal_driven_uses_scenario_spec_as_primary_source() -> None:
    config = ExperimentConfig(
        experiment_id="exp-scenario-spec",
        scenario="poffices-agent",
        scenario_spec=ScenarioSpec(
            id="poffices-agent",
            description="按场景规范执行 Agent Query 测试",
            suggested_agents=["Market Analysis"],
            allowed_blocks=[
                {"block_id": "app_ready", "params": {"options": "optional"}},
                {"block_id": "send_query", "params": {"query": "required"}},
                {"block_id": "get_response", "params": {}},
            ],
            flow_template=ScenarioFlowTemplate(
                template_id="t1",
                description="三段式",
                steps=[
                    {"block_id": "app_ready", "params": {"options": {"agent_name": "$agent_name"}}},
                    {"block_id": "send_query", "params": {"query": "$query"}},
                    {"block_id": "get_response", "params": {}},
                ],
            ),
            constraints=ScenarioConstraints(required_blocks=["app_ready", "send_query", "get_response"]),
        ),
        task_spec_ids=["t-scenario-spec"],
        extra={
            "orchestration_mode": "goal_driven",
            "use_llm_planner": False,
        },
    )
    task = TaskSpec(
        task_spec_id="t-scenario-spec",
        description="",
        initial_state={"query": "解释一下竞争分析"},
    )
    orch = Orchestrator(
        max_steps=5,
        orchestration_mode="goal_driven",
        mock_rpa=MockRPA(),
    )
    result = orch.run_until_done(config, task)

    assert result["task_spec_effective"]["description"] == "按场景规范执行 Agent Query 测试"
    planned = result["planned_tool_calls"]
    assert [x["tool_name"] for x in planned[:3]] == ["app_ready", "send_query", "get_response"]
    assert planned[0]["params"]["options"]["agent_name"] == "Market Analysis"


def test_goal_driven_rejects_agent_not_allowed_by_scenario_spec() -> None:
    config = ExperimentConfig(
        experiment_id="exp-scenario-agent-check",
        scenario="poffices-agent",
        scenario_spec=ScenarioSpec(
            id="poffices-agent",
            allowed_agents=["Research Proposal"],
            allowed_blocks=[
                {"block_id": "app_ready", "params": {"options": "optional"}},
                {"block_id": "send_query", "params": {"query": "required"}},
                {"block_id": "get_response", "params": {}},
            ],
            constraints=ScenarioConstraints(required_blocks=["app_ready", "send_query", "get_response"]),
        ),
        task_spec_ids=["t-scenario-agent-check"],
        extra={
            "orchestration_mode": "goal_driven",
            "agent_under_test": "Market Analysis",
            "use_llm_planner": False,
        },
    )
    task = TaskSpec(
        task_spec_id="t-scenario-agent-check",
        description="测试非法 Agent",
        initial_state={"query": "hello"},
    )
    orch = Orchestrator(
        max_steps=5,
        orchestration_mode="goal_driven",
        mock_rpa=MockRPA(),
    )

    try:
        orch.run_until_done(config, task)
        assert False, "expected scenario validation to reject unsupported agent"
    except ValueError as exc:
        assert "not allowed" in str(exc)


def test_goal_driven_waits_on_human_gate(monkeypatch) -> None:
    config = ExperimentConfig(
        experiment_id="exp-human-gate",
        scenario="goal-driven",
        task_spec_ids=["t-human-gate"],
        extra={"orchestration_mode": "goal_driven", "use_llm_planner": False},
    )
    task = TaskSpec(task_spec_id="t-human-gate", description="人工 gate", initial_state={"query": "hello"})
    plan = GoalPlan(
        steps=[
            GoalPlanStep(
                step_id="s0",
                tool_call=ToolCall(tool_name="send_query", params={"query": "hello"}),
                gate="human",
                risk_level="high",
            )
        ],
        source="rule_fallback",
    )

    monkeypatch.setattr("raft.orchestrator.runner.build_goal_plan", lambda **kwargs: plan)

    orch = Orchestrator(max_steps=3, orchestration_mode="goal_driven", mock_rpa=MockRPA())
    result = orch.run_until_done(config, task)

    assert result["run_status"] == "waiting_human_gate"
    assert result["waiting_human_gate"]["step_id"] == "s0"
    assert len(result["trajectory"]) == 1


def test_goal_driven_human_gate_can_auto_confirm_and_continue(monkeypatch) -> None:
    config = ExperimentConfig(
        experiment_id="exp-human-gate-continue",
        scenario="goal-driven",
        task_spec_ids=["t-human-gate-continue"],
        extra={"orchestration_mode": "goal_driven", "use_llm_planner": False},
    )
    task = TaskSpec(task_spec_id="t-human-gate-continue", description="人工 gate 放行", initial_state={"query": "hello"})
    plan = GoalPlan(
        steps=[
            GoalPlanStep(
                step_id="s0",
                tool_call=ToolCall(tool_name="send_query", params={"query": "hello"}),
                gate="human",
                risk_level="high",
            ),
            GoalPlanStep(
                step_id="s1",
                tool_call=ToolCall(tool_name="get_response", params={}),
                depends_on=["s0"],
            ),
        ],
        source="rule_fallback",
    )

    monkeypatch.setattr("raft.orchestrator.runner.build_goal_plan", lambda **kwargs: plan)

    orch = Orchestrator(
        max_steps=4,
        orchestration_mode="goal_driven",
        mock_rpa=MockRPA(),
        human_confirm_fn=lambda step, execution_result: True,
    )
    result = orch.run_until_done(config, task)

    assert result.get("run_status") != "waiting_human_gate"
    assert [e["step_result"]["tool_calls"][0]["tool_name"] for e in result["trajectory"]] == ["send_query", "get_response"]


def test_goal_driven_recovery_steps_inherit_failed_step_dependencies(monkeypatch) -> None:
    config = ExperimentConfig(
        experiment_id="exp-recovery-deps",
        scenario="goal-driven",
        task_spec_ids=["t-recovery-deps"],
        extra={"orchestration_mode": "goal_driven", "use_llm_planner": False, "replan_on_failure": True, "max_replans": 1},
    )
    task = TaskSpec(task_spec_id="t-recovery-deps", description="恢复依赖", initial_state={"query": "hello"})
    plan = GoalPlan(
        steps=[
            GoalPlanStep(step_id="s0", tool_call=ToolCall(tool_name="app_ready", params={})),
            GoalPlanStep(step_id="s1", tool_call=ToolCall(tool_name="send_query", params={"query": "hello"}), depends_on=["s0"]),
            GoalPlanStep(step_id="s2", tool_call=ToolCall(tool_name="get_response", params={}), depends_on=["s1"]),
        ],
        source="rule_fallback",
    )
    recovery = GoalPlan(
        steps=[
            GoalPlanStep(step_id="s0", tool_call=ToolCall(tool_name="send_query", params={"query": "hello"})),
            GoalPlanStep(step_id="s1", tool_call=ToolCall(tool_name="get_response", params={})),
        ],
        source="replan_rule",
    )
    captured: dict[str, list[GoalPlanStep]] = {}

    from raft.core.planner.dag_scheduler import DAGScheduler as _Scheduler

    class SpyScheduler(_Scheduler):
        def inject_steps(self, steps):
            captured["steps"] = steps
            return super().inject_steps(steps)

    monkeypatch.setattr("raft.orchestrator.runner.build_goal_plan", lambda **kwargs: plan)
    monkeypatch.setattr("raft.orchestrator.runner.build_recovery_plan", lambda **kwargs: recovery)
    monkeypatch.setattr("raft.orchestrator.runner.DAGScheduler", SpyScheduler)

    orch = Orchestrator(
        max_steps=5,
        orchestration_mode="goal_driven",
        mock_rpa=MockRPA(fail_step_ids={"s1"}),
    )
    result = orch.run_until_done(config, task)

    assert result["replan_count"] == 1
    assert captured["steps"][0].depends_on == ["s0"]
    assert captured["steps"][1].depends_on == [captured["steps"][0].step_id]


def test_goal_driven_mock_rpa_can_fail_by_step_id(monkeypatch) -> None:
    config = ExperimentConfig(
        experiment_id="exp-step-id-binding",
        scenario="goal-driven",
        task_spec_ids=["t-step-id-binding"],
        extra={"orchestration_mode": "goal_driven", "use_llm_planner": False, "replan_on_failure": True, "max_replans": 1},
    )
    task = TaskSpec(task_spec_id="t-step-id-binding", description="按 step_id 失败", initial_state={"query": "hello"})
    plan = GoalPlan(
        steps=[
            GoalPlanStep(step_id="custom_a", tool_call=ToolCall(tool_name="app_ready", params={})),
            GoalPlanStep(step_id="custom_b", tool_call=ToolCall(tool_name="send_query", params={"query": "hello"}), depends_on=["custom_a"]),
            GoalPlanStep(step_id="custom_c", tool_call=ToolCall(tool_name="get_response", params={}), depends_on=["custom_b"]),
        ],
        source="rule_fallback",
    )

    monkeypatch.setattr("raft.orchestrator.runner.build_goal_plan", lambda **kwargs: plan)

    orch = Orchestrator(
        max_steps=6,
        orchestration_mode="goal_driven",
        mock_rpa=MockRPA(fail_step_ids={"custom_b"}),
    )
    result = orch.run_until_done(config, task)
    tools = [e["step_result"]["tool_calls"][0]["tool_name"] for e in result["trajectory"]]

    assert result["replan_count"] == 1
    assert tools[:3] == ["app_ready", "send_query", "send_query"]


def test_goal_driven_auto_gate_uses_standard_output_text(monkeypatch) -> None:
    config = ExperimentConfig(
        experiment_id="exp-auto-gate-output",
        scenario="goal-driven",
        task_spec_ids=["t-auto-gate-output"],
        extra={"orchestration_mode": "goal_driven", "use_llm_planner": False},
    )
    task = TaskSpec(task_spec_id="t-auto-gate-output", description="自动 gate", initial_state={"query": "hello"})
    plan = GoalPlan(
        steps=[
            GoalPlanStep(
                step_id="s0",
                tool_call=ToolCall(tool_name="get_response", params={}),
                gate="auto",
                expected_output="non-empty response",
            )
        ],
        source="rule_fallback",
    )

    monkeypatch.setattr("raft.orchestrator.runner.build_goal_plan", lambda **kwargs: plan)

    orch = Orchestrator(max_steps=3, orchestration_mode="goal_driven", mock_rpa=MockRPA())
    result = orch.run_until_done(config, task)

    execution_result = result["trajectory"][0]["step_result"]["execution_results"][0]
    assert result.get("run_status") != "waiting_human_gate"
    assert execution_result["output_text"]
