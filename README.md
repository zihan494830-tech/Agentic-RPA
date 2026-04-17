# ART — Agentic RPA Testing Framework for Agent Systems

在真实业务环境（通过 RPA/浏览器自动化接入）中，**以本框架为主体**，通过识别任务难度、编排工作流与 RPA/多 Agent，**对「待测 Agent」进行多维度测试**，并产出轨迹与评估报告；而非仅根据待测 Agent 的输出做事后分析。

**架构理念**：ART 当测试器（出题 + 给环境 + 判卷），待测 Agent 当考生，RPA 当环境/手脚，评估层当裁判。

- **待测 Agent**：**被测对象**。Poffices 场景下即 **Poffices 页面上的 Agent**（如 Research Proposal、Market Analysis），由 `app_ready` 的 `options.agent_name` 或配置/任务描述指定。B6 中运行的决策逻辑（PofficesAgent、PofficesLLMAgent、MockAgent 等）称为 **B6 决策组件**，负责驱动流程并对待测 Agent 进行测试。术语详见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#2-术语待测-agent-vs-b6-决策组件)。
- **LLM 在框架中的角色**：LLM **仅接入 Orchestrator 层**（如 B2 的 `routing_llm` 做 single_flow / multi_flow 路由），用于辅助组织测试。

---

## 文档结构

完整文档索引见 [**docs/README.md**](docs/README.md)。核心入口如下：

| 文档 | 内容 |
|------|------|
| [**架构与项目结构**](docs/ARCHITECTURE.md) | 项目定位、四层架构、两个闭环、B1–B9 Block（含代码位置）、仓库结构、**项目状态与 L3** |
| [**实施计划**](docs/IMPLEMENTATION_PLAN.md) | **当前项目状态**、闭环策略、Block 依赖与单测摘要；L3 已基本实现，后续优化见文内 |
| [**使用与测试指南**](docs/GUIDE.md) | 安装与运行、服务地址与 ngrok、Poffices 固定/动态场景、测试命令、评估与报告、根目录规范 |
| [**API 契约**](docs/API_CONTRACT.md) | 统一 Block API 格式（请求/响应、错误、版本）、B1/B8/B9 HTTP 暴露 |
| [**当前状态与变更**](docs/STATUS.md) | Poffices 配置、入口、链路、Bootstrap 幂等、近期与历史变更 |

---

## 核心设计要点

- **Orchestrator 可配置**：难度与路由、DAG 工作流、单/多 Agent 编排、goal_driven + L3 规划均可作为实验变量。
- **两个闭环**：闭环 1 已落地（RPA ↔ Agent）；闭环 2 部分可用（评估 → 下一轮出题/策略），更深联动列为后续优化。详见 [架构](docs/ARCHITECTURE.md)。
- **Block 化**：B1–B9 接口先行、可 mock、可单独测试。详见 [架构](docs/ARCHITECTURE.md)。

---

## 当前项目状态

| 说明 | 内容 |
|------|------|
| **框架** | B1–B9 与闭环 1 已贯通；B8、多 run、Poffices、HTTP 服务可用。 |
| **RPA 第三等级（L3）** | **已基本实现**：目标导向、动态场景（`experiment_poffices_dynamic` + `--goal`）、ScenarioSpec 与 L3 规划器已接入主线；更细增强见 [LEVEL3_INTELLIGENT_RPA.md](docs/LEVEL3_INTELLIGENT_RPA.md)，**后续优化择机推进**。 |
| **主线入口** | `run_poffices_agent.py`（固定场景 / 动态场景 `--config experiment_poffices_dynamic --goal "..."`）。 |

实施计划与依赖摘要见 [实施计划](docs/IMPLEMENTATION_PLAN.md)。

---

## 默认真实 RPA（Playwright）

- **Orchestrator、演示脚本、HTTP B9** 默认使用 **PlaywrightRPA**（真实浏览器）；未安装 `playwright` 时自动回退到 MockRPA。
- **MockRPA 仅用于**：单测（`tests/`）以保证稳定、无头环境可跑。其余开发与运行均基于真实浏览器。
- 安装真实 RPA：`pip install ".[phase1]"` 且 `playwright install`。

## 安装与运行

- **安装依赖**：在项目根目录执行 `pip install -e .` 或 `pip install -r requirements.txt`；要默认用真实浏览器再加 `pip install ".[phase1]"` 与 `playwright install`。
- **运行测试**：`pytest tests -v`（单测用 MockRPA，保证可重复、不依赖浏览器）。
- **代码结构**：契约与 Block 实现位于 `raft/`（`raft/rpa` 默认 `get_default_rpa()` → PlaywrightRPA，`raft/orchestrator` B9）。

