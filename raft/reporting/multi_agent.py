"""多 Agent 场景下报告与摘要的按 Agent 拆分逻辑。与 scripts/build_flow_report 共用。"""


def get_per_agent_segments(result: dict) -> list[dict] | None:
    """
    若本 run 为多 Agent（trajectory 中多次出现 app_ready→send_query→get_response 且 agent_name 不同），
    按每 3 步一组拆成多个 segment，返回 [{agent_name, success, output_raw}]；否则返回 None（表示不展开）。
    """
    trajectory = result.get("trajectory") or []
    if len(trajectory) < 3:
        return None
    segments: list[dict] = []
    idx = 0
    while idx + 2 < len(trajectory):
        sr0 = (trajectory[idx].get("step_result") or {}).get("tool_calls") or []
        sr1 = (trajectory[idx + 1].get("step_result") or {}).get("tool_calls") or []
        sr2 = (trajectory[idx + 2].get("step_result") or {}).get("tool_calls") or []
        if not sr0 or not sr1 or not sr2:
            idx += 1
            continue
        name0 = (sr0[0].get("tool_name") or "").strip()
        name1 = (sr1[0].get("tool_name") or "").strip()
        name2 = (sr2[0].get("tool_name") or "").strip()
        if name0 != "app_ready" or name1 != "send_query" or name2 != "get_response":
            idx += 1
            continue
        opts = (sr0[0].get("params") or {}).get("options") or {}
        agent_name = (opts.get("agent_name") or "Agent").strip() if isinstance(opts, dict) else "Agent"
        ers = (trajectory[idx + 2].get("step_result") or {}).get("execution_results") or []
        seg_success = ers[0].get("success", False) if ers else False
        output_raw = ""
        for er in ers:
            delta = er.get("ui_state_delta") or {}
            for key in ("poffices_response", "response_text", "response", "output"):
                if key in delta and delta[key]:
                    v = delta[key]
                    output_raw = (v if isinstance(v, str) else str(v)).strip()
                    break
            if output_raw:
                break
        if not output_raw and ers:
            rr = ers[0].get("raw_response")
            if isinstance(rr, str) and rr.strip():
                output_raw = f"（本步执行失败）{rr.strip()}"[:2000]
            elif isinstance(rr, dict) and rr.get("response"):
                output_raw = f"（本步执行失败）{str(rr.get('response'))[:1500]}"
        segments.append({"agent_name": agent_name, "success": seg_success, "output_raw": output_raw})
        idx += 3
    if len(segments) <= 1:
        return None
    return segments
