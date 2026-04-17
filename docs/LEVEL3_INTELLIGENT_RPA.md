# 第三等级（智能 RPA）实现思路与方案

本文档基于项目当前所处的 **第二等级（基于模块的 RPA）**，给出向 **第三等级（智能 RPA）** 演进的思路与落地方案。  
RPA 三等级简述见项目内参考图：L1 硬编码脚本 → L2 模块化块 + LLM 调用预定义块 → L3 根据目标动态组合新流程、目标导向自动化。

---

## 1. 当前状态（第二等级）简要回顾

| 环节 | 现状 | 局限 |
|------|------|------|
| **B6 决策** | LLM/规则从**固定 block_catalog** 中选一个 block + 参数，每步一次决策 | 流程形态由「规则/提示」隐式约定，无法根据目标生成新流程 |
| **B7 执行** | BlockRegistry 按 `tool_name` 查找并执行**已注册**的 Block | 仅支持预定义 block，无「组合块」或运行时新流程 |
| **B3 DAG** | 由 B2 的 `route_type`（single_flow / multi_flow）**固定拓扑**生成 | 与任务目标无关，不能随目标变化 |
| **任务输入** | TaskSpec.description + initial_state（含 query 等） | 多为单轮、单目标描述，缺少显式「目标分解」与「成功标准」 |

**本质**：L2 是「在给定流程形态下，智能选择下一步调用哪个预定义块」；**流程本身**（先做什么后做什么、是否分支、是否重试）仍是预设或写死在规则/提示里的。

---

## 2. 第三等级目标（对齐参考图）

- **目标导向**：输入为高层目标（及约束），系统自动分解并执行，而非仅「执行已有脚本」。
- **动态组合流程**：代理不仅「调用块」，还能**根据目标生成新的步骤序列或 DAG**（即运行时决定做哪些步骤、先后与依赖），并可复用/组合现有 Block。
- **应对未知情境**：在未预设的流程形态下，能规划多步、重试、分支或回退，而不是单一线性或固定 multi_flow。

---

## 3. 总体思路：从「按步选块」到「先规划后执行」

```
  L2:  每步 → B6 选一个 Block → B7 执行 → 下一步（流程形态隐含在规则/提示中）
  L3:  目标 → 规划器生成「计划」(Plan) → 计划 = 步骤 DAG/序列 → 每步仍由 B6 选块执行（或直接按计划调块）
```

核心新增能力：

1. **目标理解与分解**：把 TaskSpec 中的高层目标拆成可执行的子目标序列或 DAG。
2. **动态计划生成**：根据目标、当前状态、可用 Block 能力描述，生成「计划」（步骤 + 依赖 + 可选分支）。
3. **计划驱动执行**：Orchestrator 按「计划」推进（而非仅按固定 max_steps 线性步进或固定 DAG），并在执行中根据结果**修正/延展计划**（重规划）。
4. **块的可发现性与组合**：Block 带语义描述与 I/O 约束，便于规划器「选择/组合」块以完成子目标；可选支持「复合 Block」（由多个原子 Block 组成）。

---

## 4. 方案一：规划器 + 计划驱动编排（推荐优先）

### 4.1 组件

- **GoalSpec（可选扩展 TaskSpec）**  
  - 显式目标描述、成功条件（何时算完成）、约束（超时、重试上限、必须/禁止的 block）。  
  - 可与现有 `TaskSpec.description`、`initial_state`、`ground_truth` 并存，逐步引入。

- **Planner（规划器）**  
  - 输入：GoalSpec（或 TaskSpec）+ 当前 state + Block 能力清单（block_catalog 增强：含输入输出、前置条件、效果描述）。  
  - 输出：**Plan** = 步骤列表或 DAG；每步对应一个子目标 + 推荐 block_id（可选）+ 参数约束。  
  - 实现：首版可用 LLM（与现有 B6/B2/Query 同一套 API）做「目标→计划」一步生成；后续可加规则/模板 + LLM 修补，或专用规划模型。

- **Plan 数据结构**  
  - 与现有 B3 的 `WorkflowDAG` 对齐或扩展，例如：  
    - `nodes`: 步 ID 或索引；  
    - `edges`: 依赖 (from, to)；  
    - 每节点可带：`sub_goal`、`suggested_block_id`、`params_schema` 等，便于 B6/B7 使用。

