# L3：多 Agent 目标与复合 Block 方案

在「目标驱动 + 计划驱动执行」基础上，支持：**你说明要测几个 Agent，系统自动规划整体流程并调用现有 RPA Block（或配置级复合 Block）执行**。

---

## 1. 目标

- **输入**：用户声明「要测的 Agent 列表」（如 `--agents "Research Proposal,Market Analysis,Project Proposal"` 或配置 `agents_to_test`），可选同一 query 或每 agent 不同 query。
- **规划**：Planner 根据「多 Agent 目标」生成**一整条**执行计划（如对每个 Agent：app_ready(agent) → send_query → get_response），而不是由脚本多次调 run。
- **执行**：Orchestrator 按该计划顺序执行；**同一浏览器会话内**通过现有 `app_ready`（或扩展）在完成上一 Agent 后**切换**到下一 Agent，再 send_query → get_response。
- **新 Block**：优先**复用现有 Block**；「创建新 Block」在首阶段指**配置级复合 Block**（由若干原子 block_id 组成的一个逻辑块，无需写 Python），不包含运行时生成并加载新 Python 代码。

---

## 1.5 一轮 vs 多轮：消除歧义

- **「一轮」** = 一次 `run_until_done`，即**一整条计划**从第一步执行到最后一步。
- **`--agents "A,B,C"`** 时，这条计划**包含 9 步**（A 的 3 步 + B 的 3 步 + C 的 3 步）。因此：
  - **不会**因为「只跑一轮」而只测第一个 Agent 就停；**一轮 = 把 A、B、C 都测完**，共 9 步，然后本 run 结束。
  - 之前出现「只跑 5 步就关」是因为 `max_steps=5` 限制了步数，不是「一轮」的语义问题；已改为按 Agent 数自动提高 `max_steps`。
- **`--runs N`** = 上述「一整条计划」跑 **N 次**。例如 `--runs 1 --agents "A,B,C"` = 跑 1 次，这一次里依次测完 A→B→C；`--runs 2` 则会把「A→B→C 全测一遍」执行 2 次（例如换 query 或做多轮策略）。
- 总结：**三个 agent 都测一次** = 用 `--runs 1 --agents "A,B,C"` 即可，一轮内会依次测完三个，不会判成「只跑一轮所以只测一个」。

### max_steps 与重试

- **问题**：一开始设定了 `max_steps`，失败时插入的恢复步骤和正常步骤**共用**同一预算，步数容易不够用。
- **做法**：编排器使用 **effective_max_steps = max(max_steps, 计划步数 + retry_step_budget)**。即至少允许「计划步数 + 重试预算」步；若你设的 `max_steps` 更大则以其为绝对上限。默认 `retry_step_budget = max(4, 2*max_replans+2)`，可通过 `config.extra["retry_step_budget"]` 覆盖。
- **效果**：遇错误需要重试时，不会因为一开始设定的 `max_steps` 刚好等于计划步数而提前退出；重试有步数空间。

---

## 2. 与现有 L3 的衔接

| 能力 | 现状 | 本方案 |
|------|------|--------|
| 计划来源 | `agent_under_test` 单名 → 规划器生成 app_ready(单 agent) → send_query → get_response | 支持 `agents_to_test: [A,B,C]` → 规划器展开为多段 (app_ready(A), send_query, get_response), (app_ready(B), …), … |
| app_ready 多轮 | 首轮后仅 `click_new_question`，不换 Agent | 首轮后若 params 带 `options.agent_name` 且与当前不同，则 New question + **切换 Agent**（搜索、选择、Apply） |
| 复合 Block | 未实现 | 配置 `compound_blocks: { "id": [block_id, ...] }`，执行时按序调现有 Block（可选后续再做） |

---

## 3. 实现要点

### 3.1 多 Agent 目标输入

- **配置/状态**：`config.extra["agents_to_test"]` 或 `initial_state["agents_to_test"]` 为字符串列表（如 `["Research Proposal", "Market Analysis"]`）。若存在且长度 ≥ 1，规划器使用；否则退化为单 `agent_under_test`。
- **CLI**：`run_poffices_agent.py --agents "A,B,C"` 解析为列表并写入 `config.extra["agents_to_test"]`；与 `--agent` 互斥或 `--agents` 优先。
- **Runner**：构建 `planning_state` 时，若存在 `agents_to_test` 则传入规划器；单 agent 时仍传 `agent_name`（来自 `agent_under_test`）。

### 3.2 规划器展开多 Agent 计划

