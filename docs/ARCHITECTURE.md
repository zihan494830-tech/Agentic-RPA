# ART 架构与项目结构

项目定位、四层架构、B1–B9 Block、术语、LLM 速查、仓库结构。使用与测试见 [GUIDE.md](GUIDE.md)，API 契约见 [API_CONTRACT.md](API_CONTRACT.md)。

---

## 1. 项目定位

- **ART**：Agentic RPA Testing Framework for Agent Systems。在真实 RPA/浏览器环境中对「待测 Agent」做多维度测试并产出轨迹与评估。
- **待测 Agent**：被测对象；Poffices 场景下 = 页面上的 Agent（如 Research Proposal）。**B6 决策组件**：B6 中驱动流程的逻辑（PofficesAgent、PofficesLLMAgent 等）。

---

## 2. 术语（待测 Agent vs B6 决策组件）

| 概念 | 含义 | Poffices 场景下 |
|------|------|------------------|
| **待测 Agent** | 被测对象；要评估的产品/功能 | 页面上的 Research Proposal、Market Analysis 等（由 `options.agent_name` 指定） |
| **B6 决策组件** | 决定「调用哪个 Block、传什么参数」的逻辑 | PofficesAgent、PofficesLLMAgent 等（由 agent_type / factory 选择） |

待测 Agent 的输出 = B7 从页面提取的响应内容；B6 决策组件驱动流程，通过 B7 执行 RPA Block 对待测 Agent 进行测试。

---

## 3. 四层架构

| 层 | 内容 |
|----|------|
| 实验与任务层 | 配置、TaskSpec（描述、GT） |
| 实验器层 | Orchestrator：路由、DAG、调度、状态与轨迹 |
| 智能体与 RPA 层 | Agent、RPA/浏览器 |
| 评估层 | 轨迹、指标、LLM-as-judge |

**闭环 1**（单 run）：Agent → tool_calls → B7 RPA → ExecutionResult → B5 → 注入 Agent。**闭环 2**（跨 run）：B8 评估 → 下一轮出题/策略（部分已落地；评估驱动 B2/B3/B4 编排参数为后续优化）。

---

## 4. Block 与代码位置（B1–B9）

| Block | 职责 | 主要代码 |
|-------|------|----------|
| B1 | 配置与 TaskSpec | `raft/core/config/`、`models.py` |
| B2 | 难度与路由（可 LLM） | `raft/core/difficulty/` |
| B3 | DAG 工作流 | `raft/core/dag/` |
| B4 | Agent 调度与工具路由 | `raft/core/scheduler/` |
| B5 | 状态与轨迹 | `raft/core/state/` |
| B6 | Agent 运行时（决策组件） | `raft/agents/` |
| B7 | RPA 与 ExecutionResult | `raft/rpa/` |
| B8 | 评估与指标 | `raft/evaluation/` |
| B9 | 编排器 | `raft/orchestrator/` |

契约：`raft/contracts/models.py`、`api.py`。

### 4.1 LLM 接入点速查

- **LLM**：统一 OpenAI 兼容 API，环境变量 `OPENAI_API_KEY` / `OPENAI_API_BASE` / `RAFT_LLM_PROVIDER`（ART 框架 LLM 提供商，变量名保持不变以兼容现有配置）。
- B2：`raft/core/difficulty/llm_router.py`（难度与路由）
- B9：`raft/core/task_description_suggester.py`、`query_suggester.py`（任务描述、query 建议）
- B6：`raft/agents/llm_agent.py`、`poffices_llm_agent.py`（可选决策组件）
- B8：`raft/core/llm_judge.py`（轨迹判分、多轮总结）
- 报告：`raft/reporting/llm_report.py`、`llm_extract.py`

---

## 5. 仓库结构