- **Plan-Driven Orchestrator 扩展**  
  - 运行前：若启用 L3，先调 Planner 得到 Plan。  
  - 运行中：`get_next_steps` 基于「当前 Plan + 已完成步骤」计算下一步（与现有多 Agent DAG 类似，但 DAG 来源于本次计划）。  
  - 每步仍通过现有 B6 选 block（或若计划中已指定 block，可简化为「按计划调 B7」）。  
  - 执行结果若表明计划不可行（如某步反复失败），可触发 **Replanner**：根据当前 state + 未完成子目标重新生成剩余计划。

### 4.2 与现有架构的衔接

- **B2**：可保留；L3 下可将 B2 输出作为「难度/策略提示」输入给 Planner，而不是唯一决定 DAG 形态。  
- **B3**：DAG 来源从「route_type 固定拓扑」扩展为「Planner 输出的 Plan」；`build_dag` 可接受 `Plan` 生成 `WorkflowDAG`。  
- **B4**：继续按步分配 Agent；若计划中已带 suggested_block_id，B6 可优先采用。  
- **B6**：仍负责「当前步选 block + 参数」；输入中增加「当前计划」「当前子目标」，便于在计划框架内做轻量级决策或重试。  
- **B7**：不变；仍通过 BlockRegistry 执行 block；未来可增加「复合 Block」由多个原子 block 组成，对 B7 仍是一个 block_id。

### 4.3 实施步骤（分阶段）

1. **Phase 3.x（最小 L3）**  
   - 定义 `GoalSpec`（或先在 `TaskSpec.extra` 中放 goal、success_criteria）。  
   - 实现 `Planner` 接口 + 基于 LLM 的规划器，输出「步骤序列」（先线性即可）。  
   - Orchestrator 增加 `orchestration_mode: "goal_driven"`：先调 Planner，再按计划步进；每步仍用现有 B6 → B7。  
   - block_catalog 增强：每块带 `description`、`params`、可选 `preconditions`/`effects`，供规划器使用。

2. **Phase 3.x+1**  
   - Plan 支持 DAG（带依赖的步骤），`get_next_steps` 基于 Plan 的 DAG 计算。  
   - 失败/超时触发 Replanner，只重规划「未完成部分」。

3. **Phase 3.x+2**  
   - 复合 Block：由多个原子 Block 组成的新 block，在 BlockRegistry 中注册为单 block_id，内部按子步骤执行。  
   - 规划器可输出「使用复合 Block」或「一串原子 Block」两种形态，由 B7 统一执行。

---

## 5. 方案二：强化 B6 为「规划+执行」一体（备选）

不单独引入 Planner，而是把「规划」压进 B6 的 LLM：

- 每步不仅输出 `tool_calls`，还可输出 `plan_rest`：剩余步骤的简要计划（或全文只输出一个多步 plan，再由执行层逐步执行）。  
- 优点：改动集中、无需新组件。  
- 缺点：长计划易丢、难以做结构化依赖（DAG）、重规划与状态管理复杂，且与现有「每步一次 tool_calls」的闭环耦合紧。  

更适合作为「轻量级 L3 试点」或与方案一结合（B6 在计划框架内做局部重规划）。

---

## 6. 方案三：块的可发现性与组合（与方案一配合）

- **Block 能力描述**：每个 Block 除 `block_id`、`params` 外，增加可选字段：  
  - `preconditions`：何时可调用（如 `state.app_ready == true`）；  
  - `effects`：执行后状态变化（如 `app_ready`, `query_sent`, `poffices_response`）；  
  - `description`：自然语言能力摘要（已有，可统一格式）。  
- **复合 Block**：  
  - 定义「流程块」：由若干原子 Block 的调用序列（或子 DAG）组成，对外暴露为一个 block_id。  
  - 规划器可选用原子块序列或复合块，实现「根据目标动态组合」；B7 执行层不变。  
- **发现与注册**：BlockRegistry 支持 `list_blocks()` 与能力描述查询；Planner 通过配置或运行时拉取 block_catalog 生成计划。

---

## 7. 技术要点小结

| 能力 | 实现要点 |
|------|----------|
| 目标分解 | GoalSpec / TaskSpec.extra + LLM 规划器，输出子目标序列或 DAG |
| 动态流程 | Plan 作为 B3 的 DAG 来源，Orchestrator 按 Plan 驱动步进与依赖 |
| 重规划 | 检测失败/死循环/超时，调用 Replanner 基于当前 state 与剩余目标重新生成计划 |
| 块组合 | Block 能力描述 + 复合 Block（多原子块组成），规划器可组合成新流程 |
| 兼容 L2 | 保留 single_agent / multi_agent_dag，新增 goal_driven 模式；B6/B7 接口不变 |