- **规则规划器**：若 `initial_state` 含 `agents_to_test` 且为 list、长度 > 1，且 block_catalog 含 app_ready / send_query / get_response，则生成线性序列：
  - 对每个 agent in agents_to_test：  
    `ToolCall("app_ready", {"options": {"agent_name": agent}})` →  
    `ToolCall("send_query", {"query": query})` →  
    `ToolCall("get_response", {})`
  - 使用同一 `query`（来自 initial_state.query 或 _pick_query）。
- **LLM 规划器**：已有 task_description + initial_state；若 state 中含 `agents_to_test`，prompt 中说明「依次对列表中每个 agent 执行 app_ready(agent) → send_query → get_response」，由 LLM 输出对应 steps（或规则兜底时按上一条展开）。

### 3.3 同一会话内切换 Agent（app_ready 扩展）

- **前提**：当前已执行过 get_response，即 `rpa._has_completed_first_query == True`；下一计划步为 app_ready(另一 agent_name)。
- **行为**：  
  1. 调用现有 `click_new_question(page)`。  
  2. 调用新增 `select_agent_on_current_page(page, agent_name)`：在**当前页面**（已登录、主内容区可见）仅做「打开 Agent Master 若需 → 搜索 agent_name → 选择 → Apply」，不重新 goto/登录。
- **poffices_bootstrap**：新增 `select_agent_on_current_page(page, agent_name, timeout_ms, log_fn)`，抽取并复用 run_bootstrap 中从「Clear All / 搜索框」到「Apply」的 Agent 选择逻辑；若侧栏未打开则先点击 Agent Master 再选择。
- **AppReadyBlock**：当 `_has_completed_first_query` 且 params 中 `options.agent_name` 存在时，先 `click_new_question`，再 `select_agent_on_current_page(page, agent_name)`，并返回成功/失败 ExecutionResult。

### 3.4 复合 Block（可选，后续）

- **配置**：`config.extra["compound_blocks"]` 形如 `{ "test_one_agent": ["app_ready", "send_query", "get_response"] }`。
- **执行**：B7 或 BlockRegistry 若发现 tool_name 为复合块 id，则按序执行子 block，参数可按约定拆分（如首块拿 options，第二块拿 query）或整份 params 传给每块。
- **规划器**：block_catalog 中可加入复合块描述，规划器可输出「对每个 agent 调用 test_one_agent(agent_name, query)」；与「多 agent 展开为原子步骤」二选一或并存。

---

## 4. 验收

- **多 Agent 计划**：传 `--agents "A,B,C"`（或配置 agents_to_test），检查 `planned_tool_calls` 为 9 步：app_ready(A), send_query, get_response, app_ready(B), send_query, get_response, app_ready(C), send_query, get_response（且各 app_ready 的 params.options.agent_name 正确）。
- **切换执行**：在真实或 mock 的 RPA 上执行上述计划，确认第二、第三次 app_ready 会切换 Agent（通过 select_agent_on_current_page），并完成 send_query → get_response。
- **单 Agent 兼容**：不传 agents_to_test 时行为与现有一致（单 agent_under_test → 3 步计划）。

---

## 5. 边界与不做项

- **运行时生成新 Python Block**：不做；新增能力仅通过现有 Block + 配置级复合 Block。
- **并发执行**：计划仍线性执行，依赖仅用于表示顺序；DAG/并发留作后续。
- **每 Agent 不同 query**：首版可用同一 query；若 initial_state 提供 `queries_per_agent: [q1, q2, q3]` 可与 agents_to_test 对齐扩展（后续）。

---

## 6. 故障排查（多 Agent 切换时卡住）

- **现象**：第一轮（如 Research Proposal）跑完后，选 Market Analysis 时一直卡住，随后程序退出，第一轮结果也未体现为「成功」。
- **原因 1：max_steps 不足**  
  多 Agent 时计划步数 = N×3（N 为 Agent 数）。若 Orchestrator 的 `max_steps` 只等于计划步数，**失败重试**插入的恢复步骤会占用同一预算，容易还没跑完就触顶退出。  
  **处理**：  
  - `run_poffices_agent.py` 在存在 `agents_to_test` 且长度 >1 时，将 `max_steps` 设为 `max(10, len(agents_to_test)*3+4)`，预留约 4 步给重试。  
  - **Runner 重试预算**：编排器内部使用 `effective_max_steps = max(max_steps, 计划步数 + retry_step_budget)`，默认 `retry_step_budget = max(4, 2*max_replans+2)`。即：即使你设的 `max_steps` 刚好等于计划步数，也会至少多给「重试步数预算」步，避免「一遇错误就因步数用尽而退出」。可通过 `config.extra["retry_step_budget"]` 覆盖。
