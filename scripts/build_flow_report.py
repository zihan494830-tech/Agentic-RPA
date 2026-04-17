#!/usr/bin/env python
"""跑完完整流程后生成一份测试报告：输入、输出、各 Block 运作说明。

用法：
  - 由统一入口 run_poffices_agent.py 产出轨迹后生成（单轮/多轮统一）
  - 或从已有轨迹 JSON 重新生成：python scripts/build_flow_report.py logs/poffices -o logs/poffices/run_report.html
"""
import json
import re
import sys
from pathlib import Path
from typing import Any

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from raft.contracts.models import TaskSpec
from raft.evaluation.metrics import evaluate_trajectory
from raft.reporting.multi_agent import get_per_agent_segments
from raft.reporting.output_scope import extract_last_report_from_full_output


def _get_collaboration_agents(result: dict) -> list[str] | None:
    """若本 run 为多 Agent 协作模式（agent_master_run_flow_once），返回参与协作的 Agent 列表；否则 None。"""
    trajectory = result.get("trajectory") or []
    tools = []
    for step in trajectory:
        for tc in (step.get("step_result") or {}).get("tool_calls") or []:
            tools.append(tc.get("tool_name", ""))
    if "agent_master_run_flow_once" not in tools:
        return None
    for step in trajectory:
        for er in (step.get("step_result") or {}).get("execution_results") or []:
            if er.get("tool_name") == "agent_master_select_agents_for_flow":
                delta = er.get("ui_state_delta") or {}
                agents = delta.get("agent_master_flow_agents") or delta.get("agents_selected")
                if isinstance(agents, list) and agents:
                    return [str(a) for a in agents if a]
                rr = er.get("raw_response") or {}
                if isinstance(rr, dict):
                    agents = rr.get("agents_selected") or rr.get("agents")
                    if isinstance(agents, list) and agents:
                        return [str(a) for a in agents if a]
    return None


def _collab_note_html(result: dict) -> str:
    """协作模式下，在输出前展示「本输出由 X、Y 两个 Agent 协作产出并合并」的说明。"""
    agents = _get_collaboration_agents(result)
    if not agents:
        return ""
    if len(agents) == 1:
        return f'<p class="note" style="margin-bottom:0.5rem;">本输出由 <strong>{_esc(agents[0])}</strong> 生成。</p>'

    agents_str = "、".join(agents)
    return f'<p class="note" style="margin-bottom:0.5rem;">本输出由 <strong>{_esc(agents_str)}</strong> 协作产出并合并（同一 query 分别由各 Agent 生成内容后合并为一份报告）。</p>'


def _collab_assignments_html(state: dict[str, Any] | None) -> str:
    """若 initial_state 含 per-agent queries，则在报告中展示协作分工。"""
    if not isinstance(state, dict):
        return ""
    agents = state.get("agents_to_test")
    queries = state.get("queries_per_agent")
    if not isinstance(agents, list) or not isinstance(queries, list):
        return ""
    if len(agents) <= 1 or len(agents) != len(queries):
        return ""

    rows: list[str] = []
    for idx, agent in enumerate(agents):
        query = queries[idx]
        if not isinstance(agent, str) or not agent.strip():
            continue
        if not isinstance(query, str) or not query.strip():
            continue
        rows.append(f"<li><strong>{_esc(agent.strip())}</strong>: {_esc(query.strip())}</li>")
    if not rows:
        return ""

    return (
        '<div class="detail-block">'
        "<p><strong>协作分工（per-agent queries）</strong></p>"
        f"<ul>{''.join(rows)}</ul>"
        "</div>"
    )


def _scenario_spec_section(config: dict, *, heading: str = "1.2 场景规范（ScenarioSpec）") -> str:
    """将 config 中的 scenario_spec 摘要渲染为 HTML（不展示 allowed_agents/blocks、flow_template、约束等红框内容）。"""
    spec = config.get("scenario_spec")
    if not isinstance(spec, dict):
        return ""

    parts = [f"<h3>{_esc(heading)}</h3>", "<ul>"]
    parts.append(f"<li><strong>scenario_id</strong>: {_esc(spec.get('id', ''))}</li>")
    if config.get("scenario_spec_path"):
        parts.append(f"<li><strong>scenario_spec_path</strong>: {_esc(config.get('scenario_spec_path', ''))}</li>")
    if spec.get("name"):
        parts.append(f"<li><strong>name</strong>: {_esc(spec.get('name', ''))}</li>")
    if spec.get("description"):
        parts.append(f"<li><strong>description</strong>: {_esc(spec.get('description', ''))}</li>")
    if spec.get("narrative"):
        parts.append(f"<li><strong>narrative</strong>: {_esc(spec.get('narrative', ''))}</li>")
    if spec.get("task_spec_ids"):
        parts.append(
            f"<li><strong>allowed_task_spec_ids</strong>: {_esc(json.dumps(spec.get('task_spec_ids', []), ensure_ascii=False))}</li>"
        )
    if spec.get("suggested_agents"):
        parts.append(
            f"<li><strong>suggested_agents</strong>: {_esc(json.dumps(spec.get('suggested_agents', []), ensure_ascii=False))}</li>"
        )
    # 红框内容不展示：allowed_agents、allowed_blocks、flow_template、required_blocks、forbidden_blocks、constraint_notes
    parts.append("</ul>")
    return "\n".join(parts)


def build_flow_report(
    result: dict,
    config: dict,
    task: dict,
    *,
    output_path: Path | None = None,
) -> str:
    """
    根据一次 run 的 result、config、task 生成「输入 / 输出 / 各 Block 运作」的 HTML 报告。
    result 需含 trajectory, steps_run, metrics, orchestration_mode 等（run_until_done 返回值）。
    返回 HTML 字符串；若指定 output_path 则写入文件。
    """
    trajectory = result.get("trajectory") or []
    metrics = result.get("metrics") or {}
    steps_run = result.get("steps_run", 0)
    orchestration_mode = result.get("orchestration_mode", "single_agent")
    run_id = result.get("run_id", "")
    task_spec = TaskSpec.model_validate(task)

    # 本 run 实际使用的 initial_state（B9 可能已用 LLM 建议的 query 覆盖）
    actual_initial_state = None
    if trajectory:
        first = trajectory[0]
        snap = (first.get("step_result") or {}).get("agent_input_snapshot") or {}
        if snap.get("state") is not None:
            actual_initial_state = snap["state"]

    # ----- 输入 -----（含本轮 RPA 工作流程）
    input_html = _section_input(config, task, actual_initial_state=actual_initial_state, result=result)

    # ----- 输出 -----
    output_html = _section_output(result, trajectory, metrics, steps_run)

    # ----- 各 Block 运作 -----
    blocks_html = _section_blocks(
        config, task, result, trajectory, orchestration_mode, run_id
    )

    html = _wrap_html(
        title="ART 流程测试报告",
        run_id=run_id,
        input_section=input_html,
        output_section=output_html,
        blocks_section=blocks_html,
    )

    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(html, encoding="utf-8")
    return html