---

## HTTP 服务（Postman / FastAPI 文档测试）

- **启动服务**：在项目根目录执行 `python run_server.py`（需已安装依赖：`pip install -e .` 或 `pip install -r requirements.txt`）。
- **服务地址**：**`http://127.0.0.1:8000`**（文档：`http://127.0.0.1:8000/docs`，健康检查：`http://127.0.0.1:8000/health`）。
- **公网访问**：使用 ngrok 时执行 `ngrok http 8000`，将生成的 `https://xxx.ngrok-free.app` 作为 Base URL 填入其他平台的 API 管理（仅填根地址，不填 `/docs`）。
- **Swagger 文档**：浏览器打开 `http://127.0.0.1:8000/docs`，可直接在页面上试调 B1、B8、B9。
- **Postman 测试**：
  - **B1 加载配置**：`POST http://127.0.0.1:8000/api/v1/b1/load_config`，Body 选 raw JSON，示例：
    ```json
    {
      "request_id": "req-001",
      "block_id": "B1",
      "api_version": "v1",
      "payload": {
        "config_path": "scenarios/experiment_poffices.json",
        "task_spec_path": "scenarios/task_specs.json",
        "task_spec_id": "task-poffices-query"
      }
    }
    ```
  - **B8 评估轨迹**：`POST http://127.0.0.1:8000/api/v1/b8/evaluate`，Body：`{ "trajectory": [...], "task_spec": {...}, "run_id": "可选" }`，返回 RunMetrics。
  - **B9 跑一轮任务**：`POST http://127.0.0.1:8000/api/v1/b9/run`，Body 示例（用文件路径）：
    ```json
    {
      "request_id": "req-002",
      "block_id": "B9",
      "payload": {
        "config_path": "scenarios/experiment_poffices.json",
        "task_spec_path": "scenarios/task_specs.json",
        "task_spec_id": "task-poffices-query",
        "max_steps": 5
      }
    }
    ```
  路径为相对项目根目录；返回中 `data` 含 `trajectory`、`steps_run`、`step2_agent_input_contains_step1_execution_result`。

---

## 已实现能力（闭环 1 + B8 + Poffices + L3）

- **闭环 1 验证**：Mock RPA 可配置 `fail_steps` / `timeout_steps`；真实 RPA 为 PlaywrightRPA（需 `pip install ".[phase1]"` 且 `playwright install`）。B8 最小评估：轨迹落盘、任务成功/失败、步骤数；Orchestrator 传入 `run_id` 与 `log_dir` 时自动落盘并返回 `metrics`。
- **B6**：MockAgent、PofficesAgent、PofficesLLMAgent；B1 场景为 `scenarios/experiment_poffices.json`、`experiment_poffices_dynamic.json` 等。
- **Poffices 端到端**：通过 `run_poffices_agent.py --runs N [--config experiment_poffices_dynamic --goal "..."]`，在真实 Poffices 场景下完成单/多轮 L3 智能 RPA 流程，并产出报告。
- **进度与数据流**：`python scripts/visualize_progress.py` 生成 `progress.html` / `progress.json`。

## Poffices 待测 Agent（B6 + B7）

将 [Poffices.ai](https://b1s2.hkrnd.com/) **页面上的 Agent**（如 Research Proposal、Market Analysis）作为**待测 Agent** 接入 ART：B6 使用 **PofficesAgent** 或 **PofficesLLMAgent**（决策组件）驱动流程，B7 使用 **PofficesRPA** 在真实 Poffices 页面上执行 tool_calls，闭环 1 + B8 轨迹落盘与评估。

- **前置**：`.env` 中配置 `POFFICES_USERNAME`、`POFFICES_PASSWORD`；安装 `pip install ".[phase1]"` 且 `playwright install`。若实验配置中 `extra.use_llm_query` 为 true，需配置 `OPENAI_API_KEY` 或 `XAI_API_KEY`（Grok）供 LLM 出题。
- **统一入口（支持单/多轮）**：`python run_poffices_agent.py --runs N`
  - 单轮：`python run_poffices_agent.py --runs 1`
  - 多轮：`python run_poffices_agent.py --runs 3`
  - 同一浏览器会话内首轮 bootstrap + query，后续轮次 New question + 新 query，轨迹写入 `logs/poffices/`，输出 `run_report.html`（当 `--runs 1` 自动呈现本轮视图）。
- **Bootstrap（登录 → 选 Agent）**：`python scripts/run_poffices_bootstrap.py`（见 [GUIDE · Poffices Bootstrap](docs/GUIDE.md#15-pofficesai-rpa-bootstrap登录--选-agent)）。