- **原因 2：New question 后页面/侧栏未就绪**  
  点击 New question 后若立刻找「Search agents or offices...」或 Agent Master，可能因 SPA 未渲染完而长时间等待或超时。  
  **处理**：在 `app_ready` 切换 Agent 前增加约 2s 等待；`select_agent_on_current_page` 内增加初始 1.5s 等待、侧栏打开后 2.5s 等待，并将搜索框等待超时改为 20s，减少「卡住」感。
- **结果与报告**：轨迹与 metrics 仅在整轮 `run_until_done` 正常返回后写入 JSON/报告。若中途异常退出或杀进程，已执行步不会单独落盘；需待程序正常跑完才会看到完整轨迹与「第一轮成功」等指标。

---

## 7. 为何三个 Agent 默认共用同一 query？原因与可选方案

### 7.1 直接原因（未启用「多 Agent LLM 出题」前提下）

- **Query 建议器默认是「按 run」调用的，不是「按 Agent」**  
  每次 `run_until_done` 开始时，Runner 至少会有一次 `_get_initial_state_for_run`，得到**一条** `initial_state["query"]`（来自 LLM 的 `suggest_query` 或 task_spec.initial_state）。在**未启用多 Agent LLM 出题路径**（即不走 `suggest_queries_for_agents`）时，多 Agent 情况下同一次 run 内仍只会为整条计划生成一条 query，因此三个 Agent 默认共用这条 query。
- **`queries_per_agent` 默认未设置**  
  只有通过 CLI `--queries "q1,q2,q3"` 或配置 `extra.queries_per_agent` 时，规划器才会为每个 Agent 使用不同 query。不传则规划器用同一 `query` 填满所有 send_query。
- **轮数语义是「run」维度的**  
  「一轮」= 一次 run = 一次调用 query 建议器。单 Agent 时 1 run = 1 个 query；多 Agent 时 1 run 仍只产生 1 个 query，只是被 3 个 Agent 各用一次。因此不是单/多 Agent 的轮数歧义，而是**设计上就是「每 run 一条 query」**。

### 7.2 Query 建议器当前如何工作（含场景规范）

- **触发条件**：实验配置里 `use_llm_query: true`（如 `experiment_poffices.json`）时，每个 **run** 开始前会调用 Query 建议器。
- **输入**：任务描述（effective_task_spec.description，可由场景规范 `ScenarioSpec` + use_llm_task_description 生成）、待测 Agent 描述（agent_descriptor，多 Agent 时为「Poffices 的 N 个 Agent（A, B, C）」）、多轮时还有 previous_rounds / previous_queries，以及当前场景的 `scenario_context`（由场景规范展开，包括允许 Agent/Block、流程模板与约束摘要）。
- **输出**：**一条** query 字符串，写入 `initial_state["query"]`。多 Agent 时该 query 默认被规划器复用到每个 send_query，除非另外提供 `queries_per_agent`。
- **与 scenario 的联动**：任务描述来自 effective_task_spec（可被 use_llm_task_description 用 `scenario_prompt` 生成），Query 建议器还会显式接收 `scenario_context`，其中包含当前 `ScenarioSpec` 的关键信息（允许 Agent/Block、流程模板与约束摘要）。在未启用多 Agent 出题前提下，它仍只产出「本 run 一条 query」，不会因「本 run 要测 3 个 Agent」而自动产出 3 条。

### 7.3 可选方案（已实现）

| 方式 | 说明 |
|------|------|
| **CLI `--queries "q1,q2,q3"`** | 与 `--agents "A,B,C"` 一一对应，规划器使用 `queries_per_agent`，每个 Agent 独立 query；不依赖 LLM。 |
| **多 Agent 时 LLM 为每个 Agent 出题** | 当 `agents_to_test` 长度 >1 且 `use_llm_query` 时，可调用 `suggest_queries_for_agents`，根据**任务描述 + Agent 列表**一次生成 N 条 query，写入 `initial_state["queries_per_agent"]`，与 scenario 描述联动。 |

### 7.4 多 Agent 与 scenario 联动的自动出题（实现说明）

- 当 `_get_initial_state_for_run` 检测到 `extra.agents_to_test` 长度 >1 且 `use_llm_query` 为 true 时，会调用 `suggest_queries_for_agents`，得到与 Agent 顺序一致的 query 列表，写入 `initial_state["queries_per_agent"]`；`initial_state["query"]` 取第一条以兼容单 query 逻辑。
- 规划器从 `planning_state` 读取 `queries_per_agent`（优先来自 initial_state，否则 extra），若长度与 `agents_to_test` 一致则每段 send_query 使用对应下标 query，实现「多 Agent + scenario 联动」的自动每 Agent 一 query。
