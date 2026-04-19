# ART 使用与测试指南

安装、运行、Poffices 统一入口（固定/动态场景、`--runs` / `--config` / `--goal`）、服务地址、测试。架构见 [ARCHITECTURE.md](ARCHITECTURE.md)，RPA Block 见 [RPA_BLOCKS.md](RPA_BLOCKS.md)。**项目状态**：RPA 第三等级（L3）主线已基本实现；更深优化见 [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)。

---

## 1. 安装与运行

```bash
pip install -e .
# 真实浏览器
pip install ".[phase1]"
playwright install
```

| 命令 | 说明 |
|------|------|
| `python run_server.py` | HTTP 服务 `http://127.0.0.1:8000` |
| `python run_poffices_agent.py --runs N` | Poffices 统一入口（见下节）；`--config` / `--goal` 切换固定/动态场景与目标 |

---

## 2. 服务地址

- 本机：`http://127.0.0.1:8000`，API 文档 `http://127.0.0.1:8000/docs`，健康检查 `http://127.0.0.1:8000/health`。
- 公网：用 ngrok 暴露 `ngrok http 8000`，将生成的 `https://xxx.ngrok-free.app` 填到对方 Base URL（不填 `/docs`）。

---

## 3. Poffices

**前置**：`.env` 中配置 `POFFICES_USERNAME`、`POFFICES_PASSWORD`；LLM 出题需 `OPENAI_API_KEY` 或 `XAI_API_KEY`（否则可设 `use_llm_query: false`）。

| 命令 | 说明 |
|------|------|
| `python run_poffices_agent.py --runs 1` | 单轮（**默认固定场景** `experiment_poffices.json`） |
| `python run_poffices_agent.py --runs 3` | 多轮，`--strategy rule` / `auto` / `deepen` / `diversify` |
| `python run_poffices_agent.py --runs 1 --config experiment_poffices_dynamic` | **动态场景**：L3 规划 + goal，按目标建流程 |
| `python run_poffices_agent.py --runs 1 --config experiment_poffices_dynamic --goal "..."` | 动态场景并覆盖 goal |
| `python run_poffices_agent.py --runs 1 --agent "..."` | 指定待测 Agent 单轮测试 |

轨迹与报告统一在 `logs/poffices/`（`run_report.html`，当 `--runs 1` 自动切换本轮视图）。成功判定：最后一步所有 execution 的 `success=True`（B8）。超时/选择器问题可加大 `--query-wait-sec` 或查轨迹中 `error_type`。

**Bootstrap 脚本**：`python scripts/run_poffices_bootstrap.py`（可选 `--full`、`--headless`）。

**新增 Block**：在 `raft/rpa/poffices_blocks.py` 实现并注册；在 `raft/agents/poffices_agent.py` 增加对应决策分支；补单测。

**画布规划 API** `POST /api/v1/poffices/plan`（`run_server.py` 启用）：`planning_profile=canvas` + 非空 `block_catalog` 时由规划器只在你声明的块里编排。响应 `data` 中：
- **`selected_agents`**：按 `planned_steps` 顺序从各步 `params` 抽取的 Agent 名（`agent_name` / `agent` / `options` 下同名键 / `agents[]`），画布「Agent Over Agent」等应优先读此字段。
- **`agents_planned`**：仍仅从 **`app_ready`** 的 `options.agent_name` 抽取，与本机 RPA 默认三块契约一致；无 `app_ready` 时通常为空。

导入示例见 `docs/poffices_agent_import.json`；**路线二画布（invoke_agent + `selected_agents`）** 见 `docs/poffices_canvas_flow_route2.json`；**去掉 Merge、仅用 Input Analysis + 固定名单** 见 `docs/poffices_canvas_flow_route2_no_merge.json`。

- **画布 JSON 校验**：内嵌请求体里若用占位符传 `agents_to_test`，须写成**字符串**，例如 `\"agents_to_test\": \"{layer_name_merge_output.agents_to_test}\"`（勿写成未加引号的 `{...}`，否则会报 JSON 语法错）。服务端会把展开后的 **JSON 数组字符串** 解析为列表；直接传 JSON 数组也可以。

---

## 4. 三种运行模式说明

本框架支持三种运行模式，Goal Interpreter 会根据用户自然语言自动解析并路由到对应模式。

---
### 1. 单 Agent 多轮