- **根目录**：仅允许 README、pyproject.toml、requirements.txt、run_*.py、progress.json/html、docs/、raft/、scenarios/、scripts/、tests/、logs/ 等（见 .cursor/rules）。
- **raft/**：唯一实现包。`core/`、`agents/`、`rpa/`、`evaluation/`、`orchestrator/`、`contracts/`、`api/`、`cli/`、`reporting/`。

---

## 6. 项目状态与 L3

| 说明 | 内容 |
|------|------|
| **框架** | B1–B9、闭环 1、真实 RPA（Playwright）、B2/B3/B4、B8 扩展、多 run、Poffices 主线已可用。 |
| **RPA 第三等级（L3）** | **已基本实现**：goal_driven、ScenarioSpec、L3 规划器已接入，可按目标动态组合流程；更细能力演进见下文设计文档。 |

RPA 流程块化见 [RPA_BLOCKS.md](RPA_BLOCKS.md)、`raft/rpa/blocks.py`。  
Scenario 设计见 [SCENARIO_DESIGN.md](SCENARIO_DESIGN.md)。L3 思路与后续可选增强见 [LEVEL3_INTELLIGENT_RPA.md](LEVEL3_INTELLIGENT_RPA.md)。

---

## 7. RPA 等级

项目已覆盖 **第二等级（基于模块的 RPA）** 与 **第三等级（智能 RPA）** 的主线能力：第三等级下 goal_driven + ScenarioSpec + L3 规划器已接入；可选增强（如重规划、复合块等）见 [LEVEL3_INTELLIGENT_RPA.md](LEVEL3_INTELLIGENT_RPA.md)，**后续优化择机推进**。

---

## 8. 数据流与闭环（原数据流说明）

本文档说明 B1–B9 与各 LLM 模块之间的数据流、谁消费谁产出、以及是否存在矛盾或断点，便于实现「联动并完美闭环」。

---
### 1. 总览：两条闭环

| 闭环 | 数据流 | 当前状态 |
|------|--------|----------|
| **闭环 1（单 run 内）** | B6 决策组件 → tool_calls → B7 RPA → ExecutionResult → B5 → B9 将 state + 最近 ExecutionResult 注入 B6 → 下一步决策；待测 Agent（如 Poffices 页面产品）的输出经 B7 取回 | 已落地 |
| **闭环 2（跨 run）** | B8 评估 → metrics（含 llm_judge）→ 下一轮 query_context.previous_rounds → Query 建议器 + 规则策略 → 下一轮 query → B9 出题与执行 | **部分落地**：评估 → 出题/策略已闭环；评估 → B2/B3/B4 编排参数自动调整为后续优化 |

---
### 2. 多轮场景下的端到端数据流（Poffices 多轮脚本）

```
B1 加载 config + task_spec（静态）
    │
    ├──► 脚本：num_runs = 用户指定（--runs N），rounds_rationale = "用户指定 N 轮"
    │
    ├──► 可选 B2 route(task_spec) ──► route_type, difficulty（仅用于 query_context.b2_*，不用于决定轮数）
    │
    ▼
for 每轮 i in 1..num_runs:
    query_context = {
        previous_rounds,   ← 由上一轮及更早的 results 构建（含 query, success, step_count, llm_judge 等）
        policy_hint,       ← 规则策略 decide_next_strategy(previous_rounds) 产出
        multi_round_strategy,
        [b2_difficulty, b2_route_type]  ← 可选，来自 B2，供后续策略或 LLM 一致使用
    }
    │
    ▼
B9 run_until_done(config, task_spec, query_context=query_context)
    │
    ├── _get_initial_state_for_run:
    │   ├── 若 use_llm_task_description：task_description_suggester(**scenario_prompt**, agent_descriptor, goal)
    │   │       → effective_task_spec.description = 生成描述（scenario_prompt 来自 ScenarioSpec 或 scenario）
    │   └── 若 use_llm_query：query_suggester(effective_task_spec, …, previous_rounds, policy_hint, **scenario_context=scenario_prompt**)
    │           → initial_state["query"] = 本轮 query；多轮且有 previous_rounds 时可有 query_rationale
    │
    ├── 执行闭环 1：B6(agent) ← state + effective_task_spec.description → B7 → B5 → …
    │
    └── _attach_log_and_metrics：
            evaluate_trajectory(trajectory, **effective_task_spec**, …)
                → RunMetrics（含 success、step_count、llm_judge 等）
            → result["metrics"]、落盘
    │
    ▼
result 进入 results[]；下一轮用 _previous_rounds_from_results(results) 得到 previous_rounds
```

- **B8 评估用的 task_spec**：始终为当轮的 **effective_task_spec**（可能与静态 task 不同），保证判分与当轮「任务描述」一致。
- **报告**：`task_for_report` 取最后一轮 `task_spec_effective`；`rounds_rationale` 为「用户指定 N 轮」。

---
### 3. 各 LLM 模块输入/输出与联动

| 模块 | 归属 | 输入 | 输出 | 下游消费者 |
|------|------|------|------|-------------|
| **B2 llm_router** | B2 | TaskSpec（description 等） | route_type, difficulty（**不再输出 suggested_rounds**；轮数由用户指定） | 可选 query_context.b2_* |
| **task_description_suggester** | B9 出题 | scenario_prompt（由 ScenarioSpec/scenario 生成）、agent_descriptor、goal | 一句任务描述 | effective_task_spec.description；后续 B2 不直接使用 |
| **query_suggester** | B9 出题 | effective_task_spec、agent_descriptor、previous_rounds、policy_hint、strategy、scenario_context | 本轮 query（及可选 query_rationale） | initial_state["query"]；previous_rounds 来自 B8 产出 |
| **llm_judge**（单轮判分） | B8 | trajectory、**effective_task_spec**、run_id | RunMetrics.llm_judge | B8 落盘；下一轮 previous_rounds.llm_judge → Query 建议器 + 规则策略 |
| **llm_judge**（多轮总结） | 报告 | rounds_summaries（含各轮 llm_judge、output_snippet） | 多轮分析总结段落 | build_report_with_llm → HTML 报告 |

**一致性要点**：
- **测试轮数**：由用户/脚本通过 `--runs N` 指定，不再由 B2/LLM 建议。
- **任务描述 → B2**：B2 可选用于 route_type/difficulty（供 query 策略）；若启用 LLM 任务描述，每轮描述可能与静态不同。
- **B8 → 下一轮**：metrics（含 llm_judge）→ previous_rounds → query_policy + query_suggester，形成闭环。

---
### 4. 已避免的矛盾与断点

| 项目 | 说明 |
|------|------|
| B8 判分用错 task | 已用 **effective_task_spec** 调用 evaluate_trajectory，与当轮描述一致。 |
| route_type 与轮数 | 多轮脚本的轮数由用户指定；B2 仅产出 route_type/difficulty（规则或 LLM），供 query 策略可选使用。 |
| 多轮出题无历史 | previous_rounds 从 results 构建，含 success、step_count、llm_judge 等，供 query_suggester 与 query_policy 使用。 |
| 报告轮数依据不明 | rounds_rationale 写入报告「测试轮数决定依据」；任务展示用最后一轮 task_spec_effective。 |

---
### 5. 可选增强（已实现或建议）

- **B2 结果进入 query_context**：多轮脚本可将 `b2_difficulty`、`b2_route_type`（来自规则路由）传入每轮 query_context，供策略或 LLM 与 B2 对齐。轮数由 `--runs N` 指定。
- **闭环 2 加深**：后续可让 B9 读取历史 B8 指标，调整 B2/B3/B4（如难度、DAG、策略）；当前为「评估 → 出题」闭环，「评估 → 编排参数」闭环列为后续工作。

---
### 6. 与 ARCHITECTURE 的对应

- Block 职责与代码位置：见本文件第 4 节。
- LLM 模块一览：见 ARCHITECTURE 4.2。
- 本数据流文档：聚焦「谁传谁、有无矛盾、闭环是否完整」，与 ARCHITECTURE 互补。
