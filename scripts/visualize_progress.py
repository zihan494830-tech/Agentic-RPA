#!/usr/bin/env python
"""扫描 ART 项目，生成 Block 实现进度 JSON，并输出带数据流说明的 HTML 可视化。
用法：在项目根目录执行 python scripts/visualize_progress.py，将生成 progress.html（及 progress.json）。
"""
from pathlib import Path
import json


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _dir_has_real_impl(dir_path: Path) -> bool:
    """目录下除 __init__.py 外是否有其它 .py 实现。"""
    if not dir_path.is_dir():
        return False
    py_files = [f for f in dir_path.iterdir() if f.suffix == ".py" and f.name != "__init__.py"]
    return len(py_files) > 0


def _file_exists(root: Path, *parts: str) -> bool:
    return (root / Path(*parts)).exists()


def scan_blocks(root: Path) -> list[dict]:
    """根据 raft/ 目录结构检测各 Block 实现状态。"""
    raft = root / "raft"
    blocks = []

    # B1: Experiment Config & TaskSpec
    b1 = raft / "core" / "config"
    has_loader = _file_exists(root, "raft", "core", "config", "loader.py")
    blocks.append({
        "id": "B1",
        "name": "Experiment Config & TaskSpec",
        "layer": "experiment",
        "status": "implemented" if has_loader else "not_started",
        "detail": "loader.py 加载 experiment.json / task_specs.json",
    })

    # B2: Difficulty & Routing
    b2 = raft / "core" / "difficulty"
    blocks.append({
        "id": "B2",
        "name": "Difficulty & Routing",
        "layer": "orchestrator",
        "status": "implemented" if _dir_has_real_impl(b2) else "not_started",
        "detail": "难度与路由（single/multi 等）",
    })

    # B3: Workflow Manager (DAG)
    b3 = raft / "core" / "dag"
    blocks.append({
        "id": "B3",
        "name": "Workflow Manager (DAG)",
        "layer": "orchestrator",
        "status": "implemented" if _dir_has_real_impl(b3) else "not_started",
        "detail": "WorkflowDAG / next_steps",
    })

    # B4: Agent Scheduler & Tool Router
    b4 = raft / "core" / "scheduler"
    blocks.append({
        "id": "B4",
        "name": "Agent Scheduler & Tool Router",
        "layer": "orchestrator",
        "status": "implemented" if _dir_has_real_impl(b4) else "not_started",
        "detail": "步骤 → Agent / 工具路由",
    })

    # B5: State & Trajectory Manager
    has_b5 = _file_exists(root, "raft", "core", "state", "manager.py")
    blocks.append({
        "id": "B5",
        "name": "State & Trajectory Manager",
        "layer": "orchestrator",
        "status": "implemented" if has_b5 else "not_started",
        "detail": "状态 + 最近 ExecutionResult → Agent 输入",
    })

    # B6: Agent Runtime
    agents = raft / "agents"
    has_mock = _file_exists(root, "raft", "agents", "mock_agent.py")
    has_llm = _file_exists(root, "raft", "agents", "llm_agent.py")
    if has_llm and has_mock:
        status_b6 = "implemented"
        detail_b6 = "MockAgent + LLMAgent"
    elif has_mock:
        status_b6 = "partial"
        detail_b6 = "MockAgent（可接 LLM）"
    else:
        status_b6 = "not_started"
        detail_b6 = ""
    blocks.append({"id": "B6", "name": "Agent Runtime (Single/Multi)", "layer": "agent", "status": status_b6, "detail": detail_b6})

    # B7: RPA Adapter
    rpa = raft / "rpa"
    has_mock_rpa = _file_exists(root, "raft", "rpa", "mock_rpa.py")
    has_playwright = _file_exists(root, "raft", "rpa", "playwright_rpa.py")
    if has_playwright and has_mock_rpa:
        status_b7 = "implemented"
        detail_b7 = "MockRPA + PlaywrightRPA"
    elif has_mock_rpa:
        status_b7 = "partial"
        detail_b7 = "MockRPA（可接真实 RPA）"
    else:
        status_b7 = "not_started"
        detail_b7 = ""
    blocks.append({"id": "B7", "name": "RPA Adapter & ExecutionResult", "layer": "agent", "status": status_b7, "detail": detail_b7})

    # B8: Evaluators & Metrics
    has_b8 = _file_exists(root, "raft", "evaluation", "metrics.py")
    blocks.append({
        "id": "B8",
        "name": "Evaluators & Metrics",
        "layer": "evaluation",
        "status": "implemented" if has_b8 else "not_started",
        "detail": "轨迹落盘、success/step_count、GT 比对",
    })

    # B9: Orchestrator
    has_b9 = _file_exists(root, "raft", "orchestrator", "runner.py")
    blocks.append({
        "id": "B9",
        "name": "Orchestrator",
        "layer": "orchestrator",
        "status": "implemented" if has_b9 else "not_started",
        "detail": "串联 B5/B6/B7，闭环 1，可选 B8",
    })

    return blocks