def build_multi_flow_report(
    results: list[dict],
    config: dict,
    task: dict,
    *,
    output_path: Path | None = None,
    llm_summary: str | None = None,
    rounds_rationale: str | None = None,
    minimal_report: bool = False,
    report_generation_llm_ms: int | None = None,
) -> str:
    """
    根据多轮 run 的 results 生成「多轮汇总 + 每轮明细 + 可选 LLM 多轮分析总结」的 HTML 报告。
    results 中每项为 run_until_done 返回值（含 trajectory, metrics, run_id 等）。
    llm_summary 若提供则展示在报告中的「LLM 多轮分析总结」区块。
    rounds_rationale 若提供则不再展示在报告中（已去掉「测试轮数决定依据」展示）。
    minimal_report=True 时：不展示任何打分与 LLM 简要分析，仅展示输入、输出、轨迹与各 Block 步骤。
    返回 HTML 字符串；若指定 output_path 则写入文件。
    """
    if not results:
        html = _wrap_multi_html(
            summary_stats={"total_runs": 0, "success_count": 0, "success_rate": 0, "avg_step_count": 0},
            rounds_table_html="<p>无运行记录。</p>",
            llm_summary=llm_summary,
            input_section="",
            blocks_overview_section=_section_blocks_multi_overview(config, task),
            rounds_rationale=rounds_rationale,
            show_summary_section=not minimal_report,
        )
        if output_path:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_text(html, encoding="utf-8")
        return html

    total = len(results)
    success_count = sum(1 for r in results if (r.get("metrics") or {}).get("success", False))
    success_rate = round(success_count / total, 2) if total else 0
    step_counts = [(r.get("metrics") or {}).get("step_count", r.get("steps_run", 0)) for r in results]
    avg_step_count = round(sum(step_counts) / len(step_counts), 1) if step_counts else 0
    # 聚合各 run 详细指标，统一放入 summary_stats 传给 _wrap_multi_html
    _exec_rates: list[float] = []
    _total_timeouts = 0
    _total_recoveries = 0
    _total_retries = 0
    _total_elapsed_ms = 0
    for _r in results:
        _det = (_r.get("metrics") or {}).get("details") or {}
        if _det.get("execution_success_rate") is not None:
            _exec_rates.append(float(_det["execution_success_rate"]))
        _total_timeouts += int(_det.get("timeout_count") or 0)
        _total_recoveries += int(_det.get("recovery_count") or 0)
        _total_retries += int(_det.get("retry_count") or 0)
        _bd = _collect_run_time_breakdown_ms(_r)
        _total_elapsed_ms += sum(_bd.values())

    summary_stats = {
        "total_runs": total,
        "success_count": success_count,
        "success_rate": success_rate,
        "avg_step_count": avg_step_count,
        "exec_rates": _exec_rates,
        "total_timeouts": _total_timeouts,
        "total_recoveries": _total_recoveries,
        "total_retries": _total_retries,
        "total_elapsed_ms": _total_elapsed_ms,
    }

    single_view = total == 1
    # 多 Agent 时表格按 Agent 拆成多行，并统计按 Agent 成功率
    any_multi_agent = False
    agent_ok_total = 0
    agent_total = 0
    for r in results:
        segs = get_per_agent_segments(r)
        if segs and len(segs) > 1:
            any_multi_agent = True
            agent_total += len(segs)
            agent_ok_total += sum(1 for s in segs if s.get("success", False))
    if any_multi_agent:
        summary_stats["agent_success_count"] = agent_ok_total
        summary_stats["agent_total"] = agent_total
    detail_title = "本轮明细"
    if any_multi_agent and single_view:
        detail_title = "本轮明细（本 run 含多 Agent，按 Agent 拆分）"
    elif any_multi_agent:
        detail_title = "多轮明细（含按 Agent 拆分的 run）"
    # 报告：输入区仅展示实验配置与待测场景，并展示本轮 RPA 工作流程；具体输入在明细区
    task_spec = TaskSpec.model_validate(task)
    input_section = _section_input_multi(config, task, first_result=results[0])

    # 各 Block 运作（总体说明，各 run 均按此流程）
    blocks_overview_html = _section_blocks_multi_overview(config, task)
    timing_breakdown_html = _section_time_breakdown(results)
    # 每轮明细：表格 + 可折叠的每轮详情（含每轮 Block 简要说明）
    rounds_table_html = _section_multi_rounds_table(
        results,
        config,
        task,
        task_spec,
        section_title=detail_title,
        include_llm_brief=not single_view and not minimal_report,
        minimal_report=minimal_report,
        any_multi_agent=any_multi_agent,
    )
    # 多 Agent 时「本 run 各 Block 运作」整 run 只展示一次，放在表格下方独立一节，不再塞在首行展开里
    run_level_blocks_section = ""
    if any_multi_agent and results:
        first_result = results[0]
        traj_len = len(first_result.get("trajectory") or [])
        run_level_blocks_section = (
            '<div class="section">'
            f'<h2>4. 本 run 各 Block 运作（整 run 共 {traj_len} 步）</h2>'
            '<p class="note">以下为本 run 内全部 Agent 段的 B1–B9 与 tool_calls/执行结果，与上方「多轮明细」中每一行的「本 Agent 段」对应。</p>'
            f"{_section_blocks_brief_one_run(first_result, config, task)}"
            "</div>"
        )
    html = _wrap_multi_html(
        summary_stats=summary_stats,
        rounds_table_html=rounds_table_html,
        llm_summary=llm_summary,
        input_section=input_section,
        blocks_overview_section=blocks_overview_html,
        timing_breakdown_section=timing_breakdown_html,
        rounds_rationale=rounds_rationale,
        show_summary_section=not single_view and not minimal_report,
        run_level_blocks_section=run_level_blocks_section,
        llm_section_title="LLM 本轮总结" if single_view else "LLM 多轮分析总结",
        llm_section_note=(
            "由 LLM 基于本轮指标与单轮评分（若有）生成的待测 Agent 表现分析，供报告参考。"
            if single_view
            else "由 LLM 基于多轮指标与单轮评分（若有）生成的待测 Agent 表现分析，供报告参考。"
        ),
        report_title="ART 本轮测试报告" if single_view else "ART 多轮测试报告",
        report_generation_llm_ms=report_generation_llm_ms,
    )

    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(html, encoding="utf-8")
    return html


def _section_blocks_multi_overview(config: dict, task: dict) -> str:
    """多轮报告用：各 Block 运作的总体说明（各 run 均按此流程）。"""
    exp_id = config.get("experiment_id", "")
    task_spec_id = task.get("task_spec_id", "")
    return f"""
<div class="section">
  <h2>2. 各 Block 运作</h2>
  <p class="note">每次 run 中 B1–B9 的职责与数据流简述如下；各 run 均按此流程执行，每轮在本 run 内的具体表现见下方「多轮明细」中该轮的「本 run 各 Block 运作」。</p>
  <ol class="block-list">
    <li><strong>B1 Experiment Config &amp; TaskSpec</strong><br/>
      加载实验配置 <code>{_esc(exp_id)}</code> 与任务 <code>{_esc(task_spec_id)}</code>，为 B9 提供本次 run 的 task_spec 与 initial_state。</li>
    <li><strong>B2 Difficulty &amp; Routing</strong><br/>
      本框架下为 single_agent 时路由为 single_flow；若为 multi_agent_dag 则 B2 输出 route_type 供 B3 建 DAG。</li>
    <li><strong>B3 Workflow Manager (DAG)</strong> / <strong>B4 Agent Scheduler</strong><br/>
      单 Agent 模式下为线性步进，无 DAG；每步由 B4 分配给同一 B6 决策组件（如 PofficesAgent）。</li>
    <li><strong>B5 State &amp; Trajectory Manager</strong><br/>
      维护 current_step_index、state（含 initial_state）、last_execution_result；每步记录 step_result（tool_calls + execution_results）。</li>
    <li><strong>B6 决策组件（如 PofficesAgent / PofficesLLMAgent）</strong><br/>
      根据 agent_input_context（state + last_execution_result）每步输出 tool_calls；待测 Agent 为本 run 在 Poffices 页面上测试的产品（见 app_ready 的 options.agent_name）。</li>
    <li><strong>B7 RPA Adapter（PofficesRPA）</strong><br/>
      执行 B6 下发的 tool_calls，在真实 Poffices 页面上执行 poffices_bootstrap / poffices_query，返回 ExecutionResult（success、raw_response、ui_state_delta）。</li>
    <li><strong>B8 Evaluators &amp; Metrics</strong><br/>
      根据轨迹与 TaskSpec 判定任务成功与否、步数，写入 RunMetrics；轨迹落盘至 log_dir；可选 LLM-as-judge 对决策质量、推理连贯性、工具熟练度等评分。</li>
    <li><strong>B9 Orchestrator</strong><br/>
      串联 B2–B7：初始化 B5 state → 每轮取 agent_input_context → 调用 B6 agent.run() → 对每个 tool_call 调用 B7 rpa.execute() → B5.record_step() → 直至无 tool_calls 或达 max_steps；最后调用 B8 落盘与评估。</li>
  </ol>
</div>"""


def _normalize_tool_group(tool_name: str) -> str:
    """将轨迹中的 tool_name 归入时间统计桶。

    「输出等待与抓取」：在 Poffices 流程里等待文档生成、抓取回复等 wall time 主要落在
    get_response / poffices_query / agent_master_run_flow_once 等步的 elapsed_ms 内；
    仅当计划显式包含 wait_output_complete 块时才有该工具名。
    「RPA 交互」：登录、选 Agent、发 query（不含长等待）、Discovery 子步骤等。
    """
    mapping = {
        "app_ready": "RPA 交互",
        "send_query": "RPA 交互",
        "get_response": "输出等待与抓取",
        "poffices_bootstrap": "RPA 交互",
        "poffices_query": "输出等待与抓取",
        "agent_master_select_agents_for_flow": "RPA 交互",
        "agent_master_run_flow_once": "输出等待与抓取",
        "discovery_bootstrap": "RPA 交互",
        "list_offices": "RPA 交互",
        "expand_office": "RPA 交互",
        "list_agents_in_office": "RPA 交互",
        "wait_output_complete": "输出等待与抓取",
        "refresh_page": "重试 / 恢复",
        "retry_operation": "重试 / 恢复",
    }
    return mapping.get(tool_name, "其他")


