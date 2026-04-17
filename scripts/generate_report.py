#!/usr/bin/env python
"""B8 报告：从轨迹目录读取所有 run，评估并输出汇总报告（JSON + 可选 HTML）。供迁移到其他平台后批量测真实 Agent 并出报告。"""
import json
import sys
from pathlib import Path

# 保证项目根在 path 中
_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from raft.contracts.models import TaskSpec
from raft.evaluation.metrics import evaluate_trajectory


def load_trajectory_file(path: Path) -> dict | None:
    """读取单条轨迹 JSON，返回 payload（含 run_id, task_spec, trajectory, step_count）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data
    except Exception:
        return None


def generate_report(log_dir: Path, output_path: Path | None = None, format: str = "json") -> dict:
    """
    扫描 log_dir 下所有 *_*.json（或 *.json），对每条轨迹做 B8 评估，汇总为报告。
    返回：{ "runs": [ { run_id, task_spec_id, success, step_count, ... } ], "summary": { total, success_count, ... } }
    """
    log_dir = Path(log_dir)
    if not log_dir.exists():
        return {"error": f"log_dir not found: {log_dir}", "runs": [], "summary": {}}

    runs: list[dict] = []
    for path in sorted(log_dir.glob("*.json")):
        data = load_trajectory_file(path)
        if not data or "trajectory" not in data:
            continue
        task_spec = TaskSpec.model_validate(data.get("task_spec", {}))
        run_id = data.get("run_id", path.stem)
        metrics = evaluate_trajectory(
            data["trajectory"],
            task_spec,
            run_id=run_id,
        )
        runs.append({
            "run_id": run_id,
            "experiment_id": data.get("experiment_id", ""),
            "task_spec_id": data.get("task_spec_id", ""),
            "success": metrics.success,
            "step_count": metrics.step_count,
            "details": metrics.details,
        })

    success_count = sum(1 for r in runs if r["success"])
    summary = {
        "total_runs": len(runs),
        "success_count": success_count,
        "success_rate": round(success_count / len(runs), 2) if runs else 0,
        "avg_step_count": round(sum(r["step_count"] for r in runs) / len(runs), 1) if runs else 0,
    }
    report = {"runs": runs, "summary": summary}

    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if format == "html":
            html = _report_to_html(report)
            output_path.write_text(html, encoding="utf-8")
        else:
            output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    return report


def _report_to_html(report: dict) -> str:
    """简单 HTML 报告。"""
    runs = report.get("runs", [])
    summary = report.get("summary", {})
    rows = "".join(
        f"<tr><td>{r['run_id']}</td><td>{r['task_spec_id']}</td><td>{r['success']}</td><td>{r['step_count']}</td></tr>"
        for r in runs
    )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>ART 评估报告</title></head>
<body>
<h1>ART 评估报告</h1>
<h2>汇总</h2>
<ul>
<li>总 run 数: {summary.get('total_runs', 0)}</li>
<li>成功数: {summary.get('success_count', 0)}</li>
<li>成功率: {summary.get('success_rate', 0)}</li>
<li>平均步数: {summary.get('avg_step_count', 0)}</li>
</ul>
<h2>明细</h2>
<table border="1">
<tr><th>run_id</th><th>task_spec_id</th><th>success</th><th>step_count</th></tr>
{rows}
</table>
</body></html>"""


def generate_poffices_report(
    query: str,
    response: str,
    rpas_called: list[str],
    output_path: Path | None = None,
    format: str = "html",
) -> dict:
    """
    生成 Poffices 单次测试报告：输入（Query）、输出（响应）、调用的 RPA 步骤。
    返回报告字典；若指定 output_path 则写入文件（json 或 html）。
    """
    report = {
        "input": {"query": query},
        "output": {"response": response},
        "rpas_called": rpas_called,
    }
    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if format == "html":
            html = _poffices_report_to_html(report)
            output_path.write_text(html, encoding="utf-8")
        else:
            output_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    return report


def _poffices_report_to_html(report: dict) -> str:
    """Poffices 测试报告 HTML，风格与既有 poffices_report.html 一致。"""
    query = report.get("input", {}).get("query", "")
    response = report.get("output", {}).get("response", "")
    rpas = report.get("rpas_called", [])
    # 转义 HTML 显示
    query_esc = query.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    response_esc = (
        response.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    rows = "".join(f"<li>{r}</li>" for r in rpas)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Poffices Market Analysis Agent 测试报告</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: system-ui, "Segoe UI", sans-serif; margin: 1rem 2rem; background: #f0f4f8; color: #1a1a1a; }}
  h1 {{ color: #1565c0; border-bottom: 2px solid #1565c0; padding-bottom: 0.5rem; }}
  h2 {{ color: #0d47a1; margin-top: 1.5rem; }}
  .section {{ background: #fff; padding: 1rem 1.5rem; border-radius: 8px; margin-bottom: 1rem; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  .io-block {{ background: #f5f5f5; padding: 1rem; border-radius: 6px; white-space: pre-wrap; word-break: break-word; margin: 0.5rem 0; font-family: ui-monospace, monospace; font-size: 0.95rem; max-height: 400px; overflow-y: auto; }}
  .label {{ font-weight: 600; color: #37474f; margin-bottom: 0.5rem; }}
  .meta {{ color: #546e7a; font-size: 0.95rem; }}
</style>
</head>
<body>
<h1>Poffices Market Analysis Agent 测试报告</h1>
<p class="meta">由 <code>run_poffices_bootstrap.py --query-test</code> 生成。Bootstrap 流程：登录 → Agent Master → Business Office → Market Analysis → Enable 开关 → Apply → 发送 Query → 收集响应。</p>

<div class="section">
<h2>1. 输入（Query）</h2>
<div class="label">发送给 Market Analysis Agent 的查询：</div>
<div class="io-block">{query_esc}</div>
</div>

<div class="section">
<h2>2. 输出（Market Analysis Agent 响应）</h2>
<div class="label">Market Analysis Agent 的回复（等待后提取）：</div>
<div class="io-block">{response_esc}</div>
</div>

<div class="section">
<h2>3. 调用的 RPA 步骤</h2>
<ul>
{rows}
</ul>
</div>

</body>
</html>"""


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="从轨迹目录生成 B8 评估报告")
    p.add_argument("log_dir", type=Path, nargs="?", default=_root / "logs" / "e2e_demo", help="轨迹目录")
    p.add_argument("-o", "--output", type=Path, default=None, help="输出文件（默认打印到 stdout）")
    p.add_argument("-f", "--format", choices=["json", "html"], default="json", help="输出格式")
    args = p.parse_args()
    report = generate_report(args.log_dir, output_path=args.output, format=args.format)
    if "error" in report:
        print(report["error"], file=sys.stderr)
    if not args.output:
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
