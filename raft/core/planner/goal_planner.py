"""Goal-driven planner: 基于目标与 block_catalog 生成计划，并支持失败重规划。"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from raft.contracts.models import GoalPlan, GoalPlanStep, ToolCall
from raft.core.planner.dag_validator import fix_dag, validate_dag
from raft.core.planner.goal_intent import GoalIntent
from raft.core.planner.goal_parser import parse_goal
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore

from raft.core.llm_client import chat_completion_with_retry
from raft.core.llm_providers import resolve_api_key, resolve_base_url, resolve_chat_model, normalize_provider

logger = logging.getLogger(__name__)


def _catalog_to_map(block_catalog: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in block_catalog:
        if not isinstance(item, dict):
            continue
        block_id = item.get("block_id")
        if isinstance(block_id, str) and block_id:
            out[block_id] = item
    return out


def _pick_query(initial_state: dict[str, Any], description: str) -> str:
    q = initial_state.get("query")
    if isinstance(q, str) and q.strip():
        return q.strip()
    if description.strip():
        return description.strip()
    return "请基于当前任务给出可执行建议。"


def _resolve_template_value(
    value: Any,
    *,
    query: str = "",
    agent_name: str | None = None,
    agent: str | None = None,
    agents: list[str] | None = None,
) -> Any:
    if isinstance(value, str):
        if value == "$query":
            return query
        if value in ("$agent_name", "$agent"):
            return agent_name or agent or ""
        if value == "$agents":
            return agents if isinstance(agents, list) else []
        return value
    if isinstance(value, dict):
        return {
            str(k): _resolve_template_value(v, query=query, agent_name=agent_name, agent=agent, agents=agents)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            _resolve_template_value(item, query=query, agent_name=agent_name, agent=agent, agents=agents)
            for item in value
        ]
    return value


def _build_plan_from_template(
    *,
    flow_template: dict[str, Any],
    initial_state: dict[str, Any],
    task_description: str,
) -> GoalPlan | None:
    steps = flow_template.get("steps")
    if not isinstance(steps, list) or not steps:
        return None

    query = _pick_query(initial_state, task_description)
    agent_name = initial_state.get("agent_name")
    if not isinstance(agent_name, str) or not agent_name.strip():
        agent_name = None

    calls: list[ToolCall] = []
    for item in steps:
        if not isinstance(item, dict):
            continue
        tool_name = item.get("block_id") or item.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name.strip():
            continue
        params = item.get("params")
        if isinstance(params, dict):
            resolved_params = _resolve_template_value(params, query=query, agent_name=agent_name)
        else:
            resolved_params = {}
        if tool_name == "send_query" and "query" not in resolved_params:
            resolved_params["query"] = query
        if tool_name == "app_ready" and agent_name and "options" not in resolved_params:
            resolved_params["options"] = {"agent_name": agent_name}
        calls.append(ToolCall(tool_name=tool_name, params=resolved_params))

    if not calls:
        return None
    return _make_linear_plan(
        calls,
        source="rule_fallback",
        reason=flow_template.get("description") or "scenario_flow_template",
    )


# 已知工具的 gate / risk_level 默认映射。
# gate="auto"：执行后自动校验 success 与 expected_output；
# gate="none"：无校验（读操作、初始化类 block 一般不需要 gate）。
_TOOL_GATE_MAP: dict[str, tuple[str, str]] = {
    "send_query":                    ("auto", "medium"),
    "poffices_query":                ("auto", "medium"),
    "get_response":                  ("auto", "medium"),
    "wait_output_complete":          ("auto", "low"),
    "app_ready":                     ("none", "low"),
    "poffices_bootstrap":            ("none", "low"),
    "discovery_bootstrap":           ("none", "low"),
    "refresh_page":                  ("none", "low"),
    "agent_master_run_flow_once":    ("auto", "medium"),
    "agent_master_select_agents_for_flow": ("none", "low"),
}


def _make_linear_plan(calls: list[ToolCall], *, source: str, reason: str | None = None) -> GoalPlan:
    """将 ToolCall 列表转为线性 GoalPlan；对已知工具自动填充 gate/risk_level，
    规则规划不再全部依赖 gate='none' 默认值。"""
    steps: list[GoalPlanStep] = []
    for idx, tc in enumerate(calls):
        step_id = f"s{idx}"
        depends_on = [f"s{idx - 1}"] if idx > 0 else []
        gate, risk_level = _TOOL_GATE_MAP.get(tc.tool_name, ("none", "low"))
        steps.append(
            GoalPlanStep(
                step_id=step_id,
                tool_call=tc,
                depends_on=depends_on,
                gate=gate,
                risk_level=risk_level,
            )
        )
    return GoalPlan(steps=steps, source=source, reason=reason)


def _rule_plan(
    *,
    block_catalog: list[dict[str, Any]],
    initial_state: dict[str, Any],
    task_description: str,
    flow_template: dict[str, Any] | None = None,
) -> GoalPlan:
    """无 LLM 时的规则兜底：优先走通用三段式，其次兼容旧版 poffices 块；支持多 Agent 展开。"""
    block_map = _catalog_to_map(block_catalog)
    query = _pick_query(initial_state, task_description)

    # 多 Agent 目标：agents_to_test 为列表且长度 > 1 时，对每个 agent 展开 (app_ready, send_query, get_response)
    # 优先于 flow_template 检查，避免单 Agent 模板覆盖多 Agent 计划
    agents_to_test = initial_state.get("agents_to_test")
    if (
        isinstance(agents_to_test, list)
        and len(agents_to_test) > 1
        and all(k in block_map for k in ("app_ready", "send_query", "get_response"))
    ):
        queries_per_agent = initial_state.get("queries_per_agent")
        if not isinstance(queries_per_agent, list) or len(queries_per_agent) != len(agents_to_test):
            queries_per_agent = None
        calls: list[ToolCall] = []
        for i, agent in enumerate(agents_to_test):
            if not isinstance(agent, str) or not agent.strip():
                continue
            query = (queries_per_agent[i] if queries_per_agent and i < len(queries_per_agent) and isinstance(queries_per_agent[i], str) else None) or query
            calls.append(
                ToolCall(
                    tool_name="app_ready",
                    params={"options": {"agent_name": agent.strip()}},
                )
            )
            calls.append(ToolCall(tool_name="send_query", params={"query": query}))
            calls.append(ToolCall(tool_name="get_response", params={}))
        if calls:
            return _make_linear_plan(calls, source="rule_fallback")

    # 单 Agent 时才应用 flow_template（多 Agent 已在上方处理并提前返回）
    if flow_template:
        template_plan = _build_plan_from_template(
            flow_template=flow_template,
            initial_state=initial_state,
            task_description=task_description,
        )
        if template_plan:
            return template_plan

    if all(k in block_map for k in ("app_ready", "send_query", "get_response")):
        app_ready_params: dict[str, Any] = {}
        agent_name = initial_state.get("agent_name")
        if isinstance(agent_name, str) and agent_name.strip():
            app_ready_params = {"options": {"agent_name": agent_name.strip()}}
        return _make_linear_plan([
            ToolCall(tool_name="app_ready", params=app_ready_params),
            ToolCall(tool_name="send_query", params={"query": query}),
            ToolCall(tool_name="get_response", params={}),
        ], source="rule_fallback")

    if all(k in block_map for k in ("poffices_bootstrap", "poffices_query")):
        return _make_linear_plan([
            ToolCall(tool_name="poffices_bootstrap", params={}),
            ToolCall(tool_name="poffices_query", params={"query": query}),
        ], source="rule_fallback")

    # 通用兜底：按 catalog 顺序选前 3 个 block，尽量填 query 参数
    plan: list[ToolCall] = []
    for item in block_catalog[:3]:
        block_id = item.get("block_id")
        if not isinstance(block_id, str) or not block_id:
            continue
        params_schema = item.get("params")
        params: dict[str, Any] = {}
        if isinstance(params_schema, dict) and "query" in params_schema:
            params["query"] = query
        plan.append(ToolCall(tool_name=block_id, params=params))
    return _make_linear_plan(plan, source="rule_fallback")


def _parse_llm_plan(text: str, allowed_blocks: set[str], *, source: str) -> GoalPlan | None:
    text = text.strip()
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
        raw_steps = payload.get("steps")
        if not isinstance(raw_steps, list):
            return None
        plan_steps: list[GoalPlanStep] = []
        for idx, step in enumerate(raw_steps):
            if not isinstance(step, dict):
                continue
            name = step.get("tool_name")
            params = step.get("params") or {}
            if not isinstance(name, str) or name not in allowed_blocks:
                continue
            if not isinstance(params, dict):
                params = {}
            sid = step.get("step_id")
            if not isinstance(sid, str) or not sid.strip():
                sid = f"s{idx}"
            raw_depends = step.get("depends_on")
            depends_on: list[str] = []
            if isinstance(raw_depends, list):
                depends_on = [x for x in raw_depends if isinstance(x, str) and x.strip()]
            # 解析新字段
            expected_output = step.get("expected_output")
            if not isinstance(expected_output, str):
                expected_output = None
            gate = step.get("gate", "none")
            if gate not in ("none", "auto", "human"):
                gate = "none"
            risk_level = step.get("risk_level", "low")
            if risk_level not in ("low", "medium", "high"):
                risk_level = "low"
            plan_steps.append(
                GoalPlanStep(
                    step_id=sid,
                    tool_call=ToolCall(tool_name=name, params=params),
                    depends_on=depends_on,
                    expected_output=expected_output,
                    gate=gate,
                    risk_level=risk_level,
                )
            )
        if not plan_steps:
            return None
        # 相邻重复步骤去重（避免 LLM 输出 discovery_bootstrap→discovery_bootstrap 等）
        deduped: list[GoalPlanStep] = []
        prev_name: str | None = None
        for s in plan_steps:
            name = s.tool_call.tool_name if s.tool_call else ""
            if name and name == prev_name:
                continue
            prev_name = name
            deduped.append(s)
        plan_steps = deduped
        if not plan_steps:
            return None
        # 去重后重新编号 step_id
        for i, s in enumerate(plan_steps):
            s.step_id = f"s{i}"
        # 仅当 LLM 完全未输出任何 depends_on 时才做线性兜底
        # 若 LLM 明确给出了部分依赖（哪怕只有一个步骤有），则尊重 LLM 的 DAG 语义
        any_has_dep = any(s.depends_on for s in plan_steps)
        if not any_has_dep and len(plan_steps) > 1:
            # LLM 完全未给依赖，保守线性化
            for i in range(1, len(plan_steps)):
                plan_steps[i].depends_on = [plan_steps[i - 1].step_id]
        return GoalPlan(steps=plan_steps, source=source)
    except (json.JSONDecodeError, TypeError):
        return None


def _llm_plan(
    *,
    block_catalog: list[dict[str, Any]],
    initial_state: dict[str, Any],
    task_description: str,
    goal: str | None = None,
    intent: GoalIntent | None = None,
    provider: str | None = None,
    model: str | None = None,
    flow_template: dict[str, Any] | None = None,
    constraints: dict[str, Any] | None = None,
    scenario_context: str | None = None,
    block_semantics: str | None = None,
    timing_label: str = "goal_planner",
) -> GoalPlan | None:
    if OpenAI is None:
        return None

    _prov = normalize_provider(provider)
    api_key = resolve_api_key(None, _prov)
    if not api_key:
        return None

    base_url = resolve_base_url(_prov, os.environ.get("OPENAI_API_BASE"))
    llm_model = resolve_chat_model(_prov, model, base_url_override=os.environ.get("OPENAI_API_BASE"))

    try:
        scenario_text = (scenario_context or "").strip()
        system_prompt = (
            "你是 RPA 目标规划器。给定任务目标、初始状态、场景规范与可用 block 列表，"
            "请输出在当前场景约束下的最短可行执行计划。\n"
            "输出必须是合法 JSON，格式如下（每步必须包含所有字段）：\n"
            '{"steps":[{"step_id":"s0","tool_name":"block_id","params":{},'
            '"depends_on":[],"expected_output":"本步产出的一句话描述",'
            '"gate":"none","risk_level":"low"}]}\n'
            "【DAG 依赖规则】\n"
            "- depends_on 表达真实数据依赖：步骤 B 只有在需要步骤 A 的输出时，才在 depends_on 中列出 A；\n"
            "- 若多步之间没有数据依赖（如多路信息检索、多个 Agent 独立运行），depends_on 给空列表 []，以允许并行；\n"
            "- 严禁凭习惯把所有步骤串成链（这会阻止并行加速）；\n"
            "- 严禁产生环形依赖（A 依赖 B、B 依赖 A）。\n"
            "【gate / risk_level 规则】\n"
            "- gate: 'none'=无需审核，'auto'=可自动规则校验，'human'=必须人工确认；\n"
            "- 外部写操作（提交表单、发邮件、转账等）必须设置 gate='human', risk_level='high'；\n"
            "- 纯读操作默认 gate='none', risk_level='low'；\n"
            "- expected_output: 用一句话描述本步完成后的产出（如'获取到 Agent 回复文本'）。\n"
            "不要输出 JSON 之外的任何解释文字。"
        )
        user_parts = [
            f"任务描述: {task_description}",
            f"初始状态: {json.dumps(initial_state, ensure_ascii=False)}",
        ]
        if block_semantics and block_semantics.strip():
            user_parts.append(f"Block 语义（必读，理解每个 block 的用途、副作用与流程归属）:\n{block_semantics.strip()}")
        user_parts.append(f"可用 blocks（block_id 列表）: {json.dumps([b.get('block_id') for b in block_catalog if isinstance(b, dict) and b.get('block_id')], ensure_ascii=False)}")
        user_parts.append(f"流程模板: {json.dumps(flow_template or {}, ensure_ascii=False)}")
        user_parts.append(f"场景约束: {json.dumps(constraints or {}, ensure_ascii=False)}")
        if goal and str(goal).strip():
            user_parts.append(f"用户目标（原文，供参考）: {str(goal).strip()}")
        # 结构化意图（GoalIntent）：将 goal 分解为五个维度，供精确规划
        if intent and not intent.is_empty():
            intent_text = intent.to_planner_context()
            if intent_text.strip():
                user_parts.append(
                    "用户目标（结构化解析，优先级高于原文）:\n" + intent_text
                )
        if scenario_text:
            user_parts.append(f"场景规范: {scenario_text}")
        if initial_state.get("post_discovery_resume"):
            user_parts.append(
                "【会话状态】Discovery 已在同一浏览器会话中完成（Agent Master 已就绪、Office 已展开过）。"
                "主流程请用 app_ready→send_query→get_response（或 test_agent_block）；"
                "禁止 discovery_bootstrap、list_offices、expand_office、list_agents_in_office；禁止 refresh_page。"
            )
        # 流程选择：结合 initial_state 与 goal，推荐复合 block（效率高）或基础 block 组合（灵活）
        agents = initial_state.get("agents_to_test") or []
        n_agents = len(agents) if isinstance(agents, list) else 0
        collab = bool(initial_state.get("collaboration_mode"))
        # 有明确 initial_state 时给出推荐（优先复合 block 以简化计划），仍保留用基础 block 的灵活性
        if n_agents > 1 and collab:
            flow_rule = (
                "initial_state 提示：多 Agent 协作模式。"
                "推荐 agent_master_collaboration_block（或 discovery_bootstrap→agent_master_select→agent_master_run_flow_once），步骤少、语义清晰；"
                "若 goal 有特殊需求，可用基础 block 组合。"
            )
        elif n_agents > 1:
            flow_rule = (
                "initial_state 提示：多 Agent 线性（依次测试多个 Agent）。"
                "推荐 multi_agent_linear_block，一步覆盖多个 Agent，比逐个展开 app_ready→send_query→get_response 更简洁；"
                "若 goal 需自定义顺序或穿插其他步骤，可用基础 block 组合。"
            )
        elif n_agents == 1:
            flow_rule = (
                "initial_state 提示：单 Agent。"
                "推荐 test_agent_block（或 app_ready→send_query→get_response）；"
                "若 goal 需自定义流程，可用基础 block 组合。"
            )
        else:
            flow_rule = (
                "根据 goal 分析选择 block 组合："
                "协作产出一份报告 → 推荐 agent_master_collaboration_block；"
                "依次测试/对比多个 Agent → 推荐 multi_agent_linear_block；"
                "测试单个 Agent → 推荐 test_agent_block；"
                "否则用基础 block 组合自定义流程。"
                "流程不可混用。"
            )
        user_parts.append(
            f"约束: 仅使用可用 blocks；遵守场景约束与 required/forbidden_blocks；"
            f"流程选择: {flow_rule} "
            "send_query 的 params.query 必须使用「初始状态」中的 query 或 queries_per_agent；"
            "若 catalog 含 app_ready：其 params.options.agent_name 须与 agents_to_test 对应；"
            "若 catalog 不含 app_ready（画布自定义块）：凡需绑定 Agent 的步骤，须在 params 中给出 agent_name 或 agent "
            "（多 Agent 可用 params.agents 字符串数组），以便 /plan 响应的 selected_agents 能抽取顺序；"
            "refresh_page 主流程规划中不要使用；不要输出解释。"
        )
        user_prompt = "\n".join(user_parts)
        resp = chat_completion_with_retry(
            model=llm_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            timeout=None,
            provider=_prov,
            api_key=api_key,
            base_url=base_url,
            max_retries=3,
            timing_label=timing_label,
        )
        content = (resp.choices[0].message.content or "").strip()
        return _parse_llm_plan(
            content,
            allowed_blocks=set(_catalog_to_map(block_catalog).keys()),
            source="llm",
        )
    except Exception as exc:
        logger.warning("[GoalPlanner] LLM planning failed: %s", exc)
        return None


def _expand_compound_block(
    block_id: str,
    params: dict[str, Any],
    compound_blocks: list[dict[str, Any]],
) -> list[GoalPlanStep]:
    """
    将复合 Block 调用展开为原子 GoalPlanStep 片段。
    - 无 iterate：按模板顺序线性展开
    - 有 iterate：每个迭代实例保留自己内部的线性依赖；不同实例之间默认无依赖
    """
    cb_map = {b.get("block_id"): b for b in compound_blocks if isinstance(b, dict) and b.get("block_id")}
    cb = cb_map.get(block_id)
    if not cb:
        return []

    step_template = cb.get("step_template") or cb.get("steps")
    if not isinstance(step_template, list):
        return []

    iterate = cb.get("iterate")
    if isinstance(iterate, dict):
        # 如 iterate: {"agent": "agents", "query": "queries"} 表示对 agents 和 queries 并行迭代
        agent_key = iterate.get("agent")
        query_key = iterate.get("query")
        agents = params.get(agent_key or "agents") if isinstance(agent_key, str) else []
        queries = params.get(query_key or "queries") if isinstance(query_key, str) else []
        if not isinstance(agents, list) or not isinstance(queries, list):
            return []
        n = min(len(agents), len(queries))
        if n == 0:
            return []
        steps: list[GoalPlanStep] = []
        last_iter_final_step_id: str | None = None  # 上一迭代最后一步，用于串行连接
        for i in range(n):
            agent = agents[i] if i < len(agents) else ""
            query = queries[i] if i < len(queries) else ""
            prev_step_id: str | None = None
            iter_first_step_id: str | None = None
            for st in step_template:
                tool_name = st.get("block_id") or st.get("tool_name")
                if not isinstance(tool_name, str):
                    continue
                p = dict(st.get("params") or {})
                p = _resolve_template_value(p, query=str(query), agent_name=str(agent) if agent else None, agent=str(agent) if agent else None)
                if tool_name == "send_query" and "query" not in p:
                    p["query"] = str(query)
                if tool_name == "app_ready" and agent and "options" not in p:
                    p["options"] = {"agent_name": str(agent)}
                local_step_id = f"{block_id}__iter{i}__{len(steps)}"
                # 迭代内部：依赖前一步；迭代第一步额外依赖上一迭代的最后一步（保证串行执行顺序）
                if prev_step_id:
                    depends_on = [prev_step_id]
                elif last_iter_final_step_id:
                    depends_on = [last_iter_final_step_id]
                else:
                    depends_on = []
                gate, risk_level = _TOOL_GATE_MAP.get(tool_name, ("none", "low"))
                steps.append(
                    GoalPlanStep(
                        step_id=local_step_id,
                        tool_call=ToolCall(tool_name=tool_name, params=p),
                        depends_on=depends_on,
                        gate=gate,
                        risk_level=risk_level,
                    )
                )
                if iter_first_step_id is None:
                    iter_first_step_id = local_step_id
                prev_step_id = local_step_id
            # 记录本迭代最后一步，供下一迭代的第一步依赖
            if prev_step_id:
                last_iter_final_step_id = prev_step_id
        return steps

    # 无 iterate：单次展开，如 test_agent_block、agent_master_collaboration_block
    query = params.get("query") or ""
    agent_name = params.get("agent_name")
    agents = params.get("agents") if isinstance(params.get("agents"), list) else None
    steps: list[GoalPlanStep] = []
    prev_step_id: str | None = None
    for idx, st in enumerate(step_template):
        tool_name = st.get("block_id") or st.get("tool_name")
        if not isinstance(tool_name, str):
            continue
        p = dict(st.get("params") or {})
        p = _resolve_template_value(
            p,
            query=str(query),
            agent_name=str(agent_name) if agent_name else None,
            agents=agents,
        )
        if tool_name == "send_query" and "query" not in p:
            p["query"] = str(query)
        if tool_name == "app_ready" and agent_name and "options" not in p:
            p["options"] = {"agent_name": str(agent_name)}
        local_step_id = f"{block_id}__{idx}"
        depends_on = [prev_step_id] if prev_step_id else []
        steps.append(
            GoalPlanStep(
                step_id=local_step_id,
                tool_call=ToolCall(tool_name=tool_name, params=p),
                depends_on=depends_on,
            )
        )
        prev_step_id = local_step_id
    return steps


def _expand_compound_blocks_in_plan(
    plan: GoalPlan,
    compound_blocks: list[dict[str, Any]],
    atomic_block_ids: set[str],
) -> GoalPlan:
    """将计划中的复合 Block 步骤展开为原子步骤，并保留/重建原始 DAG 依赖。"""
    compound_ids = {b.get("block_id") for b in compound_blocks if isinstance(b, dict) and b.get("block_id")}
    fragments: list[tuple[GoalPlanStep, list[GoalPlanStep]]] = []
    terminal_ids_by_origin: dict[str, list[str]] = {}

    for s in plan.steps:
        tc = s.tool_call
        if not tc or tc.tool_name in atomic_block_ids or tc.tool_name not in compound_ids:
            fragment = [
                GoalPlanStep(
                    step_id=f"{s.step_id}__0",
                    tool_call=tc,
                    depends_on=[],
                    note=s.note,
                    expected_output=s.expected_output,
                    gate=s.gate,
                    risk_level=s.risk_level,
                )
            ]
        else:
            raw_fragment = _expand_compound_block(tc.tool_name, tc.params or {}, compound_blocks)
            if not raw_fragment:
                fragment = [
                    GoalPlanStep(
                        step_id=f"{s.step_id}__0",
                        tool_call=tc,
                        depends_on=[],
                        note=s.note,
                        expected_output=s.expected_output,
                        gate=s.gate,
                        risk_level=s.risk_level,
                    )
                ]
            else:
                fragment = []
                raw_child_ids = {dep for item in raw_fragment for dep in item.depends_on}
                raw_terminal_ids = {item.step_id for item in raw_fragment if item.step_id not in raw_child_ids}
                for idx, item in enumerate(raw_fragment):
                    new_step_id = f"{s.step_id}__{idx}"
                    local_depends = []
                    for dep in item.depends_on:
                        dep_suffix = dep.split("__")[-1]
                        local_depends.append(f"{s.step_id}__{dep_suffix}")
                    is_terminal = item.step_id in raw_terminal_ids
                    fragment.append(
                        GoalPlanStep(
                            step_id=new_step_id,
                            tool_call=item.tool_call,
                            depends_on=local_depends,
                            note=s.note if idx == 0 else None,
                            expected_output=s.expected_output if is_terminal else None,
                            gate=s.gate if is_terminal else "none",
                            risk_level=s.risk_level if is_terminal else "low",
                        )
                    )

        child_ids = {dep for item in fragment for dep in item.depends_on}
        terminal_ids = [item.step_id for item in fragment if item.step_id not in child_ids]
        terminal_ids_by_origin[s.step_id] = terminal_ids or [fragment[-1].step_id]
        fragments.append((s, fragment))

    expanded_steps: list[GoalPlanStep] = []
    for origin_step, fragment in fragments:
        external_depends: list[str] = []
        for dep in origin_step.depends_on:
            external_depends.extend(terminal_ids_by_origin.get(dep, [dep]))
        entry_step_ids = {item.step_id for item in fragment if not item.depends_on}
        for item in fragment:
            depends_on = list(item.depends_on)
            if item.step_id in entry_step_ids:
                depends_on = _dedupe_step_ids(external_depends + depends_on)
            expanded_steps.append(
                GoalPlanStep(
                    step_id=item.step_id,
                    tool_call=item.tool_call,
                    depends_on=_dedupe_step_ids(depends_on),
                    note=item.note,
                    expected_output=item.expected_output,
                    gate=item.gate,
                    risk_level=item.risk_level,
                )
            )

    id_map = {step.step_id: f"s{idx}" for idx, step in enumerate(expanded_steps)}
    renumbered_steps: list[GoalPlanStep] = []
    for step in expanded_steps:
        renumbered_steps.append(
            GoalPlanStep(
                step_id=id_map[step.step_id],
                tool_call=step.tool_call,
                depends_on=[id_map.get(dep, dep) for dep in _dedupe_step_ids(step.depends_on)],
                note=step.note,
                expected_output=step.expected_output,
                gate=step.gate,
                risk_level=step.risk_level,
            )
        )
    return GoalPlan(steps=renumbered_steps, source=plan.source, reason=plan.reason or "expanded_from_compound")


def _dedupe_step_ids(step_ids: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for step_id in step_ids:
        if not isinstance(step_id, str) or not step_id.strip():
            continue
        if step_id in seen:
            continue
        seen.add(step_id)
        out.append(step_id)
    return out


def _hydrate_plan_with_initial_state(plan: GoalPlan, initial_state: dict[str, Any]) -> GoalPlan:
    """
    用 initial_state 中的 query / agents_to_test / queries_per_agent 填充计划步骤的 params，
    确保 goal → query → 执行 params 的完整联动，不因 LLM 输出遗漏而割裂。
    支持基础 block 与复合 block。
    """
    agents_to_test = initial_state.get("agents_to_test")
    queries_per_agent = initial_state.get("queries_per_agent")
    default_query = _pick_query(initial_state, "")
    agent_index = 0
    for step in plan.steps:
        tc = step.tool_call
        if not tc:
            continue
        # 复合 block：补全 agents / query / agent_name
        if tc.tool_name == "agent_master_collaboration_block":
            params = dict(tc.params or {})
            if isinstance(agents_to_test, list) and len(agents_to_test) > 1:
                params["agents"] = agents_to_test
                if isinstance(queries_per_agent, list) and len(queries_per_agent) == len(agents_to_test):
                    params["queries"] = [str(q).strip() for q in queries_per_agent if isinstance(q, str) and q.strip()]
            if not params.get("query"):
                params["query"] = default_query
            step.tool_call = ToolCall(tool_name=tc.tool_name, params=params)
        elif tc.tool_name == "agent_master_run_flow_once":
            params = dict(tc.params or {})
            if isinstance(agents_to_test, list) and len(agents_to_test) > 1:
                params.setdefault("agents", agents_to_test)
                if isinstance(queries_per_agent, list) and len(queries_per_agent) == len(agents_to_test):
                    params["queries"] = [str(q).strip() for q in queries_per_agent if isinstance(q, str) and q.strip()]
            if not params.get("query"):
                params["query"] = default_query
            step.tool_call = ToolCall(tool_name=tc.tool_name, params=params)
        elif tc.tool_name == "multi_agent_linear_block":
            params = dict(tc.params or {})
            # initial_state.agents_to_test 为 run_schedule 的权威来源，必须覆盖 LLM 可能根据 goal 输出错误的多 agent 列表
            if isinstance(agents_to_test, list) and agents_to_test:
                params["agents"] = agents_to_test
            if not params.get("queries") and isinstance(queries_per_agent, list) and len(queries_per_agent) == len(params.get("agents", [])):
                params["queries"] = [str(q).strip() for q in queries_per_agent if q]
            elif not params.get("queries") and params.get("agents"):
                params["queries"] = [default_query] * len(params["agents"])
            step.tool_call = ToolCall(tool_name=tc.tool_name, params=params)
        elif tc.tool_name == "test_agent_block":
            params = dict(tc.params or {})
            if not params.get("agent_name"):
                agent_name = initial_state.get("agent_name")
                if not agent_name and isinstance(agents_to_test, list) and len(agents_to_test) == 1:
                    agent_name = agents_to_test[0]
                if agent_name:
                    params["agent_name"] = str(agent_name)
            if not params.get("query"):
                params["query"] = default_query
            step.tool_call = ToolCall(tool_name=tc.tool_name, params=params)
        elif tc.tool_name == "app_ready":
            if isinstance(agents_to_test, list) and agent_index < len(agents_to_test):
                agent_name = agents_to_test[agent_index]
                if isinstance(agent_name, str) and agent_name.strip():
                    params = dict(tc.params or {})
                    opts = dict(params.get("options") or {})
                    opts["agent_name"] = agent_name.strip()
                    params["options"] = opts
                    step.tool_call = ToolCall(tool_name=tc.tool_name, params=params)
            agent_index += 1
        elif tc.tool_name == "send_query":
            query = default_query
            if isinstance(queries_per_agent, list) and len(queries_per_agent) > 0:
                idx = agent_index - 1 if agent_index > 0 else 0
                if idx < len(queries_per_agent):
                    q = queries_per_agent[idx]
                    if isinstance(q, str) and q.strip():
                        query = q.strip()
            if query:
                params = dict(tc.params or {})
                params["query"] = query
                step.tool_call = ToolCall(tool_name=tc.tool_name, params=params)
    return plan


def build_goal_plan(
    *,
    block_catalog: list[dict[str, Any]],
    initial_state: dict[str, Any],
    task_description: str,
    compound_blocks: list[dict[str, Any]] | None = None,
    use_llm_planner: bool = True,
    goal: str | None = None,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    flow_template: dict[str, Any] | None = None,
    constraints: dict[str, Any] | None = None,
    scenario_context: str | None = None,
    block_semantics: str | None = None,
    use_template_as_hint: bool = True,
    intent_override: GoalIntent | None = None,
) -> GoalPlan:
    """
    构建目标驱动计划。LLM Planner 始终主导：分析 goal 后选择调用复合 Block 或基础 RPA block 组成流程。
    - 复合 Block（test_agent_block、multi_agent_linear_block、agent_master_collaboration_block）作为可选快捷方式；
    - 若无符合的复合 Block，LLM 用基础 block（app_ready、send_query、get_response、discovery_bootstrap 等）组合流程。

    Returns:
        GoalPlan（含步骤、依赖、来源）。
    """
    atomic_block_ids = set(_catalog_to_map(block_catalog or []).keys())
    compound_list = compound_blocks or []

    # 合并基础 block 与复合 block，供 LLM 选择
    catalog_for_llm: list[dict[str, Any]] = list(block_catalog or [])
    for cb in compound_list:
        if isinstance(cb, dict) and cb.get("block_id"):
            item = {"block_id": cb["block_id"], "description": cb.get("description", "")}
            if cb.get("params_schema"):
                item["params"] = cb["params_schema"]
            catalog_for_llm.append(item)

    # ── 结构化目标解析（GoalIntent）──────────────────────────────────────────
    # 在调用 LLM Planner 之前，先将 goal 解析为结构化意图；
    # 硬约束（execution_constraints）将以高优先级注入规划 prompt，防止被软目标覆盖。
    # intent_override：入口（如 run_poffices_agent）已解析/Discovery 已定 Agent 时传入，避免重复 parse_goal。
    intent: GoalIntent | None = None
    if intent_override is not None:
        intent = intent_override
    elif goal and str(goal).strip():
        intent = parse_goal(goal, provider=llm_provider, model=llm_model)
        if intent.has_ambiguities():
            logger.warning(
                "[GoalPlanner] Goal 存在歧义，已按保守方式理解（建议澄清）: %s",
                intent.ambiguities,
            )

    # LLM 主导：始终由 LLM 分析 goal 并选择 block 组合
    if use_llm_planner:
        llm_result = _llm_plan(
            block_catalog=catalog_for_llm,
            initial_state=initial_state,
            task_description=task_description,
            goal=goal,
            intent=intent,
            provider=llm_provider,
            model=llm_model,
            flow_template=flow_template,
            constraints=constraints,
            scenario_context=scenario_context,
            block_semantics=block_semantics,
        )
        if llm_result:
            # DAG 校验与自动修复（有环/悬空引用均在此清除）
            llm_result = fix_dag(llm_result)
            dag_errors = validate_dag(llm_result)
            if dag_errors:
                logger.warning("[GoalPlanner] DAG 校验警告（已修复）: %s", dag_errors)
            plan = _hydrate_plan_with_initial_state(llm_result, initial_state)
            logger.info(
                "[GoalPlanner] LLM plan: %d steps, agents_to_test=%s",
                len(plan.steps),
                initial_state.get("agents_to_test"),
            )
            # 若 LLM 计划中含复合 Block，展开为原子步骤
            if compound_list:
                plan = _expand_compound_blocks_in_plan(
                    plan,
                    compound_list,
                    atomic_block_ids,
                )
            return plan

    # 无 LLM 时规则兜底
    plan = _rule_plan(
        block_catalog=block_catalog or [],
        initial_state=initial_state,
        task_description=task_description,
        flow_template=flow_template if use_template_as_hint else None,
    )
    final_plan = _hydrate_plan_with_initial_state(plan, initial_state)
    return final_plan


def linearize_goal_plan(plan: GoalPlan) -> list[ToolCall]:
    """
    将依赖计划转为可顺序执行序列（拓扑排序）。
    运行时仍按单线程执行，保证兼容现有 RPA 适配层。
    """
    pending = {s.step_id: s for s in plan.steps}
    done: set[str] = set()
    ordered: list[ToolCall] = []

    while pending:
        executable = [
            s for sid, s in pending.items()
            if set(s.depends_on).issubset(done)
        ]
        if not executable:
            # 依赖异常时兜底：按剩余步骤字典序执行，避免死锁
            fallback = pending[sorted(pending.keys())[0]]
            ordered.append(fallback.tool_call.model_copy(update={"step_id": fallback.step_id}))
            done.add(fallback.step_id)
            pending.pop(fallback.step_id, None)
            continue
        executable_sorted = sorted(executable, key=lambda x: x.step_id)
        for step in executable_sorted:
            ordered.append(step.tool_call.model_copy(update={"step_id": step.step_id}))
            done.add(step.step_id)
            pending.pop(step.step_id, None)
    return ordered


def build_recovery_plan(
    *,
    failed_tool_name: str,
    block_catalog: list[dict[str, Any]],
    initial_state: dict[str, Any],
    task_description: str,
    failed_tool_call: ToolCall | None = None,
    failed_execution_result: Any = None,
    use_llm_planner: bool = True,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    flow_template: dict[str, Any] | None = None,
    constraints: dict[str, Any] | None = None,
    block_semantics: str | None = None,
) -> GoalPlan:
    """
    构建失败后的恢复计划（Replan）。
    先给规则恢复路径；若启用 LLM，可让 LLM 在失败上下文下产出替代 plan。
    failed_execution_result 可选传入失败步骤的 ExecutionResult，供 LLM replan 时参考错误细节。
    """
    block_map = _catalog_to_map(block_catalog)
    query = _pick_query(initial_state, task_description)
    # 若失败的是 send_query 且已知其 params，优先用失败步的 query 重试（避免多 Agent 时错用 q1）
    if failed_tool_name in ("send_query", "poffices_query") and failed_tool_call and failed_tool_call.params:
        q = failed_tool_call.params.get("query")
        if isinstance(q, str) and q.strip():
            query = q.strip()

    # 优先用规则确保可控恢复
    if failed_tool_name in ("send_query", "poffices_query"):
        calls = [ToolCall(tool_name=failed_tool_name, params={"query": query})]
        if "get_response" in block_map and failed_tool_name != "poffices_query":
            calls.append(ToolCall(tool_name="get_response", params={}))
        return _make_linear_plan(calls, source="replan_rule", reason=f"recover_from_{failed_tool_name}")
    if failed_tool_name in ("get_response",):
        # 长时间卡顿时：若有 refresh_page 则先刷新再等完成再取回；否则仅 wait_output_complete + get_response
        if "refresh_page" in block_map and "wait_output_complete" in block_map and "get_response" in block_map:
            return _make_linear_plan(
                [
                    ToolCall(tool_name="refresh_page", params={}),
                    ToolCall(tool_name="wait_output_complete", params={}),
                    ToolCall(tool_name="get_response", params={}),
                ],
                source="replan_rule",
                reason="recover_from_get_response_refresh",
            )
        if "wait_output_complete" in block_map and "get_response" in block_map:
            return _make_linear_plan(
                [
                    ToolCall(tool_name="wait_output_complete", params={}),
                    ToolCall(tool_name="get_response", params={}),
                ],
                source="replan_rule",
                reason="recover_from_get_response",
            )
        if "get_response" in block_map:
            return _make_linear_plan(
                [ToolCall(tool_name="get_response", params={})],
                source="replan_rule",
                reason="recover_from_get_response",
            )
    if failed_tool_name in ("app_ready", "poffices_bootstrap"):
        calls: list[ToolCall] = [ToolCall(tool_name=failed_tool_name, params={})]
        if "send_query" in block_map:
            calls.append(ToolCall(tool_name="send_query", params={"query": query}))
        if "get_response" in block_map:
            calls.append(ToolCall(tool_name="get_response", params={}))
        return _make_linear_plan(calls, source="replan_rule", reason=f"recover_from_{failed_tool_name}")

    # agent_master_* 失败：仅重试一次，不重整个流程（避免 discovery_bootstrap→select→run 重复）
    if failed_tool_name == "agent_master_run_flow_once" and "agent_master_run_flow_once" in block_map:
        params = dict(failed_tool_call.params) if failed_tool_call and failed_tool_call.params else {}
        if "query" not in params or not params.get("query"):
            params["query"] = query
        return _make_linear_plan(
            [ToolCall(tool_name="agent_master_run_flow_once", params=params)],
            source="replan_rule",
            reason="recover_from_agent_master_run_flow_once_retry",
        )
    if failed_tool_name == "agent_master_select_agents_for_flow" and "agent_master_select_agents_for_flow" in block_map:
        params = dict(failed_tool_call.params) if failed_tool_call and failed_tool_call.params else {}
        if "agents" not in params or not params.get("agents"):
            agents = initial_state.get("agents_to_test")
            if isinstance(agents, list) and agents:
                params["agents"] = agents
        if params.get("agents"):
            return _make_linear_plan(
                [ToolCall(tool_name="agent_master_select_agents_for_flow", params=params)],
                source="replan_rule",
                reason="recover_from_agent_master_select_retry",
            )
    if failed_tool_name == "discovery_bootstrap" and "discovery_bootstrap" in block_map:
        return _make_linear_plan(
            [ToolCall(tool_name="discovery_bootstrap", params={})],
            source="replan_rule",
            reason="recover_from_discovery_bootstrap_retry",
        )

    # 可选 LLM replan（规则不覆盖时）
    if use_llm_planner:
        recovery_hint = "请仅给出轻量恢复（重试失败步或 wait_output_complete+get_response），不要建议 discovery_bootstrap→agent_master_select→agent_master_run_flow_once 这类重整个流程。"
        # 从 ExecutionResult 中提取错误细节，丰富 LLM 的上下文判断
        error_context_lines: list[str] = []
        if failed_execution_result is not None:
            err_type = getattr(failed_execution_result, "error_type", None)
            raw = getattr(failed_execution_result, "raw_response", None)
            if err_type:
                error_context_lines.append(f"错误类型: {err_type}")
            if isinstance(raw, str) and raw.strip():
                error_context_lines.append(f"错误详情: {raw.strip()[:300]}")
            elif isinstance(raw, dict):
                import json as _json
                try:
                    error_context_lines.append(f"错误详情: {_json.dumps(raw, ensure_ascii=False)[:300]}")
                except Exception:
                    pass
        error_context = ("\n" + "\n".join(error_context_lines)) if error_context_lines else ""
        recovery_description = (
            f"{task_description}\n上一步失败工具: {failed_tool_name}{error_context}，请给恢复计划。{recovery_hint}"
        )
        llm_result = _llm_plan(
            block_catalog=block_catalog,
            initial_state={**initial_state, "last_failed_tool": failed_tool_name},
            task_description=recovery_description,
            provider=llm_provider,
            model=llm_model,
            flow_template=flow_template,
            constraints=constraints,
            block_semantics=block_semantics,
            timing_label="recovery_planner",
        )
        if llm_result:
            llm_result.source = "replan_llm"
            llm_result.reason = f"recover_from_{failed_tool_name}"
            return _hydrate_plan_with_initial_state(llm_result, initial_state)

    # 最终兜底：重试同一工具一次
    return _make_linear_plan(
        [ToolCall(tool_name=failed_tool_name, params={"query": query} if "query" in failed_tool_name else {})],
        source="replan_rule",
        reason=f"retry_{failed_tool_name}",
    )