def _is_recovery_step(sr: dict) -> bool:
    """判断该 step 是否为恢复计划步骤：tool_calls 的 step_id 以 'r' 开头（如 r0_0, r1_2）。"""
    for tc in sr.get("tool_calls") or []:
        sid = tc.get("step_id") or ""
        if isinstance(sid, str) and sid.startswith("r"):
            return True
    return False


def _collect_run_time_breakdown_ms(result: dict) -> dict[str, int]:
    breakdown = {
        "LLM API 调用": 0,
        "输出等待与抓取": 0,
        "RPA 交互": 0,
        "重试 / 恢复": 0,
        "其他": 0,
    }
    for ev in result.get("llm_timing_events") or []:
        if isinstance(ev, dict):
            ms = ev.get("elapsed_ms")
            if isinstance(ms, (int, float)) and ms >= 0:
                breakdown["LLM API 调用"] += int(ms)
    trajectory = result.get("trajectory") or []
    for entry in trajectory:
        sr = entry.get("step_result") or {}
        is_recovery = _is_recovery_step(sr)
        for er in sr.get("execution_results") or []:
            name = er.get("tool_name") or ""
            extra = er.get("extra") or {}
            elapsed_ms = extra.get("elapsed_ms")
            if isinstance(elapsed_ms, (int, float)) and elapsed_ms >= 0:
                if is_recovery:
                    # 恢复计划步骤（step_id 以 r 开头）整体归入"重试/恢复"，
                    # 不再按 tool_name 分类，避免 app_ready/send_query 超时时间
                    # 被错误计入"RPA 交互"。
                    group = "重试 / 恢复"
                else:
                    group = _normalize_tool_group(str(name))
                breakdown[group] = int(breakdown.get(group, 0) + int(elapsed_ms))
    return breakdown


def _section_time_breakdown(results: list[dict]) -> str:
    rows: list[str] = []
    for i, r in enumerate(results):
        run_id = r.get("run_id", f"run_{i + 1}")
        bd = _collect_run_time_breakdown_ms(r)
        total_ms = sum(bd.values())
        if total_ms <= 0:
            continue
        llm = bd.get("LLM API 调用", 0)
        wait_cap = bd.get("输出等待与抓取", 0)
        rpa = bd.get("RPA 交互", 0)
        recovery = bd.get("重试 / 恢复", 0)
        other = bd.get("其他", 0)
        total_sec = total_ms / 1000.0
        bar_html = (
            f'<div class="time-bar-seg seg-llm" style="width:{(llm / total_ms) * 100:.2f}%;" title="LLM API: {llm / 1000:.1f}s"></div>'
            f'<div class="time-bar-seg seg-wait" style="width:{(wait_cap / total_ms) * 100:.2f}%;" title="输出等待与抓取: {wait_cap / 1000:.1f}s"></div>'
            f'<div class="time-bar-seg seg-rpa" style="width:{(rpa / total_ms) * 100:.2f}%;" title="RPA 交互: {rpa / 1000:.1f}s"></div>'
            f'<div class="time-bar-seg seg-recover" style="width:{(recovery / total_ms) * 100:.2f}%;" title="重试 / 恢复: {recovery / 1000:.1f}s"></div>'
            f'<div class="time-bar-seg seg-other" style="width:{(other / total_ms) * 100:.2f}%;" title="其他: {other / 1000:.1f}s"></div>'
        )
        def _pct(v: float) -> str:
            p = v / total_ms * 100
            return f'<span style="color:#94a3b8;font-size:0.78em;margin-left:3px;">({p:.0f}%)</span>'

        rows.append(
            "<tr>"
            f"<td class=\"col-run-id\">{_esc(run_id)}</td>"
            f"<td><strong>{total_sec:.1f}s</strong></td>"
            f"<td>{llm / 1000:.1f}s{_pct(llm)}</td>"
            f"<td>{wait_cap / 1000:.1f}s{_pct(wait_cap)}</td>"
            f"<td>{rpa / 1000:.1f}s{_pct(rpa)}</td>"
            f"<td>{recovery / 1000:.1f}s{_pct(recovery)}</td>"
            f"<td>{other / 1000:.1f}s{_pct(other)}</td>"
            f"<td><div class=\"time-bar\">{bar_html}</div></td>"
            "</tr>"
        )
    if not rows:
        return ""
    return f"""
<div class="section">
  <h2>2.5 每次运行的时间构成</h2>
  <p class="note"><strong>LLM API</strong>：本 run 内写入 <code>llm_timing_events</code> 的调用（含编排内任务描述/query、Goal 解析、规划、B6/B8 等；若在入口已开启采集，还含 run 前 Goal Interpreter / Discovery 的 LLM，经 <code>external_llm_timing_events</code> 合并）。<strong>输出等待与抓取</strong>：<code>get_response</code> / <code>poffices_query</code> / <code>agent_master_run_flow_once</code> / <code>wait_output_complete</code> 等步的 <code>elapsed_ms</code>（等待生成与抓正文主要在这里，而非单独的 wait 块）。<strong>RPA 交互</strong>：<code>app_ready</code>、<code>send_query</code>、Discovery 子步骤等。报告末尾「报告生成阶段」单独统计多轮分析总结的 LLM 耗时。</p>
  <div class="table-wrap">
    <table class="multi-round-table time-breakdown-table">
      <thead><tr>
        <th>run_id</th>
        <th>总耗时</th>
        <th><span class="col-dot-wrap"><span class="col-dot" style="background:#d97706;"></span>LLM API</span></th>
        <th><span class="col-dot-wrap"><span class="col-dot" style="background:#7c3aed;"></span>输出等待/抓取</span></th>
        <th><span class="col-dot-wrap"><span class="col-dot" style="background:#1d4ed8;"></span>RPA 交互</span></th>
        <th><span class="col-dot-wrap"><span class="col-dot" style="background:#dc2626;"></span>重试 / 恢复</span></th>
        <th><span class="col-dot-wrap"><span class="col-dot" style="background:#64748b;"></span>其他</span></th>
        <th>时间构成比例</th>
      </tr></thead>
      <tbody>
        {"".join(rows)}
      </tbody>
    </table>
  </div>
</div>"""


def _section_blocks_brief_one_run(result: dict, config: dict, task: dict) -> str:
    """单轮各 Block 运作的简要说明（用于多轮明细中该轮展开内容）。"""
    trajectory = result.get("trajectory") or []
    orchestration_mode = result.get("orchestration_mode", "single_agent")
    run_id = result.get("run_id", "")
    exp_id = config.get("experiment_id", "")
    task_spec_id = task.get("task_spec_id", "")

    b6_steps = []
    for i, entry in enumerate(trajectory):
        sr = entry.get("step_result") or {}
        tcs = sr.get("tool_calls") or []
        b6_steps.append("步 " + str(i) + ": " + ", ".join(f"{tc.get('tool_name')}({json.dumps(tc.get('params') or {}, ensure_ascii=False)})" for tc in tcs))
    b7_steps = []
    for i, entry in enumerate(trajectory):
        sr = entry.get("step_result") or {}
        for er in sr.get("execution_results") or []:
            b7_steps.append(f"步 {i} {er.get('tool_name', '')}: success={er.get('success', False)}")

    b6_pre = _esc("\n".join(b6_steps)) if b6_steps else "—"
    b7_pre = _esc("\n".join(b7_steps)) if b7_steps else "—"
    return f"""
  <div class="detail-block">
  <p><strong>本 run 各 Block 运作</strong></p>
  <ul class="block-brief-list">
    <li><strong>B1</strong> 加载 <code>{_esc(exp_id)}</code> / <code>{_esc(task_spec_id)}</code>，提供 task_spec 与 initial_state。</li>
    <li><strong>B2</strong> 本 run 为 <code>{_esc(orchestration_mode)}</code>，路由 single_flow。</li>
    <li><strong>B5</strong> 本 run 共 {len(trajectory)} 条轨迹。</li>
    <li><strong>B6</strong> 本 run tool_calls：<pre class="code block-brief-pre">{b6_pre}</pre></li>
    <li><strong>B7</strong> 本 run 执行结果：<pre class="code block-brief-pre">{b7_pre}</pre></li>
    <li><strong>B8</strong> 根据轨迹与 TaskSpec 判定成功与否、步数并落盘；可选 LLM-as-judge 评分。</li>
    <li><strong>B9</strong> 串联 B2–B7，按步执行至无 tool_calls 或达 max_steps，最后调用 B8。</li>
  </ul>
  </div>"""