def build_html(blocks: list[dict], root: Path) -> str:
    """生成带四层架构与数据流说明的 HTML，块按 status 高亮。"""
    data_flows = [
        {"from": "layer1", "to": "layer2", "label": "Config, TaskSpec"},
        {"from": "layer2", "to": "layer3", "label": "步骤 → Agent；tool_calls → RPA"},
        {"from": "B7", "to": "B5", "label": "ExecutionResult"},
        {"from": "B5", "to": "B6", "label": "state + 最近结果（闭环 1）"},
        {"from": "layer3", "to": "layer4", "label": "trajectory"},
        {"from": "layer4", "to": "layer2", "label": "metrics → 下一批（闭环 2）", "dashed": True},
    ]
    status_color = {"implemented": "#2e7d32", "partial": "#f9a825", "not_started": "#9e9e9e"}
    status_text = {"implemented": "已实现", "partial": "部分", "not_started": "未实现"}

    blocks_by_id = {b["id"]: b for b in blocks}
    layer_order = ["experiment", "orchestrator", "agent", "evaluation"]
    layer_names = {
        "experiment": "实验与任务层",
        "orchestrator": "实验器层",
        "agent": "智能体与 RPA 执行层",
        "evaluation": "评估与指标层",
    }

    rows = []
    for layer in layer_order:
        layer_blocks = [b for b in blocks if b["layer"] == layer]
        if not layer_blocks and layer == "experiment":
            layer_blocks = [{"id": "L1", "name": "Experiment Config & TaskSpec (B1)", "layer": "experiment", "status": blocks_by_id.get("B1", {}).get("status", "not_started"), "detail": ""}]
        elif not layer_blocks:
            continue
        box_html = []
        for b in layer_blocks:
            color = status_color.get(b["status"], "#9e9e9e")
            text = status_text.get(b["status"], b["status"])
            box_html.append(
                f'<div class="block" data-id="{b["id"]}" style="--block-color:{color}" title="{b.get("detail", "")}">'
                f'<span class="block-id">{b["id"]}</span> '
                f'<span class="block-name">{b["name"]}</span> '
                f'<span class="block-status">{text}</span>'
                f'</div>'
            )
        rows.append(f'<div class="layer" id="layer-{layer}"><div class="layer-title">{layer_names[layer]}</div><div class="layer-blocks">{"".join(box_html)}</div></div>')

    flows_html = []
    for f in data_flows:
        dashed = " flow-dashed" if f.get("dashed") else ""
        flows_html.append(f'<div class="flow{dashed}" data-from="{f["from"]}" data-to="{f["to"]}"><span class="flow-label">{f["label"]}</span></div>')

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>ART 实现进度与数据流</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: system-ui, sans-serif; margin: 16px; background: #f5f5f5; }}
h1 {{ font-size: 1.25rem; color: #333; }}
.diagram {{ display: flex; flex-direction: column; gap: 8px; max-width: 900px; }}
.layer {{ background: #fff; border-radius: 8px; padding: 12px; border: 1px solid #e0e0e0; }}
.layer-title {{ font-weight: 600; color: #555; margin-bottom: 8px; font-size: 0.9rem; }}
.layer-blocks {{ display: flex; flex-wrap: wrap; gap: 8px; }}
.block {{ border-left: 4px solid var(--block-color); padding: 8px 12px; background: #fafafa; border-radius: 4px; font-size: 0.85rem; }}
.block-id {{ font-weight: 700; color: #333; }}
.block-status {{ margin-left: 6px; font-size: 0.75rem; color: var(--block-color); }}
.flows {{ margin: 12px 0; padding: 12px; background: #fff; border-radius: 8px; border: 1px solid #e0e0e0; }}
.flows h2 {{ font-size: 1rem; margin: 0 0 8px 0; color: #555; }}
.flow {{ padding: 4px 0; border-left: 3px solid #1976d2; padding-left: 8px; margin: 4px 0; font-size: 0.8rem; color: #333; }}
.flow-dashed {{ border-left-style: dashed; color: #666; }}
.flow-label {{ font-style: italic; }}
.footer {{ margin-top: 16px; font-size: 0.75rem; color: #888; }}
</style>
</head>
<body>
<h1>ART 实现进度与数据流</h1>
<div class="diagram">
{chr(10).join(rows)}
</div>
<div class="flows">
<h2>数据流简述</h2>
<p style="font-size:0.8rem;color:#666;">任务层 → Orchestrator → Agent 与 RPA 执行 → 轨迹与状态回写 → 评估层。</p>
<div class="flow"><span class="flow-label">Config, TaskSpec</span> → 实验器层（B1 加载）</div>
<div class="flow"><span class="flow-label">Orchestrator</span> 驱动步骤 → Agent 得到 state + 最近 ExecutionResult；Agent 输出 tool_calls → RPA</div>
<div class="flow"><span class="flow-label">RPA</span> 返回 ExecutionResult → B5 State & Trajectory</div>
<div class="flow"><span class="flow-label">B5</span> 将 state + 最近 ExecutionResult 注入下一轮 Agent（<strong>闭环 1</strong>）</div>
<div class="flow"><span class="flow-label">trajectory</span> → B8 评估（success、step_count、落盘）</div>
<div class="flow flow-dashed"><span class="flow-label">B8 metrics</span> → Orchestrator 下一批实验调策略（<strong>闭环 2</strong>，后续优化）</div>
</div>
<div class="footer">生成自 scripts/visualize_progress.py · 绿=已实现 黄=部分 灰=未实现</div>
<script>
const data = {json.dumps(blocks, ensure_ascii=False)};
console.log("Blocks:", data);
</script>
</body>
</html>"""
    return html


def main() -> None:
    root = _project_root()
    blocks = scan_blocks(root)
    out_json = root / "progress.json"
    out_html = root / "progress.html"
    out_json.write_text(json.dumps(blocks, ensure_ascii=False, indent=2), encoding="utf-8")
    out_html.write_text(build_html(blocks, root), encoding="utf-8")
    print("已生成:", out_json, out_html)
    print("在浏览器中打开 progress.html 查看。")


if __name__ == "__main__":
    main()
