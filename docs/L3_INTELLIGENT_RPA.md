# L3 智能 RPA：目标驱动与动态流程

本文档合并原 `LEVEL3_INTELLIGENT_RPA.md`、`L3_MULTI_AGENT_AND_COMPOUND_BLOCKS.md`、`DYNAMIC_GOAL_DRIVEN_RPA.md`，完整描述 L3 的设计思路、已落地能力与演进方向。

---

## 1. 从 L2 到 L3

| 等级 | 本质 |
|------|------|
| **L1** | 硬编码脚本 |
| **L2** | 模块化 Block + LLM 从固定 block_catalog 选下一步 |
| **L3** | 根据目标动态生成步骤序列或 DAG，目标导向自动化 |

**L2 的局限**：流程形态（先做什么后做什么、是否重试）由规则/提示隐式约定，无法随目标变化；Block 本身是预定义的，无法「组合新流程」。

**L3 核心新增能力**：
1. 目标理解与分解——把高层目标拆成可执行子目标序列
2. 动态计划生成——根据目标、状态、Block 能力描述生成「Plan」
3. 计划驱动执行——Orchestrator 按 Plan 推进，失败时触发重规划
4. Block 可发现性——Block 带语义描述，规划器可选择/组合

---

## 2. 架构：规划器 + 计划驱动编排

```
目标 → Planner 生成 Plan（步骤序列/DAG）→ Orchestrator 按 Plan 执行 → 失败时 Replanner
```

### 关键组件

- **GoalSpec**（扩展 TaskSpec）：显式目标、成功条件、约束（超时、必须/禁止的 block）
- **Planner**：输入 GoalSpec + 当前 state + block_catalog，输出 Plan（步骤 + 依赖 + 推荐 block_id）
- **Plan**：与 B3 的 WorkflowDAG 对齐；每节点含 `sub_goal`、`suggested_block_id`、`params_schema`
- **Replanner**：执行失败时，基于当前 state + 未完成子目标重新生成剩余计划

### 与现有架构衔接

| Block | L3 下的角色 |
|-------|------------|
| B2 | 输出作为「难度/策略提示」传给 Planner，不再唯一决定 DAG |
| B3 | DAG 来源从固定拓扑扩展为 Planner 输出的 Plan |
| B4 | 继续按步分配 Agent；若计划中已带 suggested_block_id，B6 优先采用 |
| B6 | 仍选 block + 参数；输入中增加「当前计划」「当前子目标」 |
| B7 | 不变；可扩展「复合 Block」（多原子块），对 B7 仍是单个 block_id |

---

## 3. 多 Agent 支持

### 3.1 多 Agent 目标输入

- **配置**：`config.extra["agents_to_test"]` 或 `initial_state["agents_to_test"]`（字符串列表）
- **CLI**：`--agents "A,B,C"` 解析后写入 `agents_to_test`；与 `--agent` 互斥或 `--agents` 优先
- **规划器展开**：对每个 agent 生成 `app_ready(agent) → send_query → get_response` 线性段

### 3.2 一轮 vs 多轮语义

- **「一轮」** = 一次 `run_until_done`，即一整条计划从第一步到最后一步
- `--agents "A,B,C"` 时，计划包含 9 步（每个 Agent 3 步），**一轮内全部跑完**
- `--runs N` = 整条计划跑 N 次（例如换 query 或多轮策略）
- 示例：`--runs 1 --agents "A,B,C"` = 跑 1 次，依次测完 A→B→C

### 3.3 max_steps 与重试预算

- **问题**：恢复步骤与正常步骤共用 max_steps，容易不足
- **方案**：编排器使用 `effective_max_steps = max(max_steps, 计划步数 + retry_step_budget)`
- 默认 `retry_step_budget = max(4, 2*max_replans+2)`，可通过 `config.extra["retry_step_budget"]` 覆盖
- 入口脚本在 `agents_to_test` 长度 >1 时自动设 `max_steps = max(10, len(agents)*3+4)`

### 3.4 同一会话内切换 Agent

- **前提**：已完成上一 Agent 的 get_response，`_has_completed_first_query == True`
- **行为**：`click_new_question(page)` → `select_agent_on_current_page(page, agent_name)`
- **等待**：切换前约 2s、搜索框等待超时 20s，避免 SPA 未渲染导致卡住

