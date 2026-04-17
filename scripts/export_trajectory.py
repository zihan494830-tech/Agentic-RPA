#!/usr/bin/env python
"""单轮轨迹的查看/导出：按步、按工具、按执行结果导出为 JSON/CSV/HTML，便于可观测性。

用法：
  python scripts/export_trajectory.py <轨迹.json> -o out.json
  python scripts/export_trajectory.py logs/poffices -f csv -o steps.csv
  python scripts/export_trajectory.py logs/poffices -f html -o report.html
"""
import json
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


def load_trajectory(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data


def by_step(data: dict) -> list[dict]:
    """按步：每步一条记录，含 step_index、tool_names、success_list、summary。"""
    trajectory = data.get("trajectory") or []
    rows = []
    for entry in trajectory:
        sr = entry.get("step_result") or {}
        tcs = sr.get("tool_calls") or []
        ers = sr.get("execution_results") or []
        tool_names = [t.get("tool_name", "") for t in tcs]
        success_list = [bool(er.get("success")) for er in ers]
        rows.append({
            "step_index": entry.get("step_index", len(rows)),
            "tool_names": tool_names,
            "tool_count": len(tool_names),
            "success_list": success_list,
            "all_success": all(success_list) if success_list else None,
        })
    return rows


def by_tool(data: dict) -> list[dict]:
    """按工具：每个 tool_call 一条记录。"""
    trajectory = data.get("trajectory") or []
    rows = []
    for entry in trajectory:
        step_index = entry.get("step_index", 0)
        sr = entry.get("step_result") or {}
        for tc in sr.get("tool_calls") or []:
            rows.append({
                "step_index": step_index,
                "tool_name": tc.get("tool_name", ""),
                "params": tc.get("params"),
            })
    return rows


def by_execution_result(data: dict) -> list[dict]:
    """按执行结果：每个 execution_result 一条记录。"""
    trajectory = data.get("trajectory") or []
    rows = []
    for entry in trajectory:
        step_index = entry.get("step_index", 0)
        for er in (entry.get("step_result") or {}).get("execution_results") or []:
            rows.append({
                "step_index": step_index,
                "tool_name": er.get("tool_name", ""),
                "success": bool(er.get("success")),
                "error_type": er.get("error_type"),
            })
    return rows


def export_json(data: dict, path: Path, view: str) -> None:
    if view == "step":
        out = {"by_step": by_step(data), "run_id": data.get("run_id"), "step_count": data.get("step_count")}
    elif view == "tool":
        out = {"by_tool": by_tool(data), "run_id": data.get("run_id")}
    elif view == "result":
        out = {"by_execution_result": by_execution_result(data), "run_id": data.get("run_id")}
    else:
        out = {"by_step": by_step(data), "by_tool": by_tool(data), "by_execution_result": by_execution_result(data), "run_id": data.get("run_id")}
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


def export_csv(data: dict, path: Path, view: str) -> None:
    import csv
    if view == "step":
        rows = by_step(data)
        if not rows:
            path.write_text("step_index,tool_count,all_success\n", encoding="utf-8")
            return
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["step_index", "tool_count", "all_success", "tool_names", "success_list"])
            w.writeheader()
            for r in rows:
                w.writerow({
                    "step_index": r["step_index"],
                    "tool_count": r["tool_count"],
                    "all_success": r["all_success"],
                    "tool_names": "|".join(r["tool_names"]),
                    "success_list": "|".join(str(x) for x in r["success_list"]),
                })
    elif view == "tool":
        rows = by_tool(data)
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["step_index", "tool_name", "params"])
            w.writeheader()
            for r in rows:
                w.writerow({"step_index": r["step_index"], "tool_name": r["tool_name"], "params": json.dumps(r["params"], ensure_ascii=False) if r.get("params") else ""})
    else:
        rows = by_execution_result(data)
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["step_index", "tool_name", "success", "error_type"])
            w.writeheader()
            w.writerows(rows)


def export_html(data: dict, path: Path) -> None:
    steps = by_step(data)
    results = by_execution_result(data)
    run_id = data.get("run_id", "")
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"/><title>轨迹导出 - {run_id}</title>
<style>
  table {{ border-collapse: collapse; margin: 1rem 0; }}
  th, td {{ border: 1px solid #ccc; padding: 6px 10px; text-align: left; }}
  th {{ background: #f0f4f8; }}
  .ok {{ color: green; }} .fail {{ color: #b71c1c; }}
</style>
</head>
<body>
  <h1>轨迹导出</h1>
  <p>run_id: <code>{run_id}</code> · 步数: {len(steps)}</p>
  <h2>按步</h2>
  <table>
    <tr><th>step_index</th><th>tool_count</th><th>all_success</th><th>tool_names</th><th>success_list</th></tr>
"""
    for r in steps:
        sc = r["all_success"]
        sc_class = "ok" if sc else "fail" if sc is False else ""
        html += f"    <tr><td>{r['step_index']}</td><td>{r['tool_count']}</td><td class=\"{sc_class}\">{sc}</td><td>{', '.join(r['tool_names'])}</td><td>{r['success_list']}</td></tr>\n"
    html += "  </table>\n  <h2>按执行结果</h2>\n  <table>\n    <tr><th>step_index</th><th>tool_name</th><th>success</th><th>error_type</th></tr>\n"
    for r in results:
        cls = "ok" if r["success"] else "fail"
        html += f"    <tr><td>{r['step_index']}</td><td>{r['tool_name']}</td><td class=\"{cls}\">{r['success']}</td><td>{r.get('error_type') or ''}</td></tr>\n"
    html += "  </table>\n</body>\n</html>"
    path.write_text(html, encoding="utf-8")


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="按步/按工具/按执行结果导出轨迹")
    p.add_argument("path", type=Path, help="轨迹 .json 或目录（目录时取最新 .json）")
    p.add_argument("-o", "--output", type=Path, default=None, help="输出文件路径")
    p.add_argument("-f", "--format", choices=["json", "csv", "html"], default="json", help="导出格式")
    p.add_argument("-v", "--view", choices=["step", "tool", "result", "all"], default="all", help="视图：step/tool/result/all（仅 json 支持 all）")
    args = p.parse_args()
    path = Path(args.path)
    if path.is_dir():
        json_files = sorted(path.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)
        if not json_files:
            print("目录下无 .json 文件", file=sys.stderr)
            sys.exit(1)
        path = json_files[0]
    if not path.exists() or not path.is_file():
        print(f"文件不存在: {path}", file=sys.stderr)
        sys.exit(1)
    data = load_trajectory(path)
    out = args.output
    if not out:
        ext = {"json": ".json", "csv": ".csv", "html": ".html"}[args.format]
        out = path.parent / (path.stem + "_export" + ext)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.format == "json":
        export_json(data, out, args.view)
    elif args.format == "csv":
        export_csv(data, out, args.view)
    else:
        export_html(data, out)
    print(f"已导出: {out}")


if __name__ == "__main__":
    main()