def _section_multi_rounds_table(
    results: list[dict],
    config: dict,
    task: dict,
    task_spec: Any = None,
    *,
    section_title: str = "多轮明细",
    include_llm_brief: bool = True,
    minimal_report: bool = False,
    any_multi_agent: bool = False,
) -> str:
    """明细表：每行 run_id、成功、步数、扩展指标；展开后为本轮输入/输出、工具序列、（可选）本轮 LLM 简要分析、本 run 各 Block 运作简要说明。多 Agent 时按 Agent 拆成多行；any_multi_agent 时不在每行内嵌整份 Block 运作，改为在表格下方独立一节展示。minimal_report 时不展示打分类扩展指标与 LLM 简要分析。"""
    rows = []
    for i, r in enumerate(results):
        run_id = r.get("run_id", f"run_{i+1}")
        trajectory = r.get("trajectory") or []
        metrics = r.get("metrics") or {}
        details = metrics.get("details") or {}
        llm_judge = metrics.get("llm_judge")
        round_input_state = {}
        if trajectory:
            first_snap = (trajectory[0].get("step_result") or {}).get("agent_input_snapshot") or {}
            round_input_state = first_snap.get("state") or {}
        if isinstance(round_input_state, dict) and round_input_state:
            round_input_query = json.dumps(round_input_state, ensure_ascii=False, indent=2)
        elif round_input_state:
            round_input_query = str(round_input_state)
        else:
            round_input_query = "（无）"

        def _segment_input_query(state: dict, seg_idx: int, agent_name: str) -> str:
            """多 Agent 时每行只显示本 Agent 对应的 query，不展示整份 queries_per_agent。"""
            if not state:
                return "（无）"
            qpa = state.get("queries_per_agent")
            if isinstance(qpa, list) and seg_idx < len(qpa):
                q = qpa[seg_idx]
            else:
                q = state.get("query", "")
            return json.dumps({"query": q, "（本 Agent）": agent_name}, ensure_ascii=False, indent=2)

        query_rationale = r.get("query_rationale")
        query_rationale_html = ""
        if query_rationale and str(query_rationale).strip():
            query_rationale_html = f"""
      <div class="detail-block query-rationale">
        <p><strong>本轮 query 选择思路</strong></p>
        <p class="detail-value">{_esc(str(query_rationale).strip())}</p>
      </div>"""
        # 多 Agent 时整 run 的 Block 运作在报告下方独立一节展示，此处每行只放简短说明
        if any_multi_agent:
            blocks_for_row_note = '<div class="detail-block"><p class="note">本 run 各 Block 运作（B1–B9 及本 run 全部 tool_calls/执行结果）见报告下方「本 run 各 Block 运作」一节。</p></div>'
        blocks_brief_html = _section_blocks_brief_one_run(r, config, task)

        segments = get_per_agent_segments(r)
        if segments and len(segments) > 1:
            for seg_idx, seg in enumerate(segments):
                seg_run_id = f"{run_id} · {seg['agent_name']}"
                success = seg["success"]
                success_badge = "success" if success else "fail"
                step_count = 3
                raw_output = seg.get("output_raw") or ""
                business_output = extract_last_report_from_full_output(raw_output, take_last=True) if raw_output else ""
                if not business_output and raw_output:
                    business_output = (raw_output[:1200] + "…") if len(raw_output) > 1200 else raw_output
                tools_str = "app_ready → send_query → get_response"
                extra_cells = []
                if not minimal_report:
                    if details.get("execution_success_rate") is not None:
                        extra_cells.append(f"执行成功率={details['execution_success_rate']}")
                    if llm_judge and isinstance(llm_judge, dict) and llm_judge.get("decision_quality") is not None:
                        extra_cells.append(f"决策质量={llm_judge['decision_quality']}")
                extra_str = "; ".join(extra_cells) if extra_cells else "—"
                llm_brief_html = f'<div class="detail-block">{_format_per_round_llm_brief(llm_judge if seg_idx == 0 else None)}</div>' if include_llm_brief else ""
                if seg_idx > 0:
                    llm_brief_html = ""
                blocks_for_row = blocks_for_row_note if any_multi_agent else (blocks_brief_html if seg_idx == 0 else '<div class="detail-block"><p class="note">本 run 为多 Agent 测试，上表同 run_id 的其它行为本 run 内其它 Agent。</p></div>')
                detail_id = f"round-detail-{i}-{seg_idx}"
                # 多 Agent 时本轮输入只显示本 Agent 对应的 query，不展示全部 queries_per_agent
                segment_input_display = _segment_input_query(round_input_state if isinstance(round_input_state, dict) else {}, seg_idx, seg["agent_name"]) if any_multi_agent else (round_input_query or "（无）")
                rows.append(f"""
<tr>
  <td class="col-run-id">{_esc(seg_run_id)}</td>
  <td><span class="badge {success_badge}">{success}</span></td>
  <td>{step_count}</td>
  <td class="extra-metrics">{_esc(extra_str)}</td>
  <td><button type="button" class="toggle-detail" data-target="{detail_id}" aria-expanded="false">展开</button></td>
</tr>
<tr id="{detail_id}" class="round-detail-row" style="display:none;">
  <td colspan="5">
    <div class="round-detail">
      {query_rationale_html if seg_idx == 0 else ""}
      <div class="detail-block">
        <p><strong>本轮输入（query / 实际 state）</strong></p>
        <pre class="code round-io">{_esc(segment_input_display)}</pre>
      </div>
      <div class="detail-block">
        <p><strong>工具序列（本 Agent 段）</strong></p>
        <p class="detail-value">{_esc(tools_str)}</p>
      </div>
      <div class="detail-block">
        <p><strong>本 Agent 输出</strong></p>
        <pre class="code round-io round-output">{_esc(business_output or "（无）")}</pre>
      </div>
      {llm_brief_html}
      {blocks_for_row}
    </div>
  </td>
</tr>""")
            continue

        success = metrics.get("success", False)
        step_count = metrics.get("step_count", r.get("steps_run", 0))
        success_badge = "success" if success else "fail"
        raw_output = ""
        if trajectory:
            last_step = trajectory[-1]
            for er in (last_step.get("step_result") or {}).get("execution_results") or []:
                delta = er.get("ui_state_delta") or {}
                for key in ("poffices_response", "final_report", "response", "output", "content"):
                    if key in delta and delta[key]:
                        v = delta[key]
                        raw_output = (v if isinstance(v, str) else str(v)).strip()
                        break
                if raw_output:
                    break
        business_output = extract_last_report_from_full_output(raw_output, take_last=True) if raw_output else ""
        if not business_output and raw_output:
            business_output = "（无）"
        tools_summary = []
        for e in trajectory:
            for tc in (e.get("step_result") or {}).get("tool_calls") or []:
                tools_summary.append(tc.get("tool_name", "?"))
        tools_str = " → ".join(tools_summary) if tools_summary else "—"
        extra_cells = []
        if not minimal_report:
            if details.get("execution_success_rate") is not None:
                extra_cells.append(f"执行成功率={details['execution_success_rate']}")
            if details.get("retry_count") is not None:
                extra_cells.append(f"重试={details['retry_count']}")
            if llm_judge and isinstance(llm_judge, dict):
                dq = llm_judge.get("decision_quality")
                if dq is not None:
                    extra_cells.append(f"决策质量={dq}")
        extra_str = "; ".join(extra_cells) if extra_cells else "—"
        llm_brief_html = ""
        if include_llm_brief:
            llm_brief_html = f'<div class="detail-block">{_format_per_round_llm_brief(llm_judge)}</div>'
        detail_id = f"round-detail-{i}"
        rows.append(f"""
<tr>
  <td class="col-run-id">{_esc(run_id)}</td>
  <td><span class="badge {success_badge}">{success}</span></td>
  <td>{step_count}</td>
  <td class="extra-metrics">{_esc(extra_str)}</td>
  <td><button type="button" class="toggle-detail" data-target="{detail_id}" aria-expanded="false">展开</button></td>
</tr>
<tr id="{detail_id}" class="round-detail-row" style="display:none;">
  <td colspan="5">
    <div class="round-detail">
      {query_rationale_html}
      <div class="detail-block">
        <p><strong>本轮输入（query / 实际 state）</strong></p>
        <pre class="code round-io">{_esc(round_input_query or "（无）")}</pre>
      </div>
      <div class="detail-block">
        <p><strong>工具序列</strong></p>
        <p class="detail-value">{_esc(tools_str)}</p>
      </div>
      <div class="detail-block">
        <p><strong>本轮输出</strong></p>
        {_collab_note_html(r)}
        <pre class="code round-io round-output">{_esc(business_output or "（无）")}</pre>
      </div>
      {llm_brief_html}
      {blocks_brief_html}
    </div>
  </td>
</tr>""")

    table_body = "\n".join(rows)
    return f"""
<div class="section">
  <h2>3. {_esc(section_title)}</h2>
  <div class="table-wrap">
  <table class="multi-round-table">
    <thead><tr><th>run_id</th><th>成功</th><th>步数</th><th>扩展指标</th><th>详情</th></tr></thead>
    <tbody>
{table_body}
    </tbody>
  </table>
  </div>
</div>
<script>
(function() {{
  document.querySelectorAll('.toggle-detail').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var id = this.getAttribute('data-target');
      var row = document.getElementById(id);
      var expanded = this.getAttribute('aria-expanded') === 'true';
      row.style.display = expanded ? 'none' : 'table-row';
      this.setAttribute('aria-expanded', !expanded);
      this.textContent = expanded ? '展开' : '收起';
    }});
  }});
}})();
</script>"""


