"""
接入 LLM 完成「整理并输出」多轮测试报告。

一、模块逻辑（沿用原有报告要求）
---------------------------------
1. 从多轮 results 构建 rounds_summaries：每轮含 run_id、success、step_count、details、
   llm_judge（单轮 LLM 评分，来自 B8）、output_snippet（约 1200 字输出原文）。
2. 调用 LLM 生成「多轮分析总结」文案（summarize_multi_rounds），要求有据可依、引用输出原文、
   遵循评估基准时间/地点。
3. 调用与之前一致的 HTML 报告构建（scripts/build_flow_report.build_multi_flow_report），
   生成：实验配置与待测场景、各 Block 运作、多轮汇总、多轮明细（每轮完整输入/输出、本轮 LLM 简要分析、
   本 run 各 Block 运作）、LLM 多轮分析总结。排版与格式与原有报告一致。

三、待测 Agent 输出范围
----------------------
每一轮开头和结尾的「系统格式」不计入待测 Agent 的输出范围内（见 output_scope 模块）。
仅去除首尾系统格式后的内容用于：各轮摘要中的 output_snippet、报告中的「本轮输出」、
以及 LLM 多轮分析总结时的引用依据。

四、与 LLM-as-judge 的关系（包含关系，不是两个 LLM）
--------------------------------------------------
- LLM-as-judge 能力集中在 raft.core.llm_judge，同一套 API（OPENAI_API_KEY / RAFT_LLM_PROVIDER 等）。
- 两处调用、同一模块：
  (1) 单轮判分 judge_trajectory：在 B8/Orchestrator 落盘时调用，对**每一轮**轨迹打分
      （decision_quality、reasoning_coherence、tool_proficiency、output_quality、safety_alignment、
      interpretability、output_comment），结果写入该轮的 metrics.llm_judge。
  (2) 多轮总结 summarize_multi_rounds：在**本模块**中调用，仅**一次**，根据各轮摘要
      （含各轮已有的 llm_judge 与输出原文）生成整份「LLM 多轮分析总结」段落。
- 报告模块不新增第二个 LLM：它**复用** (1) 的产出（每轮 llm_judge 用于「本轮 LLM 简要分析」），
  并**调用** (2) 得到多轮总结段落，再交给同一套 HTML 模板输出。因此是「报告模块包含/使用
  LLM-as-judge 模块」，不是两个独立的 LLM 系统。

用法：
  from raft.reporting import build_report_with_llm
  build_report_with_llm(results, config, task, output_path=Path("logs/poffices/run_report.html"))
"""
from pathlib import Path
from typing import Any

from raft.core.llm_judge import summarize_multi_rounds
from raft.reporting.multi_agent import get_per_agent_segments
from raft.reporting.output_scope import strip_system_format_from_agent_output


def _prepare_rounds_summaries(results: list[dict]) -> list[dict]:
    """从多轮 run 的 results 构建供 LLM 总结用的各轮摘要。多 Agent 时按 Agent 拆成多条，每轮均执行：从标题到参考文献的截取。"""
    summaries = []
    for r in results:
        metrics = r.get("metrics") or {}
        traj = r.get("trajectory") or []
        run_id = r.get("run_id", "")

        segments = get_per_agent_segments(r)
        if segments and len(segments) > 1:
            for seg in segments:
                raw_output = seg.get("output_raw") or ""
                output_snippet = strip_system_format_from_agent_output(raw_output)
                if output_snippet:
                    output_snippet = output_snippet[:1200]
                summaries.append({
                    "run_id": f"{run_id} · {seg['agent_name']}",
                    "success": seg.get("success", False),
                    "step_count": 3,
                    "details": metrics.get("details"),
                    "metrics_details": metrics.get("details"),
                    "llm_judge": metrics.get("llm_judge"),
                    "output_snippet": output_snippet or None,
                })
            continue

        raw_output = ""
        if traj:
            for er in (traj[-1].get("step_result") or {}).get("execution_results") or []:
                ui = er.get("ui_state_delta") or {}
                delta = ui.get("poffices_response") or ui.get("response")
                if delta:
                    raw_output = delta if isinstance(delta, str) else str(delta)
                    break
        output_snippet = strip_system_format_from_agent_output(raw_output)
        if output_snippet:
            output_snippet = output_snippet[:1200]
        summaries.append({
            "run_id": run_id,
            "success": metrics.get("success", False),
            "step_count": metrics.get("step_count", r.get("steps_run", 0)),
            "details": metrics.get("details"),
            "metrics_details": metrics.get("details"),
            "llm_judge": metrics.get("llm_judge"),
            "output_snippet": output_snippet or None,
        })
    return summaries


def _get_multi_flow_report_builder():  # noqa: ANN202
    """延迟导入 scripts 下的 build_multi_flow_report，避免 raft 对 scripts 的静态依赖。"""
    import sys
    root = Path(__file__).resolve().parents[2]
    scripts_dir = root / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from build_flow_report import build_multi_flow_report  # type: ignore[import-untyped]
    return build_multi_flow_report


def build_report_with_llm(
    results: list[dict],
    config: dict,
    task: dict,
    *,
    output_path: Path | str | None = None,
    provider: str | None = None,
    use_llm_summary: bool = True,
    rounds_rationale: str | None = None,
    minimal_report: bool = False,
) -> dict[str, Any]:
    """
    使用 LLM 整理并输出多轮测试报告。

    - 根据 results 构建各轮摘要，调用 LLM 生成多轮分析总结（若 use_llm_summary 且 API 可用）。
    - 生成完整 HTML 报告（实验配置、各 Block 运作、多轮明细、LLM 总结）；若指定 output_path 则写入文件。
    - rounds_rationale 若提供则展示在报告中的「测试轮数决定依据」。
    - minimal_report=True：不调用 LLM 总结，报告仅展示输入、输出、轨迹与各 Block 步骤，不展示任何打分或 LLM 简要分析。
    """
    llm_summary = None
    report_gen_llm_ms: int | None = None
    if results and use_llm_summary and not minimal_report:
        rounds_summaries = _prepare_rounds_summaries(results)
        try:
            print("正在生成 LLM 多轮分析总结（依赖配置的 LLM API，可能需要 1–3 分钟）…", flush=True)
            from raft.core.llm_timing import attach_llm_timing_sink, reset_llm_timing_sink

            _rep_events: list[dict] = []
            _rep_tok = attach_llm_timing_sink(_rep_events)
            try:
                llm_summary = summarize_multi_rounds(
                    rounds_summaries,
                    task,
                    provider=provider,
                )
            finally:
                reset_llm_timing_sink(_rep_tok)
            report_gen_llm_ms = sum(
                int(e.get("elapsed_ms", 0))
                for e in _rep_events
                if isinstance(e, dict) and isinstance(e.get("elapsed_ms"), (int, float))
            )
        except Exception:
            pass

    build_multi_flow_report = _get_multi_flow_report_builder()
    html = build_multi_flow_report(
        results,
        config,
        task,
        output_path=None,
        llm_summary=llm_summary,
        rounds_rationale=rounds_rationale,
        minimal_report=minimal_report,
        report_generation_llm_ms=report_gen_llm_ms,
    )

    out_path = None
    if output_path is not None:
        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html, encoding="utf-8")

    return {
        "llm_summary": llm_summary,
        "html": html,
        "output_path": out_path,
    }
