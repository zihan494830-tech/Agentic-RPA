#!/usr/bin/env python
"""Poffices 统一入口（按 --runs 指定轮数）。

同一浏览器会话内：首轮 bootstrap + query，后续轮次点击 New question + 新 query。
复用 RPA 实例，每轮新建 Orchestrator，轨迹独立落盘并汇总 metrics。
多轮时 LLM query 建议器根据「上一轮 query + 上一轮 Agent 表现」决定下一轮问什么、怎么问（表现好可深化或换领域，表现差可降难度或换角度）。

用法：
  python run_poffices_agent.py --goal "验证正常 Query 流程没问题"   # 默认 --config dynamic --runs 1
  python run_poffices_agent.py --goal "用三个 agent 写一份报告"
  python run_poffices_agent.py --runs 3 --llm-agent
  python run_poffices_agent.py --runs 1 --agents "Research Proposal,Market Analysis,Project Proposal"
  --config: 不传则默认 experiment_poffices.json（固定场景）；传 experiment_poffices_dynamic 等可切换实验。
  --goal: 覆盖实验中的 goal，仅 goal_driven 有效。未传 --agents 时会自动用 Goal Interpreter 解析 goal 并填充 agents。
  --runs N：必填。表示「完整跑 N 次」；每次 = 一次 run_until_done（单 Agent 时约 3 步，--agents 时为 3×Agent 数 步）。
  --agents "A,B,C"：多 Agent 时，**一轮**内会按顺序测完 A→B→C（每 Agent：app_ready→send_query→get_response），
    不会因为「只跑一轮」而只测第一个就停；--runs 1 表示「这样一整轮多 Agent 跑 1 次」。
  --strategy: rule=按得分/难度规则 / auto=由 LLM 选 / deepen / diversify

依赖：playwright、POFFICES_*、可选 LLM 出题（OPENAI_API_KEY / XAI_API_KEY）。
"""
import argparse
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

root = Path(__file__).resolve().parent
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

try:
    from dotenv import load_dotenv
    load_dotenv(root / ".env")
except ImportError:
    pass

from raft.agents.factory import create_poffices_agent, resolve_agent_under_test
from raft.core.config.loader import load_experiment_config, load_task_spec
from raft.orchestrator.runner import Orchestrator
from raft.rpa import get_default_rpa

from raft.reporting import build_report_with_llm

# 默认实验配置：固定场景（单 Agent 多轮 / 多 Agent N 轮）
DEFAULT_EXPERIMENT = "experiment_poffices.json"


def _query_from_result(result: dict) -> str | None:
    """从单轮 result 中提取本轮使用的 query（用于多轮时传入下一轮）。"""
    traj = result.get("trajectory") or []
    if not traj:
        return None
    first = traj[0]
    snap = (first.get("step_result") or {}).get("agent_input_snapshot") or {}
    state = snap.get("state") or {}
    q = state.get("query")
    if isinstance(q, str) and q.strip():
        return q.strip()
    return None


def _count_agents_in_run(result: dict) -> int:
    """从 result 中检测本轮测试了几个 Agent（通过计数 app_ready 调用次数）。"""
    traj = result.get("trajectory") or []
    count = 0
    for entry in traj:
        step_result = entry.get("step_result") or {}
        for tc in step_result.get("tool_calls") or []:
            if tc.get("tool_name") == "app_ready":
                count += 1
    return count if count > 0 else 1


def _extract_failed_steps(trajectory: list[dict]) -> list[dict]:
    """从轨迹中提取失败步骤的细粒度信息，供下一轮出题策略参考。"""
    failed: list[dict] = []
    for entry in trajectory:
        step_result = entry.get("step_result") or {}
        step_index = step_result.get("step_index", entry.get("step_index"))
        execution_results = step_result.get("execution_results") or []
        tool_calls = step_result.get("tool_calls") or []
        for i, er in enumerate(execution_results):
            if not er.get("success", True):
                tool_name = er.get("tool_name") or (tool_calls[i].get("tool_name") if i < len(tool_calls) else None)
                failed.append({
                    "step_index": step_index,
                    "tool_name": tool_name,
                    "error_type": er.get("error_type"),
                    "raw_response": str(er.get("raw_response") or "")[:200],
                })
    return failed