def _format_llm_summary_for_html(text: str) -> str:
    """将 LLM 多轮总结文本转为结构化 HTML：识别 #### N. 或 N. 标题 为小节标题，段落分块展示，便于阅读。"""
    if not (text or "").strip():
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    header_re = re.compile(r"^(\s*####\s*)?(\d+\.\s+.+)$")

    def flush_paragraph_block(raw_lines: list[str]) -> None:
        if not raw_lines:
            return
        raw = "\n".join(raw_lines)
        # 先按双换行拆成多段
        blocks = [b.strip() for b in re.split(r"\n\s*\n", raw) if b.strip()]
        for block in blocks:
            if len(block) > 360 and ("。" in block or "；" in block):
                # 长段按句号/分号拆成多 <p>，避免一大坨
                sentences = re.split(r"(?<=[。；])\s*", block)
                sentences = [s.strip() for s in sentences if s.strip()]
                chunk: list[str] = []
                clen = 0
                for s in sentences:
                    chunk.append(s)
                    clen += len(s)
                    if clen >= 100:
                        out.append(f'<p class="llm-summary-p">{_esc(" ".join(chunk))}</p>')
                        chunk = []
                        clen = 0
                if chunk:
                    out.append(f'<p class="llm-summary-p">{_esc(" ".join(chunk))}</p>')
            else:
                html_block = _esc(block).replace("\n", "<br/>")
                out.append(f'<p class="llm-summary-p">{html_block}</p>')

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        m = header_re.match(stripped)
        if m:
            title = m.group(2).strip()
            out.append(f'<h4 class="llm-summary-h4">{_esc(title)}</h4>')
            i += 1
            para_lines: list[str] = []
            while i < len(lines):
                next_line = lines[i]
                next_stripped = next_line.strip()
                if header_re.match(next_stripped):
                    break
                if not next_stripped:
                    i += 1
                    break
                para_lines.append(next_line)
                i += 1
            flush_paragraph_block(para_lines)
            continue
        para_lines = []
        while i < len(lines):
            next_line = lines[i]
            next_stripped = next_line.strip()
            if header_re.match(next_stripped):
                break
            if not next_stripped:
                i += 1
                break
            para_lines.append(next_line)
            i += 1
        flush_paragraph_block(para_lines)

    return "\n".join(out)