### 3.5 Query 机制：共用 vs 独立

| 方式 | 说明 |
|------|------|
| **默认（共用）** | 每 run 一条 query，所有 Agent 共用 |
| **CLI `--queries "q1,q2,q3"`** | 与 `--agents` 一一对应，规划器为每 Agent 使用独立 query |
| **LLM 多 Agent 出题** | `agents_to_test` 长度 >1 且 `use_llm_query` 时，调用 `suggest_queries_for_agents` 生成 N 条 query，写入 `queries_per_agent` |

---

## 4. 动态 Goal 驱动发现（Discovery）

适用场景：用户说「用三个 research office 下的 agent 研究石油价格」，系统不依赖预置列表，从 UI 动态发现 office 和 agent。

### 4.1 两阶段执行

```
Phase 1: Discovery
  list_offices() → 从 UI 抓取所有 office 名
  interpret_goal() → office_intent="research", topic="石油价格", count=3
  LLM 从 discovered_offices 匹配 office_intent → matched_office
  expand_office(matched_office)
  list_agents_in_office() → 从 UI 抓取该 office 下 agent 列表
  LLM 从 discovered_agents 选 count 个最适合 topic → selected_agents

Phase 2: Execution
  用 Phase 1 的 agents 动态生成 plan，正常执行
```

### 4.2 新增 RPA Block

| block_id | 作用 | 输出 |
|----------|------|------|
| `list_offices` | 从 Agent Master 左侧面板抓取所有 office 名称 | `raw_response.offices: string[]` |
| `expand_office` | 点击展开指定 office | `ui_state.office_expanded` |
| `list_agents_in_office` | 从展开的 office 区域抓取 agent 名称 | `raw_response.agents: string[]` |

所有信息均从 UI 动态获取，不写死；Poffices 增删 office/agent 时无需改代码。

### 4.3 与现有设计的关系

- `allowed_agents` 在 scenario 中保留为**兜底**（Discovery 失败时回退）
- goal 中无 office 时，沿用当前逻辑（从 scenario 取 agents 或 Goal Interpreter 解析）
- compound_blocks 仍可用于「已知 agents 后的流程模板」

---

## 5. 当前已落地能力

已实现（L3 主链路）：

- `orchestration_mode="goal_driven"`：运行前先由 Planner 生成计划，再执行
- **结构化计划**：`GoalPlan` / `GoalPlanStep`（含 `depends_on` 依赖）
- **计划可观测**：输出 `plan_source`、`planned_tool_calls`、`plan_history`
- **失败重规划**：`replan_on_failure + max_replans` 触发 Replanner
- **可选 LLM 规划**：`use_llm_planner=true` 时 LLM 优先，失败规则兜底
- **非白名单 Agent**：`--agent` 可传任意名称，注入 `app_ready.options.agent_name`
- **多 Agent 线性计划**：`agents_to_test` 自动展开为每个 Agent 的 3 步段
- **统一入口**：`run_poffices_agent.py` 默认 `goal_driven`，旧模式仅底层保留兼容

未实现（边界）：

- 运行时自动写入并加载新 Python Block 代码（仍需开发时新增并注册）
- 复合 Block DSL（子 DAG 执行器）与并发执行引擎（当前为顺序执行）
- Discovery 阶段完整集成到 Orchestrator（架构设计完成，代码待实现）

---

## 6. 验收用例

### 6.1 目标驱动 + 非白名单 Agent

```bash
python run_poffices_agent.py --runs 1 --agent "Your Custom Agent"
```
检查：第一步 `app_ready` 携带 `options.agent_name="Your Custom Agent"`，且完成完整流程。

### 6.2 失败后重规划

注入中间失败，检查 `replan_count > 0`，`plan_history` 出现 `replan_rule` 或 `replan_llm`，后续继续执行并触达 `get_response`。

### 6.3 多目标差异化计划

用两条差异明显的任务描述分别运行，对比 `planned_tool_calls` / `plan_history` 是否不同，验证规划结果与目标语义有关联。

### 6.4 多 Agent 计划展开

```bash
python run_poffices_agent.py --runs 1 --agents "A,B,C"
```
检查 `planned_tool_calls` 为 9 步，各 `app_ready` 的 `options.agent_name` 正确对应。