---

## 8. 建议优先级

1. **先做方案一**：GoalSpec + Planner + Plan 驱动编排（线性计划即可），与现有 B2/B3/B4/B6/B7 对接，验证「目标→计划→执行」闭环。  
2. **再补 Replanner 与 DAG 计划**：支持依赖与失败重规划。  
3. **再做块能力描述与复合 Block**：实现「动态组合」与更丰富的规划空间。  
4. 方案二可作为 B6 的扩展（在给定计划下做局部调整），与方案一并行。

文档与契约层面建议：在 `docs/ARCHITECTURE.md` 中增加「L3 目标驱动」小节；在 `raft/contracts/models.py` 中增加 `GoalSpec`、`Plan` 等模型（或先在 `extra` 中约定 JSON 结构），便于前后端与评估统一。

---

## 9. 当前已落地能力（代码现状）

已实现（L3 主链路）：

- `orchestration_mode="goal_driven"`：运行前先由 Planner 生成计划，再执行。
- **结构化计划**：新增 `GoalPlan` / `GoalPlanStep`（含 `depends_on` 依赖）。
- **计划来源可观测**：输出 `plan_source`、`planned_tool_calls`、`plan_history`。
- **失败重规划（Replanner）**：执行失败后按 `replan_on_failure + max_replans` 触发恢复计划并继续执行。
- **可选 LLM 规划**：`use_llm_planner=true` 时优先 LLM 规划，失败自动规则兜底。
- **非白名单待测 Agent**：`agent_under_test` 或 `--agent` 可传任意名称，计划会把该名称注入 `app_ready.options.agent_name`。
- **多 Agent 目标**：`agents_to_test`（配置或 `--agents "A,B,C"`）可传多个 Agent 名称，规划器自动展开为「对每个 Agent：app_ready(agent)→send_query→get_response」的线性计划；同一会话内通过 `app_ready` 切换 Agent（New question + 选择新 Agent）。

未实现（边界说明）：

- 运行时自动写入并加载新的 Python Block 代码（当前仍需开发时新增 Block 类并注册）。
- 复合 Block DSL（子 DAG 执行器）与并发执行引擎（当前为顺序执行，依赖仅用于计划表示与线性化）。

### 9.1 运行入口已收敛为“单主干”

为避免“同版本多模式”带来复杂度，运行脚本层已统一：

- 统一入口 `run_poffices_agent.py` 默认固定使用 `goal_driven` 主干（`--runs 1` 即单轮）。
- 旧模式仅在底层保留兼容，不作为日常入口暴露。
- 迭代策略改为：持续升级同一主干，而不是新增并列模式。

---

## 10. 建议验收用例（判断是否达到 L3）

### 10.1 目标驱动 + 非白名单 Agent

目的：验证不是靠固定 agent 列表硬编码。

做法：

1. 在运行命令中传 `--agent "Your Custom Agent"`（统一入口默认 `goal_driven`），且该名称不在 `available_agents`。
2. 检查输出/轨迹中第一步 `app_ready` 是否携带 `options.agent_name="Your Custom Agent"`。
3. 检查是否仍完成完整流程（至少 `app_ready -> send_query -> get_response`）。

判定：

- 通过：说明可在目标驱动下接受新待测 Agent，并完成既有 Block 组合流程。
- 不通过：说明仍存在白名单耦合或参数注入缺失。

### 10.2 失败后重规划恢复

目的：验证不是“失败即停”，而是具备恢复能力。

做法：

1. 开启 `goal_driven`，并注入一次中间失败（如 send_query 首次失败）。
2. 检查 `replan_count > 0`，且 `plan_history` 出现 `replan_rule` 或 `replan_llm`。
3. 检查后续是否继续执行并触达 `get_response`。

判定：

- 通过：说明具备 L3 的关键能力之一（执行中计划修正）。

### 10.3 多目标差异化计划

目的：验证“按目标生成计划”而非固定模板。

做法：

1. 用两条差异明显的任务描述分别运行 `goal_driven`。
2. 对比 `planned_tool_calls` / `plan_history`（步骤顺序、参数、恢复路径）是否不同。

判定：

- 通过：说明规划结果与目标语义有关联，而非完全固定脚本。