def _wrap_multi_html(
    *,
    summary_stats: dict,
    rounds_table_html: str,
    llm_summary: str | None,
    input_section: str,
    blocks_overview_section: str = "",
    timing_breakdown_section: str = "",
    rounds_rationale: str | None = None,
    show_summary_section: bool = True,
    llm_section_title: str = "LLM 多轮分析总结",
    llm_section_note: str = "由 LLM 基于多轮指标与单轮评分（若有）生成的待测 Agent 表现分析，供报告参考。",
    report_title: str = "ART 多轮测试报告",
    run_level_blocks_section: str = "",
    report_generation_llm_ms: int | None = None,
) -> str:
    """多轮报告整体 HTML 壳。"""
    total = summary_stats.get("total_runs", 0)
    success_count = summary_stats.get("success_count", 0)
    success_rate = summary_stats.get("success_rate", 0)
    avg_step = summary_stats.get("avg_step_count", 0)
    agent_total = summary_stats.get("agent_total", 0)
    agent_success_count = summary_stats.get("agent_success_count", 0)
    agent_strip = f'<span>按 Agent 成功 <strong>{agent_success_count}/{agent_total}</strong></span>' if agent_total > 0 else ""

    llm_section = ""
    if llm_summary:
        llm_html = _format_llm_summary_for_html(llm_summary)
        timing_note = ""
        if report_generation_llm_ms is not None and report_generation_llm_ms > 0:
            timing_note = (
                f'<p class="note" style="margin-top:0.5rem;"><strong>报告生成阶段 LLM</strong>：'
                f'约 {report_generation_llm_ms / 1000:.1f}s（多轮分析总结 <code>chat.completions</code>，不计入上表各 run）。</p>'
            )
        llm_section = f"""
<div class="section llm-summary-section">
  <h2>{_esc(llm_section_title)}</h2>
  <p class="note">{_esc(llm_section_note)}</p>
  {timing_note}
  <div class="llm-summary-text">{llm_html}</div>
</div>"""

    # 从 summary_stats 读取预聚合指标（由 build_multi_flow_report 计算后传入）
    _exec_rates: list[float] = summary_stats.get("exec_rates") or []
    _total_timeouts: int = summary_stats.get("total_timeouts", 0)
    _total_recoveries: int = summary_stats.get("total_recoveries", 0)
    _total_retries: int = summary_stats.get("total_retries", 0)
    _total_elapsed_ms: int = summary_stats.get("total_elapsed_ms", 0)
    _avg_exec_rate_str = f"{round(sum(_exec_rates) / len(_exec_rates) * 100):.0f}%" if _exec_rates else "—"
    _total_time_str = f"{_total_elapsed_ms / 1000:.1f}s 合计 / {_total_elapsed_ms / 1000 / total:.1f}s 均值" if total else "—"

    def _summary_row(label: str, value: str, note: str = "") -> str:
        note_cell = f'<td class="sum-note">{_esc(note)}</td>' if note else '<td class="sum-note">—</td>'
        return f"<tr><td class=\"sum-label\">{_esc(label)}</td><td class=\"sum-value\">{value}</td>{note_cell}</tr>"

    summary_section = ""
    if show_summary_section:
        summary_section = f"""
<div class="section section-summary">
  <h2>多轮汇总</h2>
  <div class="table-wrap">
    <table class="summary-metrics-table">
      <thead><tr><th>指标</th><th>值</th><th>说明</th></tr></thead>
      <tbody>
        {_summary_row("总 run 数", str(total), f"共执行 {total} 次独立 run")}
        {_summary_row("任务成功数 / 成功率", f"{success_count} / {int(round(success_rate * 100))}%", "run 级别 success=true 的数量")}
        {_summary_row("平均步数", str(avg_step), "每 run 的平均 trajectory 步数")}
        {_summary_row("平均执行成功率", _avg_exec_rate_str, "各 run execution_success_rate 的算术均值")}
        {_summary_row("总超时次数", str(_total_timeouts), "各 run timeout_count 之和")}
        {_summary_row("总恢复次数", str(_total_recoveries), "各 run recovery_count 之和（恢复计划触发次数）")}
        {_summary_row("总重试次数", str(_total_retries), "各 run retry_count 之和")}
        {_summary_row("总耗时 / 均值", _total_time_str, "含 LLM、RPA 交互、输出等待各项")}
      </tbody>
    </table>
  </div>
</div>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{_esc(report_title)}</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
      font-size: 15px;
      line-height: 1.6;
      color: #1e293b;
      background: linear-gradient(180deg, #f1f5f9 0%%, #e2e8f0 100%);
      margin: 0 auto;
      padding: 2rem clamp(1rem, 4vw, 3rem);
      max-width: 1200px;
      min-height: 100vh;
    }}
    .report-header {{
      margin-bottom: 2rem;
    }}
    h1 {{
      font-size: 1.75rem;
      font-weight: 700;
      color: #0f172a;
      margin: 0 0 0.5rem 0;
      letter-spacing: -0.02em;
    }}
    .summary-strip {{
      display: flex;
      flex-wrap: wrap;
      gap: 1rem;
      align-items: center;
      padding: 0.75rem 1rem;
      background: #fff;
      border-radius: 10px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.06);
      font-size: 0.95rem;
      color: #475569;
    }}
    .summary-strip strong {{ color: #0f172a; }}
    h2 {{
      font-size: 1.2rem;
      font-weight: 600;
      color: #1e40af;
      margin: 0 0 1rem 0;
      padding-bottom: 0.35rem;
      border-bottom: 2px solid #e0e7ff;
    }}
    h3 {{
      font-size: 1rem;
      font-weight: 600;
      color: #334155;
      margin: 1.25rem 0 0.5rem 0;
    }}
    .section {{
      background: #fff;
      padding: 1.5rem 1.75rem;
      border-radius: 12px;
      margin-bottom: 1.5rem;
      box-shadow: 0 1px 4px rgba(0,0,0,0.06);
      border: 1px solid rgba(0,0,0,0.04);
    }}
    .section ul {{ margin: 0.5rem 0; padding-left: 1.5rem; }}
    .section li {{ margin-bottom: 0.35rem; }}
    .summary-metrics-table {{
      width: auto;
      min-width: 560px;
      border-collapse: collapse;
      font-size: 0.92rem;
    }}
    .summary-metrics-table th {{
      background: #f1f5f9;
      color: #475569;
      font-weight: 600;
      padding: 0.45rem 1rem;
      border: 1px solid #e2e8f0;
      text-align: left;
      white-space: nowrap;
    }}
    .summary-metrics-table td {{
      padding: 0.4rem 1rem;
      border: 1px solid #e2e8f0;
      vertical-align: middle;
    }}
    .summary-metrics-table tr:nth-child(even) td {{ background: #f8fafc; }}
    .sum-label {{ color: #334155; font-weight: 500; white-space: nowrap; }}
    .sum-value {{ font-weight: 700; color: #0f172a; white-space: nowrap; }}
    .sum-note {{ color: #64748b; font-size: 0.86rem; }}
    .stats-list {{
      display: flex;
      flex-wrap: wrap;
      gap: 1.5rem 2rem;
      list-style: none;
      padding-left: 0;
      margin: 0;
    }}
    .stats-list li {{ margin: 0; display: flex; align-items: baseline; gap: 0.35rem; }}
    .stats-label {{ color: #64748b; font-size: 0.9rem; }}
    .stats-value {{ font-weight: 600; color: #0f172a; font-size: 1.05rem; }}
    .code {{
      background: #f8fafc;
      padding: 1rem;
      border-radius: 8px;
      border: 1px solid #e2e8f0;
      font-family: "Consolas", "Monaco", monospace;
      font-size: 0.875rem;
      line-height: 1.5;
      white-space: pre-wrap;
      word-break: break-word;
      max-height: 320px;
      overflow-y: auto;
    }}
    .note {{ color: #64748b; font-size: 0.9rem; margin: 0.5rem 0 0 0; }}
    .badge {{
      display: inline-block;
      padding: 0.25rem 0.6rem;
      border-radius: 999px;
      font-size: 0.8rem;
      font-weight: 600;
    }}
    .badge.success {{ background: #dcfce7; color: #166534; }}
    .badge.fail {{ background: #fee2e2; color: #b91c1c; }}
    .multi-round-table {{
      border-collapse: collapse;
      width: 100%;
      font-size: 0.9rem;
    }}
    .multi-round-table thead th {{
      background: #f1f5f9;
      color: #334155;
      font-weight: 600;
      padding: 0.65rem 1rem;
      text-align: left;
      border: 1px solid #e2e8f0;
      border-bottom: 2px solid #cbd5e1;
    }}
    .multi-round-table td {{
      padding: 0.65rem 1rem;
      border: 1px solid #e2e8f0;
      vertical-align: middle;
    }}
    .multi-round-table tbody tr:nth-child(4n+1) td:not([colspan]),
    .multi-round-table tbody tr:nth-child(4n+2) td:not([colspan]) {{ background: #fafafa; }}
    .multi-round-table .extra-metrics {{ font-size: 0.85rem; color: #64748b; }}
    .multi-round-table .col-run-id {{ font-family: Consolas, Monaco, monospace; font-size: 0.85rem; }}
    .section .table-wrap {{ overflow-x: auto; margin: 0.5rem 0 0 0; }}
    .round-detail {{
      padding: 1.25rem 0 0.5rem 0;
      font-size: 0.9rem;
      border-top: 1px dashed #e2e8f0;
    }}
    .round-detail > p {{ margin: 1rem 0 0.35rem 0; font-weight: 600; color: #475569; font-size: 0.9rem; }}
    .round-detail > p:first-child {{ margin-top: 0; }}
    .round-detail .round-io {{
      max-height: 260px;
      overflow-y: auto;
      margin: 0.35rem 0 1rem 0;
    }}
    .round-detail .round-output {{ max-height: 360px; overflow-y: auto; }}
    .round-detail .detail-block {{ margin-bottom: 1.25rem; }}
    .round-detail .detail-block:last-child {{ margin-bottom: 0; }}
    .round-detail .detail-value {{ margin: 0.25rem 0; font-family: Consolas, Monaco, monospace; font-size: 0.875rem; color: #334155; }}
    .block-brief-list {{ margin: 0.75rem 0; padding-left: 1.5rem; font-size: 0.875rem; line-height: 1.55; }}
    .block-brief-list li {{ margin-bottom: 0.4rem; }}
    .block-brief-pre {{ max-height: 140px; overflow-y: auto; font-size: 0.8rem; margin: 0.35rem 0; padding: 0.6rem; }}
    .block-list {{ line-height: 1.6; padding-left: 1.25rem; }}
    .block-list li {{ margin-bottom: 0.5rem; }}
    .llm-brief {{
      background: #f8fafc;
      padding: 0.75rem 1rem;
      border-radius: 8px;
      border: 1px solid #e2e8f0;
      font-size: 0.875rem;
      margin-top: 0.5rem;
    }}
    .llm-comment {{ display: block; margin-top: 0.35rem; color: #475569; font-style: normal; }}
    .toggle-detail {{
      cursor: pointer;
      padding: 0.3rem 0.75rem;
      font-size: 0.85rem;
      border: 1px solid #cbd5e1;
      border-radius: 8px;
      background: #fff;
      color: #475569;
    }}
    .toggle-detail:hover {{ background: #f1f5f9; color: #1e293b; }}
    .time-bar {{
      display: flex;
      width: 280px;
      height: 14px;
      border-radius: 4px;
      overflow: hidden;
      border: 1px solid #e2e8f0;
      background: #f8fafc;
    }}
    .time-bar-seg {{ height: 100%; min-width: 2px; }}
    .time-breakdown-table td {{ white-space: nowrap; }}
    .seg-llm     {{ background: #d97706; }}
    .seg-wait    {{ background: #7c3aed; }}
    .seg-rpa     {{ background: #1d4ed8; }}
    .seg-recover {{ background: #dc2626; }}
    .seg-other   {{ background: #64748b; }}
    .col-dot {{
      display: inline-block;
      width: 9px; height: 9px;
      border-radius: 2px;
      margin-right: 4px;
      vertical-align: middle;
      flex-shrink: 0;
    }}
    .time-breakdown-table th .col-dot-wrap {{
      display: flex; align-items: center; gap: 2px; justify-content: center;
    }}
    .llm-summary-section {{
      border-left: 4px solid #2563eb;
      background: linear-gradient(90deg, #f8fafc 0%%, #fff 8%);
    }}
    .llm-summary-text {{ max-height: none; overflow-y: visible; }}
    .llm-summary-text .llm-summary-h4 {{
      margin-top: 1.35rem;
      margin-bottom: 0.4rem;
      font-size: 1rem;
      font-weight: 600;
      color: #1e40af;
    }}
    .llm-summary-text .llm-summary-h4:first-child {{ margin-top: 0; }}
    .llm-summary-text .llm-summary-p {{ margin: 0.5rem 0; line-height: 1.7; color: #334155; }}
    .llm-summary-text .llm-summary-ul {{ margin: 0.5rem 0 0.5rem 1.25rem; line-height: 1.6; }}
    @media print {{
      body {{ background: #fff; padding: 1rem; }}
      .section {{ box-shadow: none; border: 1px solid #e2e8f0; }}
      .toggle-detail {{ display: none; }}
    }}
  </style>
</head>
<body>
  <header class="report-header">
    <h1>{_esc(report_title)}</h1>
    <div class="summary-strip">
      <span><strong>共 {total} 轮</strong></span>
      <span>成功率 <strong>{success_count}/{total}</strong> = {success_rate}</span>
      <span>平均步数 <strong>{avg_step}</strong></span>
      {agent_strip}
    </div>
  </header>

{input_section}

{summary_section}

{blocks_overview_section}
{timing_breakdown_section}

{rounds_table_html}
{run_level_blocks_section}
{llm_section}
</body>
</html>"""


