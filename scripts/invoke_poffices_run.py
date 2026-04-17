#!/usr/bin/env python
"""本地调用 POST /api/v1/poffices/run，便于与 Poffices 画布行为对照。

用法（先启动服务: python run_server.py）:
  python scripts/invoke_poffices_run.py --goal "测试 Market Analysis"
  python scripts/invoke_poffices_run.py --goal "..." --agents "Market Analysis" --native
  python scripts/invoke_poffices_run.py --url http://127.0.0.1:8000 --goal "..." --query "固定问题"

- 默认使用 OpenAI 兼容信封（与画布一致），打印 choices[0].message.content 长度与摘要。
- 加 --native 则请求 response_envelope=native，直接看服务端 PofficesRunResponse JSON。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def main() -> None:
    p = argparse.ArgumentParser(description="本地调用 /api/v1/poffices/run")
    p.add_argument("--url", default="http://127.0.0.1:8000", help="服务根 URL")
    p.add_argument("--goal", required=True, help="测试目标（自然语言）")
    p.add_argument("--agents", default=None, help="逗号分隔，如 Market Analysis")
    p.add_argument("--query", default=None, help="可选：固定 query，不传则由 LLM 生成")
    p.add_argument("--request-id", default=None, dest="request_id", help="可选 request_id（幂等缓存键）")
    p.add_argument("--native", action="store_true", help="response_envelope=native，不看 OpenAI 包装")
    args = p.parse_args()

    base = args.url.rstrip("/")
    path = f"{base}/api/v1/poffices/run"
    body: dict = {"goal": args.goal}
    if args.request_id:
        body["request_id"] = args.request_id
    if args.agents:
        body["agents_to_test"] = [a.strip() for a in args.agents.split(",") if a.strip()]
    if args.query:
        body["query"] = args.query
    if args.native:
        body["response_envelope"] = "native"

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        path,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        print(e.read().decode("utf-8", errors="replace"), file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"无法连接 {path}: {e}", file=sys.stderr)
        print("请先在本机运行: python run_server.py", file=sys.stderr)
        sys.exit(1)

    obj = json.loads(raw)
    print(json.dumps(obj, ensure_ascii=False, indent=2))

    if not args.native and isinstance(obj, dict):
        ch0 = (obj.get("choices") or [{}])[0]
        msg = ch0.get("message") if isinstance(ch0, dict) else None
        content = (msg or {}).get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            print("\n--- choices[0].message.content 长度 ---", file=sys.stderr)
            print(len(content), file=sys.stderr)
            if content.strip():
                try:
                    inner = json.loads(content)
                    print(json.dumps(inner, ensure_ascii=False, indent=2)[:4000], file=sys.stderr)
                except json.JSONDecodeError:
                    print(content[:2000], file=sys.stderr)


if __name__ == "__main__":
    main()
