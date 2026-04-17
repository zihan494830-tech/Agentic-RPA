"""B9 Orchestrator：闭环 1；支持 DAG 驱动与单/多 Agent 编排；支持 RPA 故障注入（rpa_config）与 B8 扩展落盘。"""
import uuid
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Callable, Literal

from raft.contracts.models import DifficultyRoutingResult, ExecutionResult, TaskSpec, ToolCall
from raft.core.config.models import ExperimentConfig
from raft.core.config.scenario import (
    get_scenario_spec,
    resolve_block_catalog,
    resolve_block_semantics_for_planner,
    resolve_compound_blocks,
    resolve_constraints,
    resolve_default_task_description,
    resolve_flow_template,
    resolve_scenario_prompt,
    resolve_suggested_agents,
    resolve_planner_hints,
    validate_scenario_run,
)
from raft.core.llm_timing import attach_llm_timing_sink, reset_llm_timing_sink
from raft.core.state.manager import StateAndTrajectoryManager
from raft.core.difficulty import route as b2_route
from raft.core.dag import build_dag, get_next_steps
from raft.core.planner import (
    DAGScheduler,
    build_goal_plan,
    build_recovery_plan,
    linearize_goal_plan,
    parse_goal,
)
from raft.core.planner.goal_intent import goal_intent_from_dict
from raft.core.planner.gate_checker import check_gate
from raft.core.scheduler import assign_step
from raft.agents.mock_agent import MockAgent
from raft.agents.multi_agent import MultiAgentRegistry
from raft.rpa import get_default_rpa, wrap_rpa_with_fault_injection
from raft.rpa.mock_rpa import MockRPA
from raft.evaluation.metrics import evaluate_trajectory, write_trajectory_log

import logging as _logging

_logger = _logging.getLogger(__name__)

OrchestrationMode = Literal["single_agent", "multi_agent_dag", "goal_driven"]