def _plan_source_label(source: str, reason: str | None) -> str:
    """将 plan_source/reason 转为可读的「计划来源」文案。"""
    if source == "compound_block":
        return f"复合 Block（{reason or '固定流程'}）"
    if source == "llm":
        return "LLM 规划"
    if source == "replan_llm":
        return "恢复计划（LLM）"
    if source == "replan_rule":
        return "恢复计划（规则）"
    if source == "rule_fallback":
        return "规则兜底"
    return source or "—"


def _rpa_workflow_from_result(result: dict) -> str:
    """从单次 run 的 trajectory 提取 RPA 工作流程（按步顺序的 block/tool 序列），并标注计划来源。返回 HTML 片段。"""
    trajectory = result.get("trajectory") or []
    steps = []
    for entry in trajectory:
        sr = entry.get("step_result") or {}
        for tc in sr.get("tool_calls") or []:
            name = tc.get("tool_name")
            if name:
                steps.append(name)
    plan_source = result.get("plan_source", "")
    ph = result.get("plan_history") or []
    plan_reason = ph[0].get("reason") if isinstance(ph, list) and len(ph) > 0 and isinstance(ph[0], dict) else None

    parts = ['<h3>本轮 RPA 工作流程</h3>']
    if plan_source or plan_reason:
        label = _plan_source_label(plan_source, plan_reason)
        parts.append(f'<p class="note"><strong>计划来源</strong>: {_esc(label)}（复合 Block 为场景预置固定流程，未命中时才会使用 LLM 规划）</p>')
    if not steps:
        parts.append('<p class="note">（本 run 无轨迹，无法展示步骤序列）</p>')
    else:
        workflow_str = " → ".join(steps)
        parts.append(f'<p class="rpa-workflow">{_esc(workflow_str)}</p>')
    return "\n".join(parts)


def _section_input_multi(config: dict, task: dict, first_result: dict | None = None) -> str:
    """多轮报告用：仅展示实验配置与待测场景；可选展示本轮 RPA 工作流程（由 first_result 轨迹得出）。红框内容不展示。"""
    exp_id = config.get("experiment_id", "")
    scenario = config.get("scenario", "")
    task_spec_id = task.get("task_spec_id", "")
    description = task.get("description", "")
    scenario_spec_html = _scenario_spec_section(config)
    first_state = None
    if first_result:
        trajectory = first_result.get("trajectory") or []
        if trajectory:
            first_step = trajectory[0]
            first_state = ((first_step.get("step_result") or {}).get("agent_input_snapshot") or {}).get("state")
    collab_assignments_html = _collab_assignments_html(first_state if isinstance(first_state, dict) else None)
    rpa_workflow_html = _rpa_workflow_from_result(first_result) if first_result else ""
    return f"""
<div class="section">
  <h2>1. 实验配置与待测场景</h2>
  <h3>1.1 实验配置（B1 加载）</h3>
  <ul>
    <li><strong>experiment_id</strong>: {_esc(exp_id)}</li>
    <li><strong>scenario</strong>: {_esc(scenario)}</li>
    <li><strong>task_spec_ids</strong>: {_esc(json.dumps(config.get("task_spec_ids", []), ensure_ascii=False))}</li>
  </ul>
  <h3>1.2 待测场景（TaskSpec）</h3>
  <ul>
    <li><strong>task_spec_id</strong>: {_esc(task_spec_id)}</li>
    <li><strong>description</strong>: {_esc(description)}</li>
  </ul>
  {scenario_spec_html}
  {rpa_workflow_html}
  {collab_assignments_html}
</div>"""


def _format_per_round_llm_brief(llm_judge: dict | None) -> str:
    """将单轮 LLM-as-judge 结果格式化为「本轮 LLM 简要分析」HTML。"""
    if not llm_judge or not isinstance(llm_judge, dict):
        return '<p><strong>本轮 LLM 简要分析</strong>: <span class="note">（未启用或未获取到 LLM 评分）</span></p>'
    parts = []
    labels = {
        "decision_quality": "决策质量",
        "reasoning_coherence": "推理连贯性",
        "tool_proficiency": "工具熟练度",
        "output_quality": "输出内容评价",
        "safety_alignment": "安全性/对齐",
        "interpretability": "可解释性",
    }
    for key, label in labels.items():
        v = llm_judge.get(key)
        if v is not None:
            try:
                parts.append(f"{label}: {float(v):.2f}")
            except (TypeError, ValueError):
                parts.append(f"{label}: {v}")
    if not parts:
        return '<p><strong>本轮 LLM 简要分析</strong>: <span class="note">（无评分数据）</span></p>'
    body = "; ".join(parts)
    comment = llm_judge.get("output_comment")
    if comment:
        body += f'<br/><span class="llm-comment">评语: {_esc(str(comment).strip())}</span>'
    return f'<p><strong>本轮 LLM 简要分析</strong></p><div class="llm-brief">{body}</div>'


def _section_input(
    config: dict, task: dict, actual_initial_state: dict | None = None, result: dict | None = None
) -> str:
    """输入：实验配置 + 任务规范；若传入 result 则展示本轮 RPA 工作流程。红框内容不展示。"""
    exp_id = config.get("experiment_id", "")
    scenario = config.get("scenario", "")
    task_spec_id = task.get("task_spec_id", "")
    description = task.get("description", "")
    # 优先使用本 run 实际注入的 state（B9 可能已用 LLM 建议的 query 覆盖）
    display_state = actual_initial_state if actual_initial_state is not None else (task.get("initial_state") or {})
    state_str = json.dumps(display_state, ensure_ascii=False, indent=2)
    query = display_state.get("query", "")
    state_source = "本 run 实际使用的 state（可能含 LLM 建议的 query）" if actual_initial_state is not None else "TaskSpec 中的 initial_state"
    scenario_spec_html = _scenario_spec_section(config, heading="1.3 场景规范（ScenarioSpec）")
    rpa_workflow_html = _rpa_workflow_from_result(result) if result else ""
    collab_assignments_html = _collab_assignments_html(display_state if isinstance(display_state, dict) else None)
    return f"""
<div class="section">
  <h2>1. 输入</h2>
  <h3>1.1 实验配置（B1 加载）</h3>
  <ul>
    <li><strong>experiment_id</strong>: {_esc(exp_id)}</li>
    <li><strong>scenario</strong>: {_esc(scenario)}</li>
    <li><strong>task_spec_ids</strong>: {_esc(json.dumps(config.get("task_spec_ids", []), ensure_ascii=False))}</li>
  </ul>
  <h3>1.2 任务规范（TaskSpec）与本 run 实际 state</h3>
  <ul>
    <li><strong>task_spec_id</strong>: {_esc(task_spec_id)}</li>
    <li><strong>description</strong>: {_esc(description)}</li>
    <li><strong>{_esc(state_source)}</strong>: <pre class="code">{_esc(state_str)}</pre></li>
  </ul>
  {scenario_spec_html}
  {rpa_workflow_html}
  <p class="note">本流程中，<strong>发送给待测 Agent（Poffices 页面产品）的查询内容</strong>为：<strong>{_esc(query or "（未设置）")}</strong></p>
  {collab_assignments_html}
</div>"""


def _section_output(
    result: dict,
    trajectory: list,
    metrics: dict,
    steps_run: int,
) -> str:
    """输出：评估结果 + 业务输出（如 Poffices 响应）。"""
    success = metrics.get("success", None)
    step_count = metrics.get("step_count", steps_run)
    run_id = result.get("run_id", "")

    # 从最后一步提取 poffices_response / final_report（若有），再 strip 出正文
    raw_output = ""
    inferred_success = None
    if trajectory:
        last_step = trajectory[-1]
        last_execution_results = (
            last_step.get("step_result") or {}
        ).get("execution_results") or []
        if last_execution_results:
            inferred_success = all(bool(er.get("success")) for er in last_execution_results)

        for er in (last_step.get("step_result") or {}).get("execution_results") or []:
            delta = er.get("ui_state_delta") or {}
            for key in ("poffices_response", "final_report"):
                if key in delta and delta[key]:
                    v = delta[key]
                    raw_output = (v if isinstance(v, str) else str(v)).strip()
                    break
            if raw_output:
                break
    business_output = extract_last_report_from_full_output(raw_output, take_last=True) if raw_output else ""

    if success is None:
        success = bool(inferred_success)
    else:
        success = bool(success)

    # 如果「指标成功」和「执行结果推断成功」不一致，给出提示，避免误导。
    mismatch_hint = ""
    if inferred_success is not None and success is False and inferred_success is True:
        mismatch_hint = "（推断：执行层面成功）"
    elif inferred_success is not None and success is True and inferred_success is False:
        mismatch_hint = "（推断：执行层面失败）"

    success_badge = "success" if success else "fail"
    return f"""
<div class="section">
  <h2>2. 输出</h2>
  <h3>2.1 评估结果（B8）</h3>
  <ul>
    <li><strong>run_id</strong>: {_esc(run_id)}</li>
    <li><strong>任务是否成功</strong>: <span class="badge {success_badge}">{success}</span> {_esc(mismatch_hint)}</li>
    <li><strong>实际步数</strong>: {step_count}</li>
  </ul>
  <h3>2.2 业务输出（本 run 的最终结果）</h3>
  <p>本 run 待测 Agent（Poffices 页面产品）的响应：B7 执行 <code>poffices_query</code> 或 <code>get_response</code> 后，从页面提取的响应如下。</p>
  {_collab_note_html(result)}
  <pre class="code output-block">{_esc(business_output or "（无提取到的响应）")}</pre>
</div>"""


