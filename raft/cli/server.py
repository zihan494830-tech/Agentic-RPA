#!/usr/bin/env python
"""启动 RAFT HTTP 服务，供 Postman 或浏览器 /docs 测试。"""
import os

import uvicorn


def main(host: str = "127.0.0.1", port: int = 8000, reload: bool | None = None) -> None:
    # 默认开启 reload；长 RPA /run 跑数分钟时，若 IDE 保存代码会触发整进程重启，请求被掐断。
    # 关闭：set RAFT_NO_RELOAD=1 或 Windows: set RAFT_NO_RELOAD=1 && python run_server.py
    if reload is None:
        reload = os.environ.get("RAFT_NO_RELOAD", "").strip().lower() not in ("1", "true", "yes", "on")
    uvicorn.run(
        "raft.api.server:app",
        host=host,
        port=port,
        reload=reload,
    )
