# raft 包 — ART 框架实现

本目录为 **ART**（Agentic RPA Testing Framework）的 Python 包，Python 包名保持 `raft` 以兼容现有导入。安装后可通过 `import raft.xxx` 使用。

## 子目录与 Block 对应

| 子目录 | Block | 说明 |
|--------|-------|------|
| **contracts/** | — | API 契约与业务模型（ExperimentConfig、TaskSpec、ExecutionResult、RunMetrics 等） |
| **core/config/** | B1 | 实验配置与 TaskSpec 加载 |
| **core/state/** | B5 | 状态与轨迹管理 |
| **core/dag/** | B3 | 工作流 DAG（预留） |
| **core/difficulty/** | B2 | 难度与路由（预留） |
| **core/scheduler/** | B4 | Agent 调度与工具路由（预留） |
| **agents/** | B6 | Agent 协议与实现（MockAgent、LLMAgent） |
| **rpa/** | B7 | RPA 适配器（MockRPA、PlaywrightRPA、default_rpa） |
| **evaluation/** | B8 | 轨迹评估、落盘、RunMetrics |
| **orchestrator/** | B9 | 编排器：闭环 1（Agent → RPA → 状态 → Agent） |
| **api/** | — | FastAPI HTTP 服务，暴露 B1、B9 |

详细项目结构与 Block 代码位置见仓库根目录 [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)。
