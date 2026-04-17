#!/usr/bin/env python
"""单轮轨迹的「可重放」脚本：读取轨迹 JSON（含 schema_version），按步解析并逐步输出，便于复现与调试。

当前为只读重放：解析并打印每步的 tool_calls、execution_results，不执行真实 RPA。
用法：
  python scripts/replay_trajectory.py <轨迹.json>
  python scripts/replay_trajectory.py logs/poffices  # 取目录下最新一条 .json
"""
import json
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


def load_trajectory(path: Path) -> dict:
    """加载轨迹 JSON，支持 schema_version 解析。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    ver = data.get("schema_version", "1.0")
    if ver != "1.0":
        print(f"# schema_version={ver}，按 1.0 兼容解析")
    return data


def replay_readonly(data: dict) -> None:
    """只读重放：按步打印 step_index、tool_calls、execution_results。"""
    trajectory = data.get("trajectory") or []
    run_id = data.get("run_id", "")
    task_spec_id = data.get("task_spec_id", "")
    print(f"# run_id={run_id} task_spec_id={task_spec_id} steps={len(trajectory)}")
    print()
    for i, entry in enumerate(trajectory):
        step_index = entry.get("step_index", i)
        sr = entry.get("step_result") or {}
        tcs = sr.get("tool_calls") or []
        ers = sr.get("execution_results") or []
        print(f"--- Step {step_index} ---")
        for tc in tcs:
            print(f"  tool: {tc.get('tool_name')} params={tc.get('params')}")
        for er in ers:
            ok = er.get("success", False)
            name = er.get("tool_name", "")
            err = er.get("error_type", "")
            print(f"  result: {name} success={ok}" + (f" error_type={err}" if err else ""))
        print()
    extra = data.get("extra") or {}
    if extra.get("metrics"):
        m = extra["metrics"]
        print("# metrics: success=%s step_count=%s" % (m.get("success"), m.get("step_count")))
    if extra.get("run_record"):
        rr = extra["run_record"]
        print("# run_record: run_id=%s timestamp_iso=%s" % (rr.get("run_id"), rr.get("timestamp_iso")))


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="只读重放轨迹 JSON：按步打印 tool_calls 与 execution_results")
    p.add_argument("path", type=Path, help="轨迹 .json 文件或目录（目录时取最新一条 .json）")
    args = p.parse_args()
    path = Path(args.path)
    if path.is_dir():
        json_files = sorted(path.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)
        if not json_files:
            print("目录下无 .json 文件", file=sys.stderr)
            sys.exit(1)
        path = json_files[0]
        print(f"# 使用最新轨迹: {path}\n")
    if not path.exists() or not path.is_file():
        print(f"文件不存在: {path}", file=sys.stderr)
        sys.exit(1)
    data = load_trajectory(path)
    replay_readonly(data)


if __name__ == "__main__":
    main()
