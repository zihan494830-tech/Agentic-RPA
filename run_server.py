#!/usr/bin/env python
"""启动 ART HTTP 服务入口。逻辑在 raft.cli，根目录仅作启动器。
用法：python run_server.py
服务地址：http://127.0.0.1:8000  文档：http://127.0.0.1:8000/docs  B1/B8/B9 见 /docs
公网暴露：ngrok http 8000 后，将 https://xxx.ngrok-free.app 填到其他平台 API 管理。

长时 /run（RPA）时建议关闭热重载，避免保存文件导致进程重启、请求中断：
  PowerShell: $env:RAFT_NO_RELOAD='1'; python run_server.py
  cmd:        set RAFT_NO_RELOAD=1 && python run_server.py
"""
from raft.cli.server import main

if __name__ == "__main__":
    main()