| 维度 | 说明 |
|------|------|
| **含义** | 同一个 Agent 跑多轮，每轮使用不同 query，用于深化或多样化测试 |
| **条件** | `agents` 只有 1 个，`runs > 1` |
| **执行** | 每轮一次 `run_until_done`，共 `runs` 轮；每轮 query 可由 LLM 根据上一轮表现生成 |
| **产出** | 多份独立输出，报告按轮展示并做多轮总结 |

**Goal 示例**：
- 「Research Proposal 跑 3 轮」
- 「单 agent 多轮验证」
- 「Market Analysis 跑 5 轮看看」

---
### 2. 多 Agent 协作

| 维度 | 说明 |
|------|------|
| **含义** | 多个 Agent **协作**产出一份报告，一条 query，一份最终输出 |
| **条件** | `agents > 1`，`output_type = single_report`（协作语义） |
| **执行** | 使用 Agent Master 协作流程：清空右侧 → 按顺序选 Agent → Apply → 执行 Step1→2→3→Integration → 提取最终报告 |
| **产出** | **一份**协作报告 |

**Goal 示例**：
- 「用三个 agent 写一份报告」
- 「Research Proposal、Market Analysis、Project Proposal 协作输出」
- 「共同完成一份关于石油价格的分析」

---
### 3. 一次性测多个 Agent

| 维度 | 说明 |
|------|------|
| **含义** | 一轮内依次测完每个 Agent，每个 Agent 独立 query、独立输出，用于对比/比较 |
| **条件** | `agents > 1`，`output_type = multi_report`（测试/对比语义） |
| **执行** | 线性执行：对每个 Agent 依次 `app_ready(agent)` → `send_query` → `get_response`；可用 `queries_per_agent` 为每个 Agent 指定不同 query |
| **产出** | 多份独立输出（每 Agent 一份），报告按 Agent 拆分展示 |

**Goal 示例**：
- 「对比三个 agent 的表现」
- 「分别测试 Research Proposal、Market Analysis、Project Proposal」
- 「测多个 agent 各跑一次」

---
### 区分要点

| 模式 | agents 数 | runs | output_type | 产出数量 |
|------|-----------|------|-------------|----------|
| 单 Agent 多轮 | 1 | > 1 | 无关 | runs 份 |
| 多 Agent 协作 | > 1 | 通常 1 | single_report | 1 份 |
| 一次性测多个 Agent | > 1 | 通常 1 | multi_report | agents 份 |

**关键语义**：
- 「一份报告」「协作」「共同完成」→ 多 Agent 协作
- 「对比」「比较」「分别测试」「各测一次」→ 一次性测多个 Agent
- 「跑 3 轮」「多轮」+ 单 Agent → 单 Agent 多轮

---
### 实现路由

- **Goal Interpreter**（`raft/core/goal_interpreter.py`）：解析 `output_type`、`runs`、`agents`，写入 `collaboration_mode`、`runs`
- **Planner**（`raft/core/planner/goal_planner.py`）：`collaboration_mode` 时用 `agent_master_collaboration_block`，否则用 `multi_agent_linear_block`
- **Runner**（`raft/orchestrator/runner.py`）：协作模式只生成一条 query；非协作多 Agent 时生成 `queries_per_agent`（若配置）
- **入口**（`run_poffices_agent.py`）：解析 goal 后打印 `[Goal Interpreter] 模式=xxx`，便于确认路由

---
## 5. 测试

```bash
pytest tests -v
# Poffices 相关（不启动浏览器）
pytest tests/test_poffices_agent.py tests/test_poffices_blocks.py tests/test_poffices_llm_agent.py -v
```

---

## 6. 报告与 API

### 报告整体逻辑（原 REPORT_FLOW / REPORT_MULTI_AGENT_LOGIC）

#### 1. 数据来源：results 从哪来

- **入口**：`run_poffices_agent.py` 根据 `--runs N` 循环 N 次，每次调用 `Orchestrator.run_until_done(...)`，得到一份 **result**，append 到 **results** 列表。
- **单份 result**（`run_until_done` 返回值）大致包含：
  - **trajectory**：本 run 的轨迹（每步的 step_result：tool_calls、execution_results、agent_input_snapshot）
  - **metrics**：B8 产出（success、step_count、details、可选 **llm_judge**）
  - **run_id**、**steps_run**、**orchestration_mode**、**query_rationale**（若有）、**task_spec_effective** 等
- **多 Agent**：一个 result 仍是一整次 run，但 trajectory 里包含多段（每 Agent 3 步）；报告层会按「每 3 步一组」拆成 segment，用于按 Agent 展示。

