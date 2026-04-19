# ART — Agentic RPA Testing Framework

**ART 是一个用于测试 AI Agent 的自动化测试框架**，通过真实浏览器（RPA）驱动目标平台，自动出题、执行、评估并生成报告，而不是事后分析 Agent 的输出。

> **适用场景**：你有一个部署在 Web 平台上的 AI Agent，想用自动化测试验证它在真实任务中的表现。ART 代替人工，自动打开浏览器、操作页面、给 Agent 出题、观察结果、评分。

---

## 快速开始

### 方式一：无浏览器（运行单测，验证框架逻辑）

```bash
git clone https://github.com/zihan494830-tech/Agentic-RPA.git
cd Agentic-RPA
pip install -e .
pytest tests -v          # 使用 MockRPA，不需要浏览器，约 30 秒跑完
```

### 方式二：启动 HTTP 服务（Swagger UI 交互测试）

```bash
pip install -e .
python run_server.py     # 启动后访问 http://127.0.0.1:8000/docs
```

### 方式三：完整端到端测试（需要 Poffices 账号 + LLM API Key）

```bash
pip install ".[phase1]"
playwright install chromium
cp .env.example .env     # 填入 POFFICES_USERNAME、POFFICES_PASSWORD、SILICONFLOW_API_KEY
python run_poffices_agent.py --runs 1
# 完成后查看 logs/poffices/run_report.html
```

---

## 环境要求

| 项目 | 要求 |
|------|------|
| Python | 3.10+ |
| 操作系统 | Windows / macOS / Linux |
| 浏览器（方式三） | Chromium（由 `playwright install` 自动安装） |
| Poffices 账号（方式三） | 需要有 [Poffices.ai](https://b1s2.hkrnd.com/) 的登录凭据 |
| LLM API Key（方式三） | SiliconFlow / Qwen / Grok 任一，用于 LLM 出题与评估 |

`.env` 最小配置（方式三）：

```env
POFFICES_USERNAME=你的用户名
POFFICES_PASSWORD=你的密码
RAFT_LLM_PROVIDER=siliconflow
SILICONFLOW_API_KEY=你的_api_key
```

---

## 核心概念

| 术语 | 含义 |
|------|------|
| **待测 Agent** | 被测对象，即 Poffices 页面上的 AI Agent（如 Research Proposal） |
| **RPA** | 浏览器自动化（Robotic Process Automation），ART 用它操作页面 |
| **B1–B9** | 框架内部的功能模块（Block），如 B1 加载配置、B7 执行 RPA、B8 评估 |
| **Orchestrator** | 调度器，按计划依次调用各 Block 完成一轮测试 |
| **L3 智能 RPA** | 目标驱动模式：输入自然语言目标，框架自动规划测试步骤 |
| **MockRPA** | 模拟 RPA，不打开浏览器，用于单元测试 |

---

## 文档结构

完整文档索引见 [**docs/README.md**](docs/README.md)。核心入口如下：

| 文档 | 内容 |
|------|------|
| [**架构与项目结构**](docs/ARCHITECTURE.md) | 项目定位、四层架构、两个闭环、B1–B9 Block（含代码位置）、仓库结构 |
| [**使用与测试指南**](docs/GUIDE.md) | 安装运行、Poffices 场景、三种运行模式、报告生成、近期变更 |
| [**API 契约**](docs/API_CONTRACT.md) | 统一 Block API 格式（请求/响应、错误、版本）、B1/B8/B9 HTTP 暴露 |
| [**L3 智能 RPA**](docs/L3_INTELLIGENT_RPA.md) | 目标驱动设计、多 Agent 支持、动态发现、已落地能力与验收用例 |
| [**实施计划**](docs/IMPLEMENTATION_PLAN.md) | 当前项目状态、闭环策略、Block 依赖与单测摘要 |

---

## 当前项目状态

| 说明 | 内容 |
|------|------|
| **框架** | B1–B9 与闭环 1 已贯通；B8、多 run、Poffices、HTTP 服务可用 |
| **RPA 第三等级（L3）** | **已基本实现**：目标导向、动态场景（`--config experiment_poffices_dynamic --goal "..."`）、ScenarioSpec 与 L3 规划器已接入主线；更细增强见 [L3_INTELLIGENT_RPA.md](docs/L3_INTELLIGENT_RPA.md) |
| **主线入口** | `run_poffices_agent.py`（固定场景 / 动态场景） |

---

## HTTP 服务

```bash
python run_server.py     # http://127.0.0.1:8000
                         # Swagger UI: http://127.0.0.1:8000/docs
ngrok http 8000          # 公网访问（将生成的 URL 发给协作者）
```

**Postman 示例 — B9 跑一轮任务**：

```http
POST http://127.0.0.1:8000/api/v1/b9/run
Content-Type: application/json

{
  "request_id": "req-001",
  "block_id": "B9",
  "payload": {
    "config_path": "scenarios/experiment_poffices.json",
    "task_spec_path": "scenarios/task_specs.json",
    "task_spec_id": "task-poffices-query",
    "max_steps": 5
  }
}
```

更多接口示例见 [docs/GUIDE.md](docs/GUIDE.md) 与 [docs/API_CONTRACT.md](docs/API_CONTRACT.md)。

---

## 架构概览

```
ART 框架（出题 + 给环境 + 判卷）
├── B1  加载实验配置与任务规格
├── B2  评估任务难度与路由策略
├── B3  构建执行 DAG
├── B4  分配 Agent 与步骤
├── B5  管理执行状态与轨迹
├── B6  决策组件（驱动测试流程的逻辑）
├── B7  RPA 执行层（Playwright 操作真实浏览器）
├── B8  评估层（轨迹分析、LLM 判分）
└── B9  Orchestrator（总调度，串联 B1–B8）

待测 Agent（被测对象，运行在 Poffices 页面上）
```

详细架构见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。