def _safe_rpa_execute(rpa: Any, step_index: int, tc: ToolCall) -> ExecutionResult:
    """安全执行 RPA，捕获所有未处理异常并转为 ExecutionResult(success=False)，防止单步崩溃中断整个 run。"""
    try:
        return rpa.execute(step_index, tc)
    except Exception as exc:
        _logger.error(
            "[B7] RPA 执行异常 step=%s tool=%s: %s: %s",
            step_index,
            tc.tool_name,
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        return ExecutionResult(
            success=False,
            error_type="runtime_error",
            raw_response=f"RPA 执行异常({type(exc).__name__}): {exc}",
            tool_name=tc.tool_name,
        )


class Orchestrator:
    """
    Orchestrator：串联 B2–B7，闭环 1；
    支持 single_agent（线性单 Agent）与 multi_agent_dag（DAG + 多 Agent 编排）。
    可选 B8 轨迹落盘与评估。
    """

    def __init__(
        self,
        *,
        max_steps: int = 5,
        agent: Any = None,
        rpa: Any = None,
        mock_agent: MockAgent | None = None,
        mock_rpa: MockRPA | None = None,
        orchestration_mode: OrchestrationMode = "single_agent",
        multi_agent_registry: MultiAgentRegistry | None = None,
        routing_llm: Callable[[TaskSpec], DifficultyRoutingResult] | None = None,
        human_confirm_fn: Callable[[Any, Any], bool] | None = None,
    ) -> None:
        self.max_steps = max_steps
        self.orchestration_mode = orchestration_mode
        self.agent = agent or mock_agent or MockAgent()
        self.rpa = rpa or mock_rpa or get_default_rpa()
        self.b5 = StateAndTrajectoryManager()
        self.multi_agent_registry = multi_agent_registry
        self.routing_llm = routing_llm  # B2 可选：LLM 辅助编排决策（single/multi 路由）
        self.human_confirm_fn = human_confirm_fn
        if orchestration_mode == "multi_agent_dag" and multi_agent_registry is None:
            self.multi_agent_registry = MultiAgentRegistry()

    def _prepare_rpa(self, experiment_config: "ExperimentConfig") -> Any:
        """B9 的 B1→B7 配置绑定点：读取 B1 实验配置中的 rpa_config，
        按需用 FaultInjectionRPA 包装底层 RPA 适配器。
        - mode=normal 且无 fault_injection 时直接返回原 rpa（零开销）
        - mode=robustness/stress 或显式配置 fault_injection 时返回包装器
        集中管理此处以避免在三个执行路径中重复相同逻辑。
        """
        rpa_config = (getattr(experiment_config, "extra", None) or {}).get("rpa_config")
        return wrap_rpa_with_fault_injection(self.rpa, rpa_config)

    @staticmethod
    def _merge_llm_timing_into_out(out: dict, query_context: dict | None, llm_sink: list[dict]) -> None:
        events: list[dict] = list(llm_sink)
        ext = (query_context or {}).get("external_llm_timing_events")
        if isinstance(ext, list):
            for e in ext:
                if isinstance(e, dict) and isinstance(e.get("elapsed_ms"), (int, float)):
                    events.append(dict(e))
        if events:
            out["llm_timing_events"] = events

    def run_until_done(
        self,
        experiment_config: ExperimentConfig,
        task_spec: TaskSpec,
        *,
        run_id: str | None = None,
        log_dir: Path | str | None = None,
        query_context: dict | None = None,
    ) -> dict:
        """
        加载 Config + TaskSpec，按 orchestration_mode 运行闭环 1；
        single_agent：线性步进、单 Agent；
        multi_agent_dag：B2 路由 → B3 DAG → 每轮 next_steps → B4 分配 Agent → 执行 → B5 更新。
        可选轨迹落盘与 B8 评估。
        query_context：多轮时可选，含 previous_rounds、previous_queries、multi_round_strategy、policy_hint；可选 b2_difficulty、b2_route_type（来自 B2，供出题/策略与 B2 联动，见 docs/ARCHITECTURE.md 第 8 节「数据流与闭环」）。
        """
        if self.orchestration_mode == "multi_agent_dag":
            return self._run_multi_agent_dag(
                experiment_config,
                task_spec,
                run_id=run_id,
                log_dir=log_dir,
                query_context=query_context,
            )
        if self.orchestration_mode == "goal_driven":
            return self._run_goal_driven(
                experiment_config,
                task_spec,
                run_id=run_id,
                log_dir=log_dir,
                query_context=query_context,
            )
        return self._run_single_agent(
            experiment_config,
            task_spec,
            run_id=run_id,
            log_dir=log_dir,
            query_context=query_context,
        )

    def _get_initial_state_for_run(
        self,
        experiment_config: ExperimentConfig,
        task_spec: TaskSpec,
        *,
        query_context: dict | None = None,
    ) -> tuple[dict, TaskSpec, str | None]:
        """
        若启用 use_llm_task_description，先用 LLM 生成任务描述；
        若启用 use_llm_query，再用（可能已更新的）任务描述调用 Query 建议器生成 query。
        多轮时可通过 query_context 传入 previous_rounds/previous_queries，并返回 query_rationale 供报告展示。
        返回 (initial_state, effective_task_spec, query_rationale)。
        """
        import os

        validate_scenario_run(experiment_config, task_spec)
        extra = getattr(experiment_config, "extra", None) or {}
        agent_descriptor = extra.get("agent_descriptor") or "待测 Agent（如 Poffices 页面产品）"
        provider = extra.get("llm_provider") or extra.get("agent_provider") or os.environ.get("RAFT_LLM_PROVIDER")
        goal = extra.get("goal") or ""
        query_rationale: str | None = None
        scenario_prompt = resolve_scenario_prompt(experiment_config)
        fallback_description = (
            resolve_default_task_description(experiment_config, task_spec)
            or "在 Poffices 上完成一次 Query 测试"
        )

        # 第一段：可选 LLM 生成任务描述（支持 query_context 传入预生成，避免每轮重复调用）
        effective_task_spec = task_spec
        precomputed_desc = (query_context or {}).get("task_description")
        if extra.get("use_llm_task_description"):
            if precomputed_desc and str(precomputed_desc).strip():
                effective_task_spec = task_spec.model_copy(update={"description": str(precomputed_desc).strip()})
            else:
                from raft.core.task_description_suggester import suggest_task_description

                if goal:
                    print("[LLM] 正在根据 goal 生成任务描述…", flush=True)
                generated_desc = suggest_task_description(
                    scenario_prompt,
                    agent_descriptor,
                    goal=goal or None,
                    fallback=fallback_description,
                    provider=provider,
                )
                effective_task_spec = task_spec.model_copy(update={"description": generated_desc})
                if goal:
                    print(f"      任务描述: {generated_desc[:60]}{'…' if len(generated_desc) > 60 else ''}", flush=True)
        elif fallback_description and fallback_description != task_spec.description:
            effective_task_spec = task_spec.model_copy(update={"description": fallback_description})

        # 第二段：可选 LLM 生成 query，并合并进 initial_state；多轮时一并获取选择思路供报告展示
        initial_state = dict(task_spec.initial_state)
        if extra.get("use_llm_query"):
            qctx = query_context or {}
            prev_rounds = qctx.get("previous_rounds")
            prev_queries = qctx.get("previous_queries")
            strategy = qctx.get("multi_round_strategy", "auto")
            if strategy not in ("deepen", "diversify", "auto"):
                strategy = "auto"
            policy_hint = qctx.get("policy_hint")
            collaboration_mode = bool(extra.get("collaboration_mode"))
            agents_to_test = extra.get("agents_to_test")
            if not isinstance(agents_to_test, list) or not agents_to_test:
                agents_to_test = resolve_suggested_agents(experiment_config)
            # 多 Agent（含协作）且本 run 测多个 Agent：优先为每个 Agent 生成一条 query。
            # 非协作模式直接使用 queries_per_agent；协作模式则先保留 per-agent query，再合成为兼容旧接口的一条总 query。
            if (
                isinstance(agents_to_test, list)
                and len(agents_to_test) > 1
            ):
                from raft.core.query_suggester import suggest_queries_for_agents, synthesize_collaboration_query
                agents_filtered = [a for a in agents_to_test if isinstance(a, str) and a.strip()]
                if len(agents_filtered) >= 2:
                    print(
                        f"[LLM] 正在根据 goal 为 {len(agents_filtered)} 个 Agent 生成"
                        + ("协作 query" if collaboration_mode else "测试 query")
                        + "（调用 LLM，请稍候）…",
                        flush=True,
                    )
                    qlist = suggest_queries_for_agents(
                        effective_task_spec,
                        agent_descriptor,
                        agents_filtered,
                        scenario_context=scenario_prompt,
                        goal=goal or None,
                        provider=provider,
                    )
                    if qlist and len(qlist) == len(agents_filtered):
                        initial_state["queries_per_agent"] = [str(q).strip() for q in qlist if q]
                        if collaboration_mode:
                            initial_state["query"] = synthesize_collaboration_query(
                                agents_filtered,
                                initial_state["queries_per_agent"],
                                fallback_query=initial_state.get("query"),
                            )
                            print(f"      已生成 {len(qlist)} 条 per-agent query，并合成为协作总 query", flush=True)
                        else:
                            initial_state["query"] = initial_state["queries_per_agent"][0]
                            print(f"      已生成 {len(qlist)} 条 query", flush=True)
                    else:
                        # 回退：只生成一条
                        from raft.core.query_suggester import suggest_query
                        query = suggest_query(
                            effective_task_spec,
                            agent_descriptor,
                            scenario_context=scenario_prompt,
                            goal=goal or None,
                            provider=provider,
                            previous_queries=None,
                            previous_rounds=None,
                            multi_round_strategy=strategy,
                            policy_hint=policy_hint,
                        )
                        initial_state = {**initial_state, "query": query}
                else:
                    from raft.core.query_suggester import suggest_query
                    query = suggest_query(
                        effective_task_spec,
                        agent_descriptor,
                        scenario_context=scenario_prompt,
                        goal=goal or None,
                        provider=provider,
                        previous_queries=None,
                        previous_rounds=None,
                        multi_round_strategy=strategy,
                        policy_hint=policy_hint,
                    )
                    initial_state = {**initial_state, "query": query}
            elif prev_rounds or prev_queries:
                print("[LLM] 正在根据上一轮表现生成 query（深入提问）…", flush=True)
                from raft.core.query_suggester import suggest_query_with_rationale
                query, query_rationale = suggest_query_with_rationale(
                    effective_task_spec,
                    agent_descriptor,
                    scenario_context=scenario_prompt,
                    goal=goal or None,
                    provider=provider,
                    previous_queries=prev_queries if isinstance(prev_queries, list) else None,
                    previous_rounds=prev_rounds if isinstance(prev_rounds, list) else None,
                    multi_round_strategy=strategy,
                    policy_hint=policy_hint,
                )
                initial_state = {**initial_state, "query": query}
            else:
                from raft.core.query_suggester import suggest_query
                print("[LLM] 正在根据 goal 生成" + ("协作 " if collaboration_mode else "测试 ") + "query（调用 LLM，请稍候）…", flush=True)
                query = suggest_query(
                    effective_task_spec,
                    agent_descriptor,
                    scenario_context=scenario_prompt,
                    goal=goal or None,
                    provider=provider,
                    previous_queries=None,
                    previous_rounds=None,
                    multi_round_strategy=strategy,
                    policy_hint=policy_hint,
                )
                initial_state = {**initial_state, "query": query}
                print(f"      已生成 query", flush=True)

        return (initial_state, effective_task_spec, query_rationale)

    def _run_single_agent(
        self,
        experiment_config: ExperimentConfig,
        task_spec: TaskSpec,
        *,
        run_id: str | None = None,
        log_dir: Path | str | None = None,
        query_context: dict | None = None,
    ) -> dict:
        """单 Agent 线性闭环；可按 rpa_config 包装故障注入 RPA。"""
        if run_id is None and log_dir is not None:
            run_id = str(uuid.uuid4())
        llm_sink: list[dict] = []
        llm_token = attach_llm_timing_sink(llm_sink)
        try:
            return self._run_single_agent_body(
                experiment_config,
                task_spec,
                run_id=run_id,
                log_dir=log_dir,
                query_context=query_context,
                llm_sink=llm_sink,
            )
        finally:
            reset_llm_timing_sink(llm_token)

    def _run_single_agent_body(
        self,
        experiment_config: ExperimentConfig,
        task_spec: TaskSpec,
        *,
        run_id: str | None = None,
        log_dir: Path | str | None = None,
        query_context: dict | None = None,
        llm_sink: list[dict],
    ) -> dict:
        rpa = self._prepare_rpa(experiment_config)
        rpa_mode = _rpa_mode_from_config(experiment_config)
        initial_state, effective_task_spec, query_rationale = self._get_initial_state_for_run(
            experiment_config, task_spec, query_context=query_context
        )
        self.b5.update_state(
            current_step_index=0,
            last_execution_result=None,
            state_delta=initial_state,
        )
        trajectory_snapshots: list[dict] = []

        for step in range(self.max_steps):
            self.b5.update_state(current_step_index=step)
            agent_input_context = self.b5.get_agent_input_context()
            # 快照语义：记录 Agent 在执行 step 前所看到的输入状态（含上一步的 ExecutionResult）。
            # trajectory_snapshots[i] == Agent 决策 step i 时的输入；trajectory[i] == step i 执行后的结果。
            trajectory_snapshots.append(dict(agent_input_context))

            tool_calls = self.agent.run(
                agent_input_context=agent_input_context,
                task_description=effective_task_spec.description,
            )
            if not tool_calls:
                break

            execution_results: list[ExecutionResult] = []
            for tc in tool_calls:
                started = time.perf_counter()
                er = _safe_rpa_execute(rpa, step, tc)
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                er.extra = dict(er.extra or {})
                er.extra["elapsed_ms"] = elapsed_ms
                execution_results.append(er)
                if not er.success:
                    _raw = str(er.raw_response or "")
                    print(
                        f"  [step {step}] ✗ {tc.tool_name}"
                        f"（{er.error_type or 'failed'}）: {_raw[:80]}{'…' if len(_raw) > 80 else ''}",
                        flush=True,
                    )

            self.b5.record_step(
                step_index=step,
                tool_calls=tool_calls,
                execution_results=execution_results,
                agent_input_snapshot=agent_input_context,
            )

        step1_input_has_step0_result = False
        if len(trajectory_snapshots) >= 2:
            step1_input = trajectory_snapshots[1]
            step1_input_has_step0_result = "last_execution_result" in step1_input

        trajectory_serialized = self.b5.serialize_trajectory()
        out: dict = {
            "trajectory": trajectory_serialized,
            "trajectory_snapshots": trajectory_snapshots,
            "step2_agent_input_contains_step1_execution_result": step1_input_has_step0_result,
            "steps_run": len(self.b5.trajectory),
            "orchestration_mode": "single_agent",
            "task_spec_effective": effective_task_spec.model_dump(),
        }
        if query_rationale is not None:
            out["query_rationale"] = query_rationale
        out = self._attach_log_and_metrics(
            out, experiment_config, effective_task_spec, run_id, log_dir, rpa_mode=rpa_mode
        )
        self._merge_llm_timing_into_out(out, query_context, llm_sink)
        return out

    def _run_multi_agent_dag(
        self,
        experiment_config: ExperimentConfig,
        task_spec: TaskSpec,
        *,
        run_id: str | None = None,
        log_dir: Path | str | None = None,
        query_context: dict | None = None,
    ) -> dict:
        """DAG 驱动：B2 → B3 → 每轮 next_steps → B4 分配 Agent → 执行 → B5；可按 rpa_config 包装故障注入 RPA。"""
        if run_id is None and log_dir is not None:
            run_id = str(uuid.uuid4())
        llm_sink: list[dict] = []
        llm_token = attach_llm_timing_sink(llm_sink)
        try:
            return self._run_multi_agent_dag_body(
                experiment_config,
                task_spec,
                run_id=run_id,
                log_dir=log_dir,
                query_context=query_context,
                llm_sink=llm_sink,
            )
        finally:
            reset_llm_timing_sink(llm_token)

    def _run_multi_agent_dag_body(
        self,
        experiment_config: ExperimentConfig,
        task_spec: TaskSpec,
        *,
        run_id: str | None = None,
        log_dir: Path | str | None = None,
        query_context: dict | None = None,
        llm_sink: list[dict],
    ) -> dict:
        rpa = self._prepare_rpa(experiment_config)
        rpa_mode = _rpa_mode_from_config(experiment_config)
        initial_state, effective_task_spec, query_rationale = self._get_initial_state_for_run(
            experiment_config, task_spec, query_context=query_context
        )
        self.b5.update_state(
            current_step_index=0,
            last_execution_result=None,
            state_delta=initial_state,
        )
        routing = b2_route(
            effective_task_spec,
            max_steps=self.max_steps,
            llm_router=getattr(self, "routing_llm", None),
        )
        dag = build_dag(
            effective_task_spec, routing.route_type, max_steps=self.max_steps
        )
        completed_steps: set[int] = set()
        trajectory_snapshots: list[dict] = []

        while True:
            next_steps = get_next_steps(dag, completed_steps)
            if not next_steps:
                break
            step_index = min(next_steps)
            assignment = assign_step(step_index, dag_nodes=dag.nodes)
            self.b5.update_state(current_step_index=step_index)
            agent_input_context = self.b5.get_agent_input_context()
            # 快照语义：执行前的 Agent 输入状态（含上一步 ExecutionResult）；与 trajectory[i] 对应但时序早于执行。
            trajectory_snapshots.append(dict(agent_input_context))

            tool_calls = self.multi_agent_registry.run(
                assignment.agent_role,
                agent_input_context,
                effective_task_spec.description,
            )
            if tool_calls:
                execution_results = []
                for tc in tool_calls:
                    started = time.perf_counter()
                    er = _safe_rpa_execute(rpa, step_index, tc)
                    elapsed_ms = int((time.perf_counter() - started) * 1000)
                    er.extra = dict(er.extra or {})
                    er.extra["elapsed_ms"] = elapsed_ms
                    execution_results.append(er)
                self.b5.record_step(
                    step_index=step_index,
                    tool_calls=tool_calls,
                    execution_results=execution_results,
                    agent_input_snapshot=agent_input_context,
                )
            completed_steps.add(step_index)
            if len(completed_steps) >= len(dag.nodes):
                break

        step1_has_step0 = False
        if len(trajectory_snapshots) >= 2:
            step1_has_step0 = "last_execution_result" in trajectory_snapshots[1]

        trajectory_serialized = self.b5.serialize_trajectory()
        out = {
            "trajectory": trajectory_serialized,
            "trajectory_snapshots": trajectory_snapshots,
            "step2_agent_input_contains_step1_execution_result": step1_has_step0,
            "steps_run": len(self.b5.trajectory),
            "orchestration_mode": "multi_agent_dag",
            "route_type": routing.route_type,
            "dag_nodes": dag.nodes,
            "dag_edges": dag.edges,
            "task_spec_effective": effective_task_spec.model_dump(),
        }
        if query_rationale is not None:
            out["query_rationale"] = query_rationale
        out = self._attach_log_and_metrics(
            out, experiment_config, effective_task_spec, run_id, log_dir, rpa_mode=rpa_mode
        )
        self._merge_llm_timing_into_out(out, query_context, llm_sink)
        return out

    def _run_goal_driven(
        self,
        experiment_config: ExperimentConfig,
        task_spec: TaskSpec,
        *,
        run_id: str | None = None,
        log_dir: Path | str | None = None,
        query_context: dict | None = None,
    ) -> dict:
        """
        目标驱动编排（L3）：先生成结构化计划，再执行；失败时可触发重规划恢复。
        """
        llm_sink: list[dict] = []
        llm_token = attach_llm_timing_sink(llm_sink)
        try:
            return self._run_goal_driven_inner(
                experiment_config,
                task_spec,
                run_id=run_id,
                log_dir=log_dir,
                query_context=query_context,
                llm_sink=llm_sink,
            )
        finally:
            reset_llm_timing_sink(llm_token)

    def _run_goal_driven_inner(
        self,
        experiment_config: ExperimentConfig,
        task_spec: TaskSpec,
        *,
        run_id: str | None = None,
        log_dir: Path | str | None = None,
        query_context: dict | None = None,
        llm_sink: list[dict],
    ) -> dict:
        if run_id is None and log_dir is not None:
            run_id = str(uuid.uuid4())
        rpa = self._prepare_rpa(experiment_config)
        rpa_mode = _rpa_mode_from_config(experiment_config)
        initial_state, effective_task_spec, query_rationale = self._get_initial_state_for_run(
            experiment_config, task_spec, query_context=query_context
        )
        self.b5.update_state(
            current_step_index=0,
            last_execution_result=None,
            state_delta=initial_state,
        )
        extra_cfg = getattr(experiment_config, "extra", None) or {}
        block_catalog = resolve_block_catalog(experiment_config)
        if not block_catalog:
            # 没有显式 block_catalog 时，尽量用默认通用块完成最小闭环（含 wait_output_complete 供 get_response 失败时恢复）
            block_catalog = [
                {"block_id": "app_ready", "params": {"options": "可选"}},
                {"block_id": "send_query", "params": {"query": "string"}},
                {"block_id": "get_response", "params": {}},
                {"block_id": "wait_output_complete", "params": {"timeout_sec": "可选"}},
                {"block_id": "refresh_page", "params": {}},
            ]

        # 将待测 Agent 名称注入 planner 上下文，便于 app_ready 生成 options.agent_name
        planning_state = dict(initial_state)
        if isinstance(extra_cfg.get("agent_under_test"), str):
            planning_state["agent_name"] = extra_cfg["agent_under_test"]
        elif "agent_name" not in planning_state:
            suggested_agents = resolve_suggested_agents(experiment_config)
            if len(suggested_agents) == 1:
                planning_state["agent_name"] = suggested_agents[0]
        # 多 Agent 目标：若配置了 agents_to_test，传入规划器以生成多段计划
        agents_to_test = extra_cfg.get("agents_to_test")
        if not isinstance(agents_to_test, list) or not agents_to_test:
            suggested_agents = resolve_suggested_agents(experiment_config)
            agents_to_test = suggested_agents if len(suggested_agents) > 1 else None
        if isinstance(agents_to_test, list) and agents_to_test:
            planning_state["agents_to_test"] = [a for a in agents_to_test if isinstance(a, str) and a.strip()]
            if len(planning_state.get("agents_to_test", [])) == 1:
                planning_state["agent_name"] = planning_state["agents_to_test"][0]
            # 每 Agent 不同 query：queries_per_agent 与 agents_to_test 一一对应（优先用 initial_state，如 LLM 多 Agent 出题结果）
            # collaboration_mode 时只用一条 query，不设 queries_per_agent
            if not extra_cfg.get("collaboration_mode"):
                qpa = initial_state.get("queries_per_agent") or extra_cfg.get("queries_per_agent")
                if isinstance(qpa, list) and len(qpa) == len(planning_state["agents_to_test"]):
                    planning_state["queries_per_agent"] = [str(q).strip() for q in qpa if q]
            planning_state["collaboration_mode"] = bool(extra_cfg.get("collaboration_mode"))

        planner_hints = resolve_planner_hints(experiment_config)
        planner_provider = extra_cfg.get("llm_provider") or extra_cfg.get("agent_provider")
        goal_text = extra_cfg.get("goal") or None
        if bool(extra_cfg.get("post_discovery_resume")):
            planning_state["post_discovery_resume"] = True
        # 入口已注入 planner_goal_intent（Discovery / Goal Interpreter）时不再重复 parse_goal，避免歧义与重复 LLM
        intent_for_plan = None
        pg = extra_cfg.get("planner_goal_intent")
        if isinstance(pg, dict) and pg:
            intent_for_plan = goal_intent_from_dict(pg)
        elif goal_text:
            intent_for_plan = parse_goal(
                goal_text,
                provider=planner_provider or None,
                model=extra_cfg.get("llm_model") or extra_cfg.get("agent_model") or None,
            )
        goal_intent = intent_for_plan
        # run_schedule 每轮只测一个 Agent 时，收窄硬约束，避免 planner_goal_intent 仍写「多个 Agent」与 agents_to_test 冲突
        _ats = planning_state.get("agents_to_test")
        if (
            intent_for_plan is not None
            and isinstance(_ats, list)
            and len(_ats) == 1
            and isinstance(_ats[0], str)
            and _ats[0].strip()
        ):
            _only = _ats[0].strip()
            _ec = list(intent_for_plan.execution_constraints)
            if _ec:
                _ec[0] = (
                    f"仅使用待测 Agent: {_only}（本 run 仅测该 Agent；不得更换为其他 Agent）。"
                )
            intent_for_plan = replace(intent_for_plan, execution_constraints=_ec)
            goal_intent = intent_for_plan
        print("[RPA] 正在构建执行计划…", flush=True)
        plan = build_goal_plan(
            block_catalog=block_catalog,
            initial_state=planning_state,
            task_description=effective_task_spec.description,
            compound_blocks=resolve_compound_blocks(experiment_config),
            use_llm_planner=bool(extra_cfg.get("use_llm_planner", True)),
            goal=goal_text,
            llm_provider=planner_provider or None,
            llm_model=extra_cfg.get("llm_model") or extra_cfg.get("agent_model") or None,
            flow_template=resolve_flow_template(experiment_config),
            constraints=resolve_constraints(experiment_config),
            scenario_context=resolve_scenario_prompt(experiment_config),
            block_semantics=resolve_block_semantics_for_planner(experiment_config),
            use_template_as_hint=bool(planner_hints.get("use_template_as_hint", True)),
            intent_override=intent_for_plan,
        )
        planned_calls = linearize_goal_plan(plan)
        n_agents = len(planning_state.get("agents_to_test") or [])
        expected = 3 if n_agents <= 1 else n_agents * 3
        print(f"      计划步数: {len(planned_calls)}（预期 {'单 Agent 3 步' if n_agents <= 1 else f'{n_agents} Agent × 3 步'}）", flush=True)

        trajectory_snapshots: list[dict] = []
        plan_history: list[dict[str, Any]] = [
            {
                "source": plan.source,
                "reason": plan.reason,
                "steps": [x.model_dump() for x in plan.steps],
            }
        ]
        replan_count = 0
        max_replans = int(extra_cfg.get("max_replans", 2) or 2)
        replan_on_failure = bool(extra_cfg.get("replan_on_failure", True))
        # 重试步数预算：失败恢复插入的步骤与正常步骤共用总步数
        retry_step_budget = int(extra_cfg.get("retry_step_budget", 0)) or max(4, 2 * max_replans + 2)
        effective_max_steps = max(self.max_steps, len(planned_calls) + retry_step_budget)

        # 多 Agent 进度展示
        agents_list_for_progress = extra_cfg.get("agents_to_test") if isinstance(extra_cfg.get("agents_to_test"), list) else []
        total_agents_for_progress = len(agents_list_for_progress) if len(agents_list_for_progress) > 1 else 0
        agent_segment_index = 0
        current_agent_name_for_progress: str | None = None
        progress_header_printed = False

        # DAG 感知调度器：替代 FIFO queue，按依赖驱动执行顺序
        scheduler = DAGScheduler(plan)
        step_index = 0
        waiting_human_gate: dict[str, Any] | None = None
        while not scheduler.is_done() and step_index < effective_max_steps:
            ready_steps = scheduler.next_ready()
            if not ready_steps:
                # 所有剩余步骤被 SKIPPED 或无可执行步骤
                break
            # 当前阶段单线程取第一个就绪步骤执行（保持兼容）
            current_step = ready_steps[0]
            tc = ToolCall(
                tool_name=current_step.tool_call.tool_name,
                params=dict(current_step.tool_call.params or {}),
                step_id=current_step.step_id,
            )
            scheduler.mark_running(current_step.step_id)

            self.b5.update_state(current_step_index=step_index)
            agent_input_context = self.b5.get_agent_input_context()
            # 快照语义：执行前的 Agent 输入状态（含上一步 ExecutionResult）；与 trajectory[i] 对应但时序早于执行。
            trajectory_snapshots.append(dict(agent_input_context))

            # 每步执行前：打印即将执行的工具名及关键参数
            _tc_label = tc.tool_name
            if tc.tool_name == "app_ready":
                _ag = ((tc.params or {}).get("options") or {}).get("agent_name") or (tc.params or {}).get("agent_name")
                if _ag:
                    _tc_label = f"app_ready ({_ag})"
            elif tc.tool_name == "send_query":
                _q = (tc.params or {}).get("query", "")
                _tc_label = f'send_query: "{_q[:35]}{"…" if len(_q) > 35 else ""}"'
            print(f"  → {_tc_label}…", flush=True)

            started = time.perf_counter()
            er = _safe_rpa_execute(rpa, step_index, tc)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            er.extra = dict(er.extra or {})
            er.extra["elapsed_ms"] = elapsed_ms

            # 每步执行后：打印结果
            _step_status = "✓" if er.success else f"✗ ({er.error_type or 'failed'})"
            print(f"  {_step_status}  {elapsed_ms}ms", flush=True)

            self.b5.record_step(
                step_index=step_index,
                tool_calls=[tc],
                execution_results=[er],
                agent_input_snapshot=agent_input_context,
            )

            # gate 验收：在执行结果出来后对当前步骤做审核
            try:
                gate_result = check_gate(current_step, er, human_confirm_fn=self.human_confirm_fn)
            except Exception as _gate_exc:
                _logger.warning("[Gate] check_gate 异常，保守放行步骤 %s: %s", current_step.step_id, _gate_exc)
                from raft.core.planner.gate_checker import GateResult as _GateResult
                gate_result = _GateResult(passed=True, action="continue", reason=f"gate异常放行: {_gate_exc}")
            if not gate_result.passed and gate_result.action == "wait_human":
                scheduler.mark_waiting_human(current_step.step_id)
                waiting_human_gate = {
                    "step_id": current_step.step_id,
                    "tool_name": tc.tool_name,
                    "reason": gate_result.reason,
                    "details": gate_result.details,
                }
                break

            # 多 Agent 进度输出
            collaboration_mode = bool(extra_cfg.get("collaboration_mode"))
            if collaboration_mode and tc.tool_name == "agent_master_run_flow_once":
                if not progress_header_printed:
                    print("多 Agent 协作进度：", flush=True)
                    progress_header_printed = True
                status = "成功" if er.success else "失败"
                print(f"  协作流程（{len(agents_list_for_progress)} 个 Agent）: {status}", flush=True)
            elif total_agents_for_progress > 0 and not collaboration_mode:
                if tc.tool_name == "app_ready":
                    opts = (tc.params or {}).get("options")
                    if isinstance(opts, dict):
                        current_agent_name_for_progress = opts.get("agent_name") or None
                    if not current_agent_name_for_progress and agent_segment_index < len(agents_list_for_progress):
                        current_agent_name_for_progress = agents_list_for_progress[agent_segment_index]
                    agent_segment_index += 1
                elif tc.tool_name == "get_response" and current_agent_name_for_progress:
                    if not progress_header_printed:
                        print("多 Agent 进度：", flush=True)
                        progress_header_printed = True
                    status = "成功" if er.success else "失败"
                    print(f"  Agent {agent_segment_index}/{total_agents_for_progress}（{current_agent_name_for_progress}）: {status}", flush=True)

            # 执行失败或 gate 要求重规划：局部子图 replan
            step_failed = (not er.success) or (not gate_result.passed and gate_result.action == "replan")
            if step_failed and replan_on_failure and replan_count < max_replans:
                scheduler.mark_failed(current_step.step_id, skip_downstream=True)
                print(
                    f"  [重规划 {replan_count + 1}/{max_replans}] '{tc.tool_name}' 失败"
                    f"（{er.error_type or 'unknown'}），生成恢复计划…",
                    flush=True,
                )
                recovery = build_recovery_plan(
                    failed_tool_name=tc.tool_name,
                    block_catalog=block_catalog,
                    initial_state=dict(self.b5.state.state),
                    task_description=effective_task_spec.description,
                    failed_tool_call=tc,
                    failed_execution_result=er,
                    use_llm_planner=bool(extra_cfg.get("use_llm_planner", True)),
                    llm_provider=planner_provider or None,
                    llm_model=extra_cfg.get("llm_model") or extra_cfg.get("agent_model") or None,
                    flow_template=resolve_flow_template(experiment_config),
                    constraints=resolve_constraints(experiment_config),
                    block_semantics=resolve_block_semantics_for_planner(experiment_config),
                )
                from raft.contracts.models import GoalPlanStep as _GPS
                patched_steps: list[_GPS] = []
                for i, rs in enumerate(recovery.steps):
                    new_sid = f"r{replan_count}_{i}"
                    patched_steps.append(
                        _GPS(
                            step_id=new_sid,
                            tool_call=rs.tool_call,
                            depends_on=list(current_step.depends_on) if i == 0 else [],
                            note=rs.note,
                            expected_output=rs.expected_output,
                            gate=rs.gate,
                            risk_level=rs.risk_level,
                        )
                    )
                # 建立恢复步骤内部线性依赖
                for i in range(1, len(patched_steps)):
                    patched_steps[i].depends_on = [patched_steps[i - 1].step_id]
                if patched_steps:
                    scheduler.inject_steps(patched_steps)
                    replan_count += 1
                    _recovery_tools = [s.tool_call.tool_name for s in patched_steps if s.tool_call]
                    print(
                        f"      已注入 {len(patched_steps)} 个恢复步骤: {' → '.join(_recovery_tools)}",
                        flush=True,
                    )
                    plan_history.append(
                        {
                            "source": recovery.source,
                            "reason": recovery.reason,
                            "steps": [x.model_dump() for x in recovery.steps],
                        }
                    )
                else:
                    # Recovery plan 为空：无法生成恢复步骤，记录日志并中止执行
                    _logger.warning(
                        "[B9] Recovery plan 为空，无法从 '%s' 失败中恢复（step_id=%s），中止执行。",
                        tc.tool_name,
                        current_step.step_id,
                    )
                    print(
                        f"  [重规划失败] '{tc.tool_name}' 的恢复计划为空，终止本轮执行。",
                        flush=True,
                    )
                    plan_history.append(
                        {
                            "source": "replan_failed",
                            "reason": f"recovery_plan_empty_for_{tc.tool_name}",
                            "steps": [],
                        }
                    )
                    scheduler.mark_failed(current_step.step_id, skip_downstream=True)
                    break
            elif step_failed:
                scheduler.mark_failed(current_step.step_id, skip_downstream=True)
                break
            else:
                scheduler.mark_done(current_step.step_id)
            step_index += 1

        step1_has_step0 = False
        if len(trajectory_snapshots) >= 2:
            step1_has_step0 = "last_execution_result" in trajectory_snapshots[1]

        trajectory_serialized = self.b5.serialize_trajectory()
        out = {
            "trajectory": trajectory_serialized,
            "trajectory_snapshots": trajectory_snapshots,
            "step2_agent_input_contains_step1_execution_result": step1_has_step0,
            "steps_run": len(self.b5.trajectory),
            "orchestration_mode": "goal_driven",
            "effective_max_steps": effective_max_steps,
            "task_spec_effective": effective_task_spec.model_dump(),
            "plan_source": plan.source,
            "planned_tool_calls": [tc.model_dump() for tc in planned_calls],
            "plan_history": plan_history,
            "replan_count": replan_count,
        }
        if goal_intent is not None:
            # 给报告展示「GoalParser 如何理解」提供可读结构
            out["goal_intent"] = asdict(goal_intent)
        if query_rationale is not None:
            out["query_rationale"] = query_rationale
        if waiting_human_gate is not None:
            out["waiting_human_gate"] = waiting_human_gate
            out["run_status"] = "waiting_human_gate"

        out = self._attach_log_and_metrics(
            out, experiment_config, effective_task_spec, run_id, log_dir, rpa_mode=rpa_mode
        )

        # 等待人工确认时：系统尚未完成目标，指标置为未成功
        if out.get("run_status") == "waiting_human_gate":
            if isinstance(out.get("metrics"), dict):
                out["metrics"]["success"] = False
        self._merge_llm_timing_into_out(out, query_context, llm_sink)
        return out

    def _attach_log_and_metrics(
        self,
        out: dict,
        experiment_config: ExperimentConfig,
        task_spec: TaskSpec,
        run_id: str | None,
        log_dir: Path | str | None,
        *,
        rpa_mode: str = "normal",
    ) -> dict:
        if log_dir is not None and run_id is not None:
            import threading as _threading
            log_path = Path(log_dir)
            extra_cfg = getattr(experiment_config, "extra", None) or {}
            # 已配置 API 时默认接入 LLM-as-judge（与 B2/Query 同一套 env）；实验里可显式 use_llm_judge: false 关闭
            import os as _os
            use_llm_judge = extra_cfg.get("use_llm_judge")
            if use_llm_judge is None:
                use_llm_judge = bool(_os.environ.get("OPENAI_API_KEY") or _os.environ.get("XAI_API_KEY"))
            llm_judge_provider = extra_cfg.get("llm_provider") or (_os.environ.get("RAFT_LLM_PROVIDER") if use_llm_judge else None)

            # 1. 尽早启动 LLM-as-judge 后台线程，与后续同步计算并行执行，减少整体等待时间
            _llm_judge_result: list = [None]
            _judge_thread: _threading.Thread | None = None
            if use_llm_judge:
                from raft.evaluation.metrics import llm_judge_trajectory as _llm_judge_fn
                _traj_snapshot = out["trajectory"]
                _ts = task_spec
                _prov = llm_judge_provider
                def _run_llm_judge() -> None:
                    try:
                        _llm_judge_result[0] = _llm_judge_fn(_traj_snapshot, _ts, provider=_prov)
                    except Exception as _exc:
                        _logger.warning("[B8] LLM-as-judge 后台线程异常: %s", _exc)
                _judge_thread = _threading.Thread(target=_run_llm_judge, daemon=True, name="llm_judge_bg")
                _judge_thread.start()

            # 2. 同步规则评估（无 LLM，快速）；与 LLM judge 线程并行运行
            metrics = evaluate_trajectory(
                out["trajectory"],
                task_spec,
                run_id=run_id,
                extended=True,
                use_llm_judge=False,        # LLM judge 已在后台线程处理
                llm_judge_provider=None,
            )
            import time as _time
            timestamp_iso = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())

            # 3. 等待 LLM judge 结果（到此处时线程已运行了同步评估的时长，通常已接近完成）
            if _judge_thread is not None:
                _judge_thread.join(timeout=120)  # 最多等待 120s
                if _llm_judge_result[0] is not None:
                    metrics.llm_judge = _llm_judge_result[0]

            metrics_dump = metrics.model_dump()
            scenario_spec = get_scenario_spec(experiment_config)
            extra: dict[str, Any] = {
                "metrics": metrics_dump,
                "rpa_mode": rpa_mode,
                "orchestration_mode": out.get("orchestration_mode", "single_agent"),
                "route_type": out.get("route_type"),
                "scenario": getattr(experiment_config, "scenario", ""),
                "scenario_id": getattr(experiment_config, "scenario_id", None),
                "timestamp_iso": timestamp_iso,
            }
            if out.get("goal_intent") is not None:
                extra["goal_intent"] = out.get("goal_intent")
            if scenario_spec is not None:
                extra["scenario_spec"] = scenario_spec.model_dump()
            # 单轮结果落库：便于后续多轮聚合（run_id、metrics、llm_judge、timestamp 等）
            extra["run_record"] = {
                "run_id": run_id,
                "experiment_id": experiment_config.experiment_id,
                "scenario": getattr(experiment_config, "scenario", ""),
                "scenario_id": getattr(experiment_config, "scenario_id", None),
                "task_spec_id": task_spec.task_spec_id,
                "orchestration_mode": out.get("orchestration_mode", "single_agent"),
                "rpa_mode": rpa_mode,
                "metrics": metrics_dump,
                "timestamp_iso": timestamp_iso,
            }
            trajectory_file = write_trajectory_log(
                out["trajectory"],
                task_spec,
                run_id,
                log_path,
                experiment_id=experiment_config.experiment_id,
                extra=extra,
            )
            out["run_id"] = run_id
            out["metrics"] = metrics.model_dump()
            out["trajectory_log_path"] = str(trajectory_file)
        return out


def _rpa_mode_from_config(experiment_config: ExperimentConfig) -> str:
    """从实验配置 extra.rpa_config 解析 rpa_mode（normal/robustness/stress）。"""
    extra = getattr(experiment_config, "extra", None) or {}
    rpa_cfg = extra.get("rpa_config")
    if isinstance(rpa_cfg, dict):
        return rpa_cfg.get("mode", "normal")
    if hasattr(rpa_cfg, "mode"):
        return getattr(rpa_cfg, "mode", "normal")
    return "normal"
