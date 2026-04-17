"""报告模板：单轮视图与多轮视图分支验证。"""

from scripts.build_flow_report import build_multi_flow_report


def _mock_result(run_id: str, *, include_llm_judge: bool = True) -> dict:
    metrics = {
        "success": True,
        "step_count": 3,
        "details": {"execution_success_rate": 1.0, "retry_count": 0},
    }
    if include_llm_judge:
        metrics["llm_judge"] = {
            "decision_quality": 0.9,
            "reasoning_coherence": 0.85,
            "tool_proficiency": 0.88,
            "output_quality": 0.82,
            "safety_alignment": 1.0,
            "interpretability": 0.9,
            "output_comment": "表现良好。",
        }
    return {
        "run_id": run_id,
        "steps_run": 3,
        "metrics": metrics,
        "trajectory": [
            {
                "step_index": 0,
                "step_result": {
                    "agent_input_snapshot": {"state": {"query": "test query"}},
                    "tool_calls": [{"tool_name": "app_ready", "params": {}}],
                    "execution_results": [{"tool_name": "app_ready", "success": True, "ui_state_delta": {}}],
                },
            },
            {
                "step_index": 1,
                "step_result": {
                    "tool_calls": [{"tool_name": "send_query", "params": {"query": "test query"}}],
                    "execution_results": [{"tool_name": "send_query", "success": True, "ui_state_delta": {}}],
                },
            },
            {
                "step_index": 2,
                "step_result": {
                    "tool_calls": [{"tool_name": "get_response", "params": {}}],
                    "execution_results": [
                        {
                            "tool_name": "get_response",
                            "success": True,
                            "ui_state_delta": {"poffices_response": "# Title\n内容正文\n## References\n- a"},
                        }
                    ],
                },
            },
        ],
    }


def test_single_round_report_hides_multi_summary_and_llm_brief() -> None:
    html = build_multi_flow_report(
        [_mock_result("r1")],
        {"experiment_id": "exp1", "scenario": "s1", "task_spec_ids": ["t1"]},
        {"task_spec_id": "t1", "description": "d1", "initial_state": {}},
        llm_summary="1. 本轮总结内容。",
    )
    assert "多轮汇总" not in html
    assert "本轮明细" in html
    assert "本轮 LLM 简要分析" not in html
    assert "LLM 本轮总结" in html


def test_multi_round_report_keeps_multi_sections() -> None:
    html = build_multi_flow_report(
        [_mock_result("r1"), _mock_result("r2")],
        {"experiment_id": "exp1", "scenario": "s1", "task_spec_ids": ["t1"]},
        {"task_spec_id": "t1", "description": "d1", "initial_state": {}},
        llm_summary="1. 多轮总结内容。",
    )
    assert "多轮汇总" in html
    assert "多轮明细" in html
    assert "本轮 LLM 简要分析" in html
    assert "LLM 多轮分析总结" in html


def test_report_shows_scenario_spec_summary() -> None:
    html = build_multi_flow_report(
        [_mock_result("r1")],
        {
            "experiment_id": "exp1",
            "scenario": "poffices-agent",
            "scenario_spec_path": "scenarios/poffices-agent.json",
            "scenario_spec": {
                "id": "poffices-agent",
                "description": "测试 Poffices Agent Query 能力",
                "allowed_agents": ["Research Proposal", "Market Analysis"],
                "allowed_blocks": [
                    {"block_id": "app_ready"},
                    {"block_id": "send_query"},
                    {"block_id": "get_response"},
                ],
                "flow_template": {
                    "description": "默认三段式流程",
                    "steps": [
                        {"block_id": "app_ready"},
                        {"block_id": "send_query"},
                        {"block_id": "get_response"},
                    ],
                },
                "constraints": {
                    "required_blocks": ["app_ready", "send_query", "get_response"],
                    "forbidden_blocks": ["poffices_query"],
                },
            },
            "task_spec_ids": ["t1"],
        },
        {"task_spec_id": "t1", "description": "d1", "initial_state": {}},
    )
    assert "场景规范（ScenarioSpec）" in html
    assert "poffices-agent" in html
    # 红框内容已不展示；改为展示本轮 RPA 工作流程（来自 trajectory）
    assert "RPA 工作流程" in html
    assert "app_ready" in html and "send_query" in html and "get_response" in html


def test_report_shows_collaboration_assignments_when_queries_per_agent_present() -> None:
    result = {
        "run_id": "r-collab",
        "steps_run": 3,
        "metrics": {"success": True, "step_count": 3, "details": {"execution_success_rate": 1.0}},
        "trajectory": [
            {
                "step_index": 0,
                "step_result": {
                    "agent_input_snapshot": {
                        "state": {
                            "query": "兼容协作总 query",
                            "agents_to_test": ["Research Proposal", "Literature Review"],
                            "queries_per_agent": ["设计调查方案与抽样量表", "综述近五年相关文献与研究空白"],
                        }
                    },
                    "tool_calls": [{"tool_name": "agent_master_run_flow_once", "params": {"query": "兼容协作总 query"}}],
                    "execution_results": [{"tool_name": "agent_master_run_flow_once", "success": True, "ui_state_delta": {"poffices_response": "ok"}}],
                },
            }
        ],
    }
    html = build_multi_flow_report(
        [result],
        {"experiment_id": "exp1", "scenario": "s1", "task_spec_ids": ["t1"]},
        {"task_spec_id": "t1", "description": "d1", "initial_state": {}},
    )
    assert "协作分工（per-agent queries）" in html
    assert "Research Proposal" in html
    assert "Literature Review" in html
    assert "设计调查方案与抽样量表" in html
    assert "综述近五年相关文献与研究空白" in html


def test_multi_flow_report_contains_no_self_check_copy() -> None:
    """报告不再渲染计划/结果自检块。"""
    result = {
        "run_id": "r1",
        "steps_run": 3,
        "metrics": {"success": True, "step_count": 3, "details": {}},
        "goal_intent": {"raw_goal": "测试", "confidence": 0.9},
        "trajectory": [
            {
                "step_index": 0,
                "step_result": {
                    "agent_input_snapshot": {"state": {"query": "test query"}},
                    "tool_calls": [{"tool_name": "app_ready", "params": {}}],
                    "execution_results": [{"tool_name": "app_ready", "success": True, "ui_state_delta": {}}],
                },
            }
        ],
    }
    html = build_multi_flow_report(
        [result],
        {"experiment_id": "exp1", "scenario": "s1", "task_spec_ids": ["t1"]},
        {"task_spec_id": "t1", "description": "d1", "initial_state": {}},
    )
    assert "计划自检" not in html
    assert "结果自检" not in html
    assert "validate_plan_against_intent" not in html