def _section_blocks(
    config: dict,
    task: dict,
    result: dict,
    trajectory: list,
    orchestration_mode: str,
    run_id: str,
) -> str:
    """各 Block 运作说明。"""
    parts = []

    # B1
    parts.append(f"""
  <li><strong>B1 Experiment Config &amp; TaskSpec</strong><br/>
    加载实验配置 <code>{_esc(config.get("experiment_id", ""))}</code> 与任务 <code>{_esc(task.get("task_spec_id", ""))}</code>，为 B9 提供本次 run 的 task_spec 与 initial_state。</li>""")

    # B2
    route_type = result.get("route_type", "single_flow")
    parts.append(f"""
  <li><strong>B2 Difficulty &amp; Routing</strong><br/>
    本 run 为 <code>{_esc(orchestration_mode)}</code>，未使用多流 DAG 时路由为 single_flow。若为 multi_agent_dag 则 B2 输出 route_type（如 {_esc(route_type)}) 供 B3 建 DAG。</li>""")

    # B3 / B4
    parts.append("""
  <li><strong>B3 Workflow Manager (DAG)</strong> / <strong>B4 Agent Scheduler</strong><br/>
    单 Agent 模式下为线性步进，无 DAG；每步由 B4 分配给同一 B6 决策组件（如 PofficesAgent）。</li>""")

    # B5
    parts.append(f"""
  <li><strong>B5 State &amp; Trajectory Manager</strong><br/>
    维护 current_step_index、state（含 initial_state）、last_execution_result；每步记录 step_result（tool_calls + execution_results），本 run 共 {len(trajectory)} 条轨迹。</li>""")

    # B6
    b6_steps = []
    for i, entry in enumerate(trajectory):
        sr = entry.get("step_result") or {}
        tcs = sr.get("tool_calls") or []
        b6_steps.append(f"步 {i}: " + ", ".join(f"{tc.get('tool_name')}({json.dumps(tc.get('params') or {}, ensure_ascii=False)})" for tc in tcs))
    parts.append(f"""
  <li><strong>B6 决策组件（如 PofficesAgent / PofficesLLMAgent）</strong><br/>
    根据 agent_input_context（state + last_execution_result）每步输出 tool_calls：<br/>
    <pre class="code">{_esc("\n".join(b6_steps))}</pre></li>""")

    # B7
    b7_steps = []
    for i, entry in enumerate(trajectory):
        sr = entry.get("step_result") or {}
        for er in sr.get("execution_results") or []:
            name = er.get("tool_name", "")
            ok = er.get("success", False)
            b7_steps.append(f"步 {i} {name}: success={ok}")
    parts.append(f"""
  <li><strong>B7 RPA Adapter（PofficesRPA）</strong><br/>
    执行 B6 下发的 tool_calls，在真实 Poffices 页面上执行 poffices_bootstrap / poffices_query，返回 ExecutionResult（success、raw_response、ui_state_delta）。<br/>
    <pre class="code">{_esc("\n".join(b7_steps))}</pre></li>""")

    # B8
    metrics = result.get("metrics") or {}
    parts.append(f"""
  <li><strong>B8 Evaluators &amp; Metrics</strong><br/>
    根据轨迹与 TaskSpec 判定任务成功与否、步数，写入 RunMetrics；轨迹已落盘至 log_dir。</li>""")

    # B9
    parts.append("""
  <li><strong>B9 Orchestrator</strong><br/>
    串联 B2–B7：初始化 B5 state → 每轮取 agent_input_context → 调用 B6 agent.run() → 对每个 tool_call 调用 B7 rpa.execute() → B5.record_step() → 直至无 tool_calls 或达 max_steps；最后调用 B8 落盘与评估。</li>""")

    return f"""
<div class="section">
  <h2>3. 各 Block 运作</h2>
  <p>本 run 中 B1–B9 的职责与数据流简述如下。</p>
  <ol class="block-list">
{"".join(parts)}
  </ol>
</div>"""


def _wrap_html(
    *,
    title: str,
    run_id: str,
    input_section: str,
    output_section: str,
    blocks_section: str,
) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{_esc(title)}</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ font-family: system-ui, "Segoe UI", sans-serif; margin: 1rem auto; padding: 0 2rem; background: #f0f4f8; color: #1a1a1a; width: 92%; max-width: 1400px; }}
    h1 {{ color: #1565c0; border-bottom: 2px solid #1565c0; padding-bottom: 0.5rem; }}
    h2 {{ color: #0d47a1; margin-top: 1.5rem; }}
    h3 {{ color: #37474f; margin-top: 1rem; font-size: 1rem; }}
    .section {{ background: #fff; padding: 1rem 1.5rem; border-radius: 8px; margin-bottom: 1rem; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
    .code {{ background: #f5f5f5; padding: 0.75rem; border-radius: 6px; overflow-x: auto; font-size: 0.9rem; white-space: pre-wrap; word-break: break-word; max-height: 320px; overflow-y: auto; }}
    .output-block {{ max-height: 400px; }}
    .note {{ color: #546e7a; font-size: 0.95rem; }}
    .badge {{ padding: 2px 8px; border-radius: 4px; font-weight: 600; }}
    .badge.success {{ background: #c8e6c9; color: #1b5e20; }}
    .badge.fail {{ background: #ffcdd2; color: #b71c1c; }}
    .block-list {{ line-height: 1.6; }}
    .block-list li {{ margin-bottom: 0.75rem; }}
  </style>
</head>
<body>
  <h1>{_esc(title)}</h1>
  <p class="note">run_id: <code>{_esc(run_id)}</code> · 跑完完整流程后生成（输入 → 各 Block 运作 → 输出）</p>

{input_section}
{output_section}
{blocks_section}

</body>
</html>"""


def _esc(s: str) -> str:
    if s is None:
        return ""
    s = str(s)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_report_from_log_dir(
    log_dir: Path,
    output_path: Path | None = None,
) -> str:
    """
    从轨迹目录中取最新一条轨迹 JSON，生成同格式的测试报告。
    用于「跑完流程后未保存 result 时」从 log_dir 重新生成报告。
    """
    log_dir = Path(log_dir)
    if not log_dir.exists():
        raise FileNotFoundError(f"log_dir not found: {log_dir}")
    json_files = sorted(log_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not json_files:
        raise FileNotFoundError(f"no *.json in {log_dir}")
    data = json.loads(json_files[0].read_text(encoding="utf-8"))
    task_spec = TaskSpec.model_validate(data.get("task_spec", {}))
    metrics = evaluate_trajectory(
        data["trajectory"],
        task_spec,
        run_id=data.get("run_id", ""),
    )
    extra = data.get("extra") or {}
    if not isinstance(extra, dict):
        extra = {}

    result = {
        "trajectory": data["trajectory"],
        "steps_run": len(data["trajectory"]),
        "metrics": metrics.model_dump(),
        "run_id": data.get("run_id", ""),
        "orchestration_mode": "single_agent",
        "route_type": "single_flow",
    }
    if isinstance(extra.get("goal_intent"), dict):
        result["goal_intent"] = extra.get("goal_intent")
    config = {
        "experiment_id": data.get("experiment_id", ""),
        "scenario": "poffices-agent",
        "task_spec_ids": [data.get("task_spec_id", "")],
    }
    task = data.get("task_spec", {})
    return build_flow_report(result, config, task, output_path=output_path)


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="从轨迹目录生成流程测试报告（输入/输出/各 Block）")
    p.add_argument("log_dir", type=Path, nargs="?", default=_root / "logs" / "poffices", help="轨迹目录")
    p.add_argument("-o", "--output", type=Path, default=None, help="输出 HTML 路径（默认 log_dir/test_report.html）")
    args = p.parse_args()
    out = args.output or args.log_dir / "test_report.html"
    build_report_from_log_dir(args.log_dir, output_path=out)
    print(f"报告已写入: {out}")


if __name__ == "__main__":
    main()