def _previous_rounds_from_results(results: list[dict]) -> list[dict]:
    """从多轮 results 构建 previous_rounds：每轮含 query、success、step_count、details、llm_judge
    及失败步骤细节（failed_steps），供下一轮 LLM 根据「问什么+表现如何+哪里失败」出题。"""
    rounds: list[dict] = []
    for r in results:
        q = _query_from_result(r)
        metrics = r.get("metrics") or {}
        if not isinstance(metrics, dict):
            metrics = {}
        trajectory = r.get("trajectory") or []
        failed_steps = _extract_failed_steps(trajectory)
        num_agents = _count_agents_in_run(r)
        rounds.append({
            "query": q or "",
            "success": metrics.get("success"),
            "step_count": metrics.get("step_count"),
            "details": metrics.get("details"),
            "llm_judge": metrics.get("llm_judge"),
            "execution_success_rate": metrics.get("execution_success_rate"),
            "retry_count": metrics.get("retry_count"),
            "timeout_count": metrics.get("timeout_count"),
            "failed_steps": failed_steps,   # 细粒度：哪个 tool 在哪步失败、错误类型
            "num_agents_in_run": num_agents, # 本轮测试了几个 Agent，供下一轮出题策略感知多 Agent 场景
        })
    return rounds


def main() -> None:
    parser = argparse.ArgumentParser(description="Poffices 统一入口（按 --runs 指定轮数）")
    parser.add_argument(
        "--runs",
        type=int,
        default=None,
        help="完整跑 N 次。不传时从 goal 中解析（如「跑 3 轮」→ 3）；解析不到则默认 1",
    )
    parser.add_argument(
        "--strategy",
        choices=["auto", "rule", "deepen", "diversify"],
        default="rule",
        help="多轮 query 策略：rule=按得分/难度/错误类型规则决定，auto=由 LLM 选，deepen=同领域深化，diversify=换领域（默认 rule）",
    )
    parser.add_argument(
        "--no-llm-planner",
        action="store_true",
        dest="no_llm_planner",
        help="关闭 LLM 规划器；默认：三种模式（单 Agent 多轮/多 Agent 协作/一次性测多 Agent）+ 未命中时自动 LLM 规划",
    )
    parser.add_argument(
        "--force-llm-plan",
        action="store_true",
        dest="force_llm_plan",
        help="强制使用 LLM 规划：跳过复合 Block 匹配，由 LLM 从 allowed_blocks 组流程（用于测试 LLM 规划能力）",
    )
    parser.add_argument(
        "--max-replans",
        type=int,
        default=None,
        help="goal_driven 下失败后的最大重规划次数（默认配置值或 2）",
    )
    parser.add_argument(
        "--fault-query-wait-sec",
        type=int,
        default=None,
        dest="fault_query_wait_sec",
        metavar="N",
        help=(
            "故障注入：将 get_response / wait_output_complete 的等待超时强制设为 N 秒（绕过 60s 下限）。"
            "用于复现 Case Study B——设 N<120 时 get_response 必然超时，触发 recovery 计划。"
            "示例：--fault-query-wait-sec 10"
        ),
    )
    # Agent 类型覆盖（不传则沿用配置文件 agent_type 字段）
    agent_group = parser.add_mutually_exclusive_group()
    agent_group.add_argument(
        "--llm-agent",
        dest="agent_type",
        action="store_const",
        const="llm",
        help="强制使用 LLM 驱动 Agent（覆盖配置文件）",
    )
    agent_group.add_argument(
        "--no-llm-agent",
        dest="agent_type",
        action="store_const",
        const="rule",
        help="强制使用规则驱动 Agent（覆盖配置文件）",
    )
    parser.add_argument(
        "--llm-provider",
        type=str,
        default=None,
        choices=["qwen", "openai", "grok"],
        help="覆盖 LLM 提供商（不传则沿用配置文件 agent_provider 字段）",
    )
    parser.add_argument(
        "--llm-model",
        type=str,
        default=None,
        help="覆盖 LLM 模型名称（不传则沿用配置文件 agent_model 字段）",
    )
    parser.add_argument(
        "--agent",
        type=str,
        default=None,
        help="覆盖待测 Agent 名称（出题线与执行线均联动，不传则沿用配置文件 agent_under_test）",
    )
    parser.add_argument(
        "--agents",
        type=str,
        default=None,
        help="多 Agent：逗号分隔名单。一轮内会依次测完每个 Agent（每 Agent 3 步），不会只测第一个就停。与 --agent 二选一，--agents 优先",
    )
    parser.add_argument(
        "--queries",
        type=str,
        default=None,
        help="多 Agent 时可选：与 --agents 一一对应的 query，逗号分隔（若条数与 agents 一致则每 Agent 使用各自 query，实现动态联动）",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        metavar="NAME_OR_PATH",
        help="实验配置：不传则默认 scenarios/experiment_poffices.json（固定场景）。可传文件名如 experiment_poffices_dynamic 或相对路径",
    )
    parser.add_argument(
        "--goal",
        type=str,
        default=None,
        help="覆盖实验中的 goal（仅 goal_driven 有效）；不传则使用配置文件里的 extra.goal",
    )
    parser.add_argument(
        "--full-report",
        action="store_true",
        dest="full_report",
        help="完整报告：启用 LLM 判分与多轮总结。不传则默认最简报告（mini 模式，便于测试）",
    )
    parser.add_argument(
        "--no-interpret",
        action="store_true",
        dest="no_interpret",
        help="禁用 Goal Interpreter，不自动从 goal 解析 agents（沿用配置文件默认）",
    )
    args = parser.parse_args()

    scenarios = root / "scenarios"
    log_dir = root / "logs" / "poffices"
    log_dir.mkdir(parents=True, exist_ok=True)

    # 解析实验配置：不传 --config 时，有 --goal 则默认 experiment_poffices_dynamic，否则固定场景
    if args.config is None or args.config.strip() == "":
        if args.goal:
            config_path = scenarios / "experiment_poffices_dynamic.json"
        else:
            config_path = scenarios / DEFAULT_EXPERIMENT
    else:
        raw = args.config.strip()
        if os.path.isabs(raw) or "/" in raw or "\\" in raw:
            config_path = Path(raw)
        else:
            config_path = scenarios / (raw if raw.endswith(".json") else f"{raw}.json")
    if not config_path.exists():
        print(f"错误：实验配置不存在 {config_path}", file=sys.stderr)
        sys.exit(1)
    config = load_experiment_config(config_path)

    # 故障注入：--fault-query-wait-sec 仅缩短 get_response 的超时；其他步骤（wait_output_complete 等）保持正常
    _fault_wait = getattr(args, "fault_query_wait_sec", None)
    if _fault_wait:
        print(f"[故障注入] get_response 超时强制设为 {_fault_wait}s（其他步骤保持 240s），用于触发 recovery 计划", flush=True)

    # 提前创建 RPA，供 Discovery 与后续编排共用
    rpa = get_default_rpa(
        backend="poffices",
        headless=False,
        timeout_ms=30_000,
        query_wait_sec=240,
    )
    if _fault_wait:
        rpa.fault_get_response_remaining = 1  # 第一次 get_response 强制超时，触发 recovery

    # run 编排前：Goal Interpreter / Discovery 的 LLM 耗时（合并进首轮 result.llm_timing_events）
    preflight_llm_events: list[dict] = []

    # 未传 --agent/--agents 且有 --goal 且未禁用时，解析 goal 并可能执行 Discovery
    if args.goal and not args.agent and not args.agents and not getattr(args, "no_interpret", False):
        from raft.core.goal_interpreter import interpret_goal
        from raft.core.config.scenario import resolve_allowed_agents
        from raft.core.llm_timing import attach_llm_timing_sink, reset_llm_timing_sink
        from raft.core.office_discovery import run_discovery
        from raft.core.planner.goal_intent import GoalIntent as PlannerGoalIntent

        def _planner_intent_payload(agents: list[str], *, source_cn: str, topic: str = "") -> dict:
            label = "、".join(agents)
            t = (topic or "").strip()
            ci = f"对 {label} 在「{t}」主题下进行测试。" if t else f"对 {label} 在用户指定主题下进行测试。"
            return asdict(
                PlannerGoalIntent(
                    execution_constraints=[
                        f"仅使用待测 Agent: {label}（{source_cn}，不得更换为其他 Agent）。",
                        "不得在主流程中规划 discovery_bootstrap、list_offices、expand_office、list_agents_in_office（解析阶段已完成）。",
                    ],
                    content_intent=[ci],
                    ambiguities=[],
                    raw_goal=args.goal or "",
                    confidence=1.0,
                )
            )

        _pre_llm_tok = attach_llm_timing_sink(preflight_llm_events)
        try:
            allowed = resolve_allowed_agents(config)
            provider = os.environ.get("RAFT_LLM_PROVIDER") or "qwen"
            intent = interpret_goal(args.goal, provider=provider, available_agents=allowed if allowed else None)
            if intent.agents:
                args.agents = ",".join(intent.agents)
                if len(intent.agents) > 1:
                    mode = "多 Agent 协作" if intent.collaboration_mode else "一次性测多个 Agent"
                else:
                    mode = "单 Agent 多轮" if intent.runs > 1 else "单 Agent 单轮"
                print(f"[Goal Interpreter] 模式={mode}, agents={intent.agents}, topic={intent.topic or '(无)'}, runs={intent.runs}", flush=True)
                config.extra["planner_goal_intent"] = _planner_intent_payload(
                    intent.agents, source_cn="来自 Goal Interpreter", topic=intent.topic or ""
                )
            elif intent.office_intent or intent.topic or intent.raw_goal:
                # Discovery 模式：从 UI 动态发现 office 与 agents（office 未指定时从 topic 推断）
                print(f"[Discovery] office_intent={intent.office_intent}, topic={intent.topic or '(无)'}, count={intent.count}", flush=True)
                selected = run_discovery(rpa, intent, provider=provider, allowed_agents=allowed)
                if selected:
                    args.agents = ",".join(selected)
                    intent.agents = selected
                    config.extra["agents_from_discovery"] = selected  # 供 validate_scenario_run 放行
                    config.extra["planner_goal_intent"] = _planner_intent_payload(
                        selected, source_cn="已由 Discovery 在 UI 中选定", topic=intent.topic or ""
                    )
                    config.extra["post_discovery_resume"] = True
                    if hasattr(rpa, "mark_resume_after_discovery"):
                        rpa.mark_resume_after_discovery()
                    print(f"[Discovery] 选中: {selected}", flush=True)
                else:
                    print("[Discovery] 未获取到 agents，将使用配置文件默认", flush=True)
        finally:
            reset_llm_timing_sink(_pre_llm_tok)
        for k, v in intent.to_extra_overrides().items():
            config.extra[k] = v
        config.extra["_preflight_llm_timing_events"] = list(preflight_llm_events)
    if args.goal is not None:
        config.extra["goal"] = args.goal
    task = load_task_spec(scenarios / "task_specs.json", config.task_spec_ids[0])
    print(f"[实验配置] {config_path.name}（goal_driven 下 goal 可由 --goal 覆盖）")

    runs_per_agent = max(1, config.extra.get("runs_per_agent") or 1)
    base_runs = args.runs if args.runs is not None else max(1, config.extra.get("runs") or 1)
    # 可选：用 B2 规则路由得到 difficulty/route_type，供多轮 query 策略使用（不用于决定轮数）
    routing_result = None
    try:
        from raft.core.difficulty import route
        routing_result = route(task, max_steps=10, llm_router=None)
    except Exception:
        pass

    effective_agent = resolve_agent_under_test(config, cli_agent=args.agent)
    config.extra["agent_under_test"] = effective_agent
    config.extra["agent_descriptor"] = f"Poffices 的 {effective_agent} Agent"
    if args.agents:
        agents_list = [a.strip() for a in args.agents.split(",") if a.strip()]
        if agents_list:
            config.extra["agents_to_test"] = agents_list
            effective_agent = agents_list[0]
            config.extra["agent_under_test"] = effective_agent
            if len(agents_list) == 1:
                config.extra["agent_descriptor"] = f"Poffices 的 {effective_agent} Agent"
            else:
                config.extra["agent_descriptor"] = f"Poffices 的 {len(agents_list)} 个 Agent（{', '.join(agents_list[:3])}{'…' if len(agents_list) > 3 else ''}）"
            # 每 Agent 独立 query：--queries "q1,q2,q3" 与 agents 一一对应
            if args.queries:
                q_list = [q.strip() for q in args.queries.split(",") if q.strip()]
                if len(q_list) == len(agents_list):
                    config.extra["queries_per_agent"] = q_list
                    print(f"[多 Agent 动态 query] 已为 {len(q_list)} 个 Agent 分别指定 query")
    # 多 Agent 且「每个 agent 跑 N 轮」：在 args.agents 之后读取最终 agents，顺序为 Agent1×N → Agent2×N
    agents_list_for_schedule = config.extra.get("agents_to_test") or []
    if isinstance(agents_list_for_schedule, list) and len(agents_list_for_schedule) > 1 and runs_per_agent > 1:
        num_runs = len(agents_list_for_schedule) * runs_per_agent
        run_schedule = [
            (agent_idx, round_idx)
            for agent_idx in range(len(agents_list_for_schedule))
            for round_idx in range(runs_per_agent)
        ]
        rounds_rationale = f"goal 解析：{len(agents_list_for_schedule)} 个 Agent，每个 {runs_per_agent} 轮（顺序 Agent1×{runs_per_agent}→Agent2×{runs_per_agent}）"
    else:
        num_runs = base_runs
        run_schedule = None
        if args.runs is not None:
            rounds_rationale = f"用户指定 {num_runs} 轮"
        elif config.extra.get("runs"):
            rounds_rationale = f"goal 解析 {num_runs} 轮"
        else:
            rounds_rationale = f"默认 {num_runs} 轮"
    if not getattr(args, "full_report", False):
        config.extra["use_llm_judge"] = False  # 默认 mini 模式：不调用 LLM 判分
    # 默认：三种模式 + use_llm_planner=True（未命中时自动 LLM 规划）；仅 --no-llm-planner 时关闭
    config.extra["use_llm_planner"] = not getattr(args, "no_llm_planner", False)
    # 强制 LLM 规划（跳过复合 Block，用于测试 LLM 规划）
    if getattr(args, "force_llm_plan", False):
        config.extra["force_llm_plan"] = True
        print("[规划] 已启用 --force-llm-plan：跳过复合 Block，由 LLM 从 allowed_blocks 组流程", flush=True)
    if args.max_replans is not None:
        config.extra["max_replans"] = max(0, args.max_replans)
    config.extra.setdefault("replan_on_failure", True)
    config.extra["orchestration_mode"] = "goal_driven"

    agent = create_poffices_agent(
        config,
        cli_agent_type=args.agent_type,
        cli_provider=args.llm_provider,
        cli_model=args.llm_model,
        default_agent_name=effective_agent,
    )
    effective_type = config.extra.get("agent_type", "rule") if args.agent_type is None else args.agent_type
    print(f"[Agent 模式] {effective_type}（agent_type 来自{'CLI 覆盖' if args.agent_type else '配置文件'}）")
    agents_list = config.extra.get("agents_to_test") or []
    if isinstance(agents_list, list) and len(agents_list) > 1:
        src = "CLI --agents" if args.agents else "配置文件"
        print(f"[待测 Agents] 共 {len(agents_list)} 个：{', '.join(agents_list[:5])}{'…' if len(agents_list) > 5 else ''}（来自 {src}）")
    else:
        src = "CLI --agent" if args.agent else ("CLI --agents" if args.agents else "配置文件")
        print(f"[待测 Agent] {effective_agent}（来自 {src}）")
    print("[编排主干] goal_driven（默认启用最新能力）")
    if not getattr(args, "full_report", False):
        print("[报告] 默认 mini 模式（无 LLM 判分/总结）；加 --full-report 可生成完整报告")

    # 多 Agent 时计划步数 = 每 Agent 3 步（app_ready, send_query, get_response），需足够 max_steps 否则会提前退出
    if isinstance(agents_list, list) and len(agents_list) > 1:
        max_steps = max(10, len(agents_list) * 3 + 4)  # +4 留给重规划等
    else:
        max_steps = 5

    successes = 0
    steps_list: list[int] = []
    results: list[dict] = []

    # 任务描述只生成一次，后续轮次复用（避免每轮调用 LLM）
    precomputed_task_description: str | None = None
    precompute_task_desc_llm_ms: int | None = None
    if config.extra.get("use_llm_task_description") and num_runs >= 1:
        from raft.core.task_description_suggester import suggest_task_description
        from raft.core.config.scenario import resolve_scenario_prompt, resolve_default_task_description

        scenario_prompt = resolve_scenario_prompt(config)
        agent_descriptor = config.extra.get("agent_descriptor") or "待测 Agent"
        goal = config.extra.get("goal") or ""
        fallback = resolve_default_task_description(config, task) or "在 Poffices 上完成一次 Query 测试"
        provider = config.extra.get("llm_provider") or config.extra.get("agent_provider") or os.environ.get("RAFT_LLM_PROVIDER")
        _t_desc = time.perf_counter()
        precomputed_task_description = suggest_task_description(
            scenario_prompt,
            agent_descriptor,
            goal=goal or None,
            fallback=fallback,
            provider=provider,
        )
        precompute_task_desc_llm_ms = int((time.perf_counter() - _t_desc) * 1000)
        if goal:
            print(f"[任务描述] 已生成（后续 {num_runs} 轮复用）: {precomputed_task_description[:50]}{'…' if len(precomputed_task_description) > 50 else ''}", flush=True)

    try:
        for i in range(num_runs):
            # 多 Agent 每 agent 多轮：本 run 只测当前 agent，previous_rounds 仅含该 agent 的历史
            current_agent = None
            if run_schedule is not None:
                agent_idx, round_idx = run_schedule[i]
                current_agent = agents_list_for_schedule[agent_idx]
                config.extra["agents_to_test"] = [current_agent]
                config.extra["agent_under_test"] = current_agent
                print(f"[run_schedule] Run {i + 1}/{num_runs}: agents_to_test={config.extra['agents_to_test']} (单 Agent 预期 3 步)", flush=True)
                config.extra["agent_descriptor"] = f"Poffices 的 {current_agent} Agent"
                # 仅传入本 agent 的 previous_rounds（多 Agent 每 agent 多轮时，每个 agent 的 round 2+ 也会用深入提问）
                prev_results_for_agent = [
                    r for j, r in enumerate(results)
                    if j < i and run_schedule[j][0] == agent_idx
                ]
            else:
                prev_results_for_agent = results

            orch = Orchestrator(
                max_steps=max_steps,
                agent=agent,
                rpa=rpa,
                orchestration_mode="goal_driven",
            )
            run_id = (
                f"poffices-run-{i + 1} · {current_agent}"
                if run_schedule is not None and current_agent
                else f"poffices-run-{i + 1}"
            )
            # 构建 query_context：B2 结果每轮带入以联动；第 2 轮起加入 previous_rounds 与策略（见 docs/ARCHITECTURE.md 第 8 节「数据流与闭环」）
            query_context: dict = {}
            if precomputed_task_description:
                query_context["task_description"] = precomputed_task_description
            if i == 0:
                ext_ev: list[dict] = []
                pf = (config.extra or {}).get("_preflight_llm_timing_events")
                if isinstance(pf, list) and pf:
                    ext_ev.extend([e for e in pf if isinstance(e, dict)])
                if precompute_task_desc_llm_ms is not None and precompute_task_desc_llm_ms > 0:
                    ext_ev.append({"label": "task_description", "elapsed_ms": int(precompute_task_desc_llm_ms)})
                if ext_ev:
                    query_context["external_llm_timing_events"] = ext_ev
            if routing_result is not None:
                query_context["b2_difficulty"] = routing_result.difficulty
                query_context["b2_route_type"] = routing_result.route_type
            if i > 0 and prev_results_for_agent:
                previous_rounds = _previous_rounds_from_results(prev_results_for_agent)
                query_context["previous_rounds"] = previous_rounds
                # 同步构建 previous_queries（query_suggester 多轮 prompt 需要此字段，缺失时退化为单轮行为）
                query_context["previous_queries"] = [r.get("query") for r in previous_rounds if r.get("query")]
                # 当前轮次与总轮数（用于 query 策略）
                if run_schedule is not None:
                    _round_idx = run_schedule[i][1]
                    _total_rounds = runs_per_agent
                else:
                    _round_idx = i
                    _total_rounds = num_runs
                # Query 策略：第 2 轮固定不同领域；第 3 轮起（总轮数≥3 时）用策略（深化/换领域）
                if _round_idx == 1:
                    query_context["multi_round_strategy"] = "diversify"
                    query_context["policy_hint"] = "本轮请换一个与第一轮完全不同的领域或话题，以考察 Agent 的多样化能力。"
                elif _round_idx >= 2 and _total_rounds >= 3:
                    if args.strategy == "rule":
                        from raft.core.query_policy import decide_next_strategy
                        strategy, policy_hint = decide_next_strategy(previous_rounds)
                        query_context["multi_round_strategy"] = strategy
                        query_context["policy_hint"] = policy_hint
                    else:
                        query_context["multi_round_strategy"] = args.strategy
            query_context = query_context if query_context else None
            result = orch.run_until_done(
                config, task, run_id=run_id, log_dir=log_dir, query_context=query_context
            )
            results.append(result)

            q = _query_from_result(result)
            metrics = result.get("metrics") or {}
            success = metrics.get("success")
            steps = result.get("steps_run", 0)
            # 多 Agent 时按 Agent 统计成功数，消除「只按 run 数」的歧义
            agents_list = config.extra.get("agents_to_test") or []
            if isinstance(agents_list, list) and len(agents_list) > 1:
                from raft.reporting.multi_agent import get_per_agent_segments
                segments = get_per_agent_segments(result)
                if segments:
                    agent_ok = sum(1 for s in segments if s.get("success", False))
                    print(f"  轮 {i + 1}/{num_runs}: 步数={steps} 按 Agent 成功 {agent_ok}/{len(segments)}（整 run: {'成功' if success else '失败'}）")
                else:
                    if q:
                        perf = "成功" if success else "失败"
                        print(f"  轮 {i + 1}/{num_runs}: query=\"{(q[:40] + '…') if len(q) > 40 else q}\" 步数={steps} {perf}")
                    else:
                        print(f"  轮 {i + 1}/{num_runs}: 步数={steps} {'成功' if success else '失败'}")
            else:
                if q:
                    perf = "成功" if success else "失败"
                    print(f"  轮 {i + 1}/{num_runs}: query=\"{(q[:40] + '…') if len(q) > 40 else q}\" 步数={steps} {perf}")
                else:
                    print(f"  轮 {i + 1}/{num_runs}: 步数={steps} {'成功' if success else '失败'}")

            metrics = result.get("metrics")
            if metrics:
                if metrics.get("success"):
                    successes += 1
                steps_list.append(metrics.get("step_count", 0))

        print("\n=== Poffices 运行汇总 ===")
        print(f"成功率: {successes}/{num_runs} = {100 * successes / num_runs:.0f}%")
        if run_schedule is not None and results:
            print(f"按 Agent 计: 成功 {successes}/{num_runs}（共 {num_runs} 个 run，顺序 Agent1×{runs_per_agent}→Agent2×{runs_per_agent}）")
        elif isinstance(agents_list, list) and len(agents_list) > 1 and results:
            from raft.reporting.multi_agent import get_per_agent_segments
            total_agents = 0
            agent_ok_total = 0
            for r in results:
                segs = get_per_agent_segments(r)
                if segs:
                    total_agents += len(segs)
                    agent_ok_total += sum(1 for s in segs if s.get("success", False))
            if total_agents > 0:
                print(f"本 run 含多 Agent 时按 Agent 计: 成功 {agent_ok_total}/{total_agents}（共 {num_runs} 个 run）")
        avg_steps = sum(steps_list) / len(steps_list) if steps_list else 0
        print(f"平均步骤数: {avg_steps:.1f}")
        print(f"轨迹日志目录: {log_dir}")

        if results:
            task_for_report = results[-1].get("task_spec_effective") or task.model_dump()
            config_dump = config.model_dump()
            report_path = log_dir / "run_report.html"

            use_full_report = getattr(args, "full_report", False)
            out = build_report_with_llm(
                results,
                config_dump,
                task_for_report,
                output_path=report_path,
                use_llm_summary=use_full_report,
                rounds_rationale=rounds_rationale,
                minimal_report=not use_full_report,
            )
            if out.get("llm_summary"):
                print("已生成 LLM 分析总结。")
            mode_hint = "完整" if use_full_report else "mini"
            print(f"报告（{mode_hint} 模式）: {out.get('output_path') or report_path}")

        # 仅对失败轮次打印工具链，方便定位问题；全部成功时不打印冗余信息
        failed_rounds = [
            (i, r) for i, r in enumerate(results)
            if not (r.get("metrics") or {}).get("success", True)
        ]
        if failed_rounds:
            print("\n失败轮次工具链:")
            for i, r in failed_rounds:
                traj = r.get("trajectory") or []
                tools = []
                for e in traj:
                    for tc in (e.get("step_result") or {}).get("tool_calls") or []:
                        tools.append(tc.get("tool_name", "?"))
                print(f"  轮 {i + 1}: {' → '.join(tools)}")
    finally:
        rpa.close()
    print("已关闭浏览器。")


if __name__ == "__main__":
    main()
