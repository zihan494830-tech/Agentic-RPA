#!/usr/bin/env python
"""从轨迹目录聚合多 run，按 (scenario, orchestration_mode, rpa_mode) 产出对比表（JSON/HTML）。"""
import argparse
import json
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from scripts.generate_report import generate_phase3_comparison


def main() -> None:
    p = argparse.ArgumentParser(description="多 run 聚合对比表（按场景/编排/RPA 模式）")
    p.add_argument(
        "log_dirs",
        type=Path,
        nargs="+",
        help="一个或多个轨迹目录（将合并扫描 *.json）",
    )
    p.add_argument("-o", "--output", type=Path, default=None, help="输出文件")
    p.add_argument("-f", "--format", choices=["json", "html"], default="html", help="输出格式")
    args = p.parse_args()
    report = generate_phase3_comparison(
        args.log_dirs,
        output_path=args.output,
        format=args.format,
    )
    if not args.output:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"已写入: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