#### 2. 报告生成入口与模式

- **调用位置**：循环结束后，若有 `results`，则调用 `build_report_with_llm(...)`，输出路径一般为 `logs/poffices/run_report.html`。
- **两种模式**（由 CLI 决定）：
  - **完整报告**（默认）：包含 LLM 判分与多轮 LLM 总结（以及「本轮 LLM 简要分析」等内容）。
  - **mini 模式**：不调任何 LLM（不判分、不总结），报告只含输入、输出、轨迹与各 Block 步骤。
  - `--full-report`：启用 LLM 判分与多轮总结，生成完整报告。

#### 3. build_report_with_llm（`raft.reporting.llm_report`）

职责：决定是否调 LLM 多轮总结，并拼出最终 HTML。

- 可选生成 LLM 多轮总结：用 `_prepare_rounds_summaries(results)` 从 results 构建各轮摘要（多 Agent 时按 segment 拆成多条），再调用 `summarize_multi_rounds(...)` 得到 `llm_summary`。
- 拼 HTML：调用 `scripts/build_flow_report.build_multi_flow_report` 生成 HTML 字符串。
- 写文件/返回：可选写入 `output_path`，并返回 `{ "llm_summary", "html", "output_path" }`。

#### 4. build_multi_flow_report（`scripts/build_flow_report.py`）

职责：根据 results + config + task 生成多轮报告的 HTML 结构，不负责调 LLM。

- 汇总统计：success率/平均步数等。
- 多 Agent 展示：遍历 results，对每个 result 调 `get_per_agent_segments(result)`，多 Agent 会按 Agent 拆行并展开展示各段输出。
- 报告展开内容：输入（query/state）、工具序列、输出、可选 LLM 简要分析、可选各 Block 运作说明。

#### 5. 多 Agent 的报告语义保证

- 不再只用「整 run 的最后一个 Agent 输出」做总结；而是按每段（每 Agent 3 步）提取该 Agent 的输出 snippet、成功状态，并参与 LLM 多轮总结输入。
- 若某段 get_response 失败但 `ui_state_delta` 无输出，报告会用该步 `raw_response` 做兜底显示，避免该 Agent 行完全空白。
- 若配置或状态中提供 `queries_per_agent` 且长度与 `agents_to_test` 一致，每个 Agent 段的 `send_query` 会使用对应下标的 query。

- 从轨迹生成报告：`python scripts/generate_report.py logs/e2e_demo -o report.html -f html`。
- B9 跑任务：`POST /api/v1/b9/run`；B8 只评估：`POST /api/v1/b8/evaluate`。详见 [API_CONTRACT.md](API_CONTRACT.md)。

---

## 7. 执行链路

```
实验配置 → create_poffices_agent(config)
  → PofficesLLMAgent / PofficesAgent
  → Orchestrator 每步 state + last_execution_result
  → Agent.run() → tool_calls
  → PofficesRPA.execute → BlockRegistry.execute(block_id, …) → Block.run()
  → ExecutionResult → B5 → B8 评估落盘、多轮 LLM 总结报告
```

**Bootstrap 幂等**：登录 / Agent Master / Business Office 展开 / Market Analysis 选中 / Enable Agent Master Mode / Apply 均有「先检测再操作」；开关误关时 `_ensure_agent_master_mode_on` 自我纠正。详见 `raft/rpa/poffices_bootstrap.py`。

---

## 8. 近期变更记录

| 主题 | 说明 |
|------|------|
| **LLM 驱动 Agent** | `raft/agents/poffices_llm_agent.py`：根据 state + last_execution_result 决定调哪个 Block；解析/API 异常时 fallback 到 PofficesAgent |
| **配置驱动 Agent 类型** | `raft/agents/factory.py`：`create_poffices_agent(config)`，优先级 CLI > 配置 > 默认 rule；`experiment_poffices.json` 的 `extra.agent_type`、`agent_provider` |
| **Bootstrap 幂等** | `_is_business_office_expanded`、`_is_apply_needed` 等；已展开/已选中/已开启则跳过 |
| **Enable Agent Master 开关** | 检测逻辑改为取最后匹配节点再找 switch；点击后校验，误关则再点一次纠正 |

---

## 9. 根目录规范

仅允许 README、pyproject.toml、requirements.txt、run_*.py、progress.json/html、docs/、raft/、scenarios/、scripts/、tests/、logs/ 等；禁止根目录无扩展名/随机命名文件及与 raft 子包同名的文件夹。
