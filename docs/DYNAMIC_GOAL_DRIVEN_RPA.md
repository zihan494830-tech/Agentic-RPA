# 动态 Goal 驱动 RPA 设计

## 目标

用户只需说：「我想用三个 research office 下的 agent 研究石油价格」，系统应：

1. **RPA 先点开 Research Office**（不依赖预置 agent 列表）
2. **从 UI 发现该 office 下有哪些 agent**
3. **LLM 根据 topic（石油价格）分析选哪 3 个 agent 去测**
4. **动态生成并执行 RPA 流程**

即：**RPA 流程不是一开始就定死的，而是根据运行时发现的信息动态生成**。

**核心原则：goal 随时变化，所有信息（office、agent）均从 UI 动态发现，不写死。**

---

## 当前 vs 目标

| 维度 | 当前 | 目标 |
|------|------|------|
| Agent 来源 | scenario 预置 `allowed_agents` | RPA 从 UI 发现 |
| Office 来源 | 无 | RPA 从 UI 发现（不写死 office 列表） |
| 流程设计 | 设计时确定（compound_blocks） | 运行时根据发现动态生成 |
| Office | 无显式概念，直接搜 agent 名 | 先点开 office，再在 office 内选 agent |

---

## 架构设计

### 1. 两阶段执行（全动态）

```
Phase 1: Discovery（发现）
  - RPA: list_offices() → 从 UI 抓取所有 office 名称（不写死）
  - 解析 goal → office_intent="research"（用户意图）, topic="石油价格", count=3
  - LLM: 从 discovered_offices 中匹配 office_intent → 如 "Research Office"
  - RPA: expand_office("Research Office")
  - RPA: list_agents_in_office() → 从 UI 抓取该 office 下的 agent 列表
  - LLM: 从 discovered_agents 中选 count 个最适合 topic 的 → selected_agents

Phase 2: Execution（执行）
  - 用 Phase 1 得到的 agents 动态生成 plan
  - 执行 app_ready(agent_1) → send_query → get_response → ...
```

### 2. 新增 RPA Block（均从 UI 动态获取）

| block_id | 作用 | 输出 |
|----------|------|------|
| `list_offices` | 从 Agent Master 左侧面板抓取所有 office 名称（如 Research Office、Business Office） | raw_response.offices: string[] |
| `expand_office` | 点击展开指定 office | ui_state.office_expanded |
| `list_agents_in_office` | 从当前展开的 office 区域抓取可见 agent 名称列表 | raw_response.agents: string[] |

**不写死**：office 名、agent 名均来自 UI，Poffices 增删改 office/agent 时无需改代码。

### 3. Goal Interpreter 扩展（只解析用户意图）

解析出**语义意图**（非具体 UI 文本）：

- `office_intent`: 如 "research"、"研究相关的"、"business"（用户怎么说就怎么解析，用于后续匹配）
- `topic`: 如 "石油价格"
- `count`: 如 3
- `agents`: 留空，由 Discovery 阶段填充

`office_intent` 与 `list_offices()` 返回的列表在运行时由 LLM 做模糊匹配，得到实际 UI 中的 office 名。

### 4. Discovery 流程（全动态）

```
1. RPA: list_offices() → discovered_offices（若未登录则先 bootstrap）
2. interpret_goal(goal) → GoalIntent{ office_intent, topic, count, agents=[] }
3. 若 office_intent 非空且 agents 为空：
   a. LLM: 从 discovered_offices 中匹配 office_intent → matched_office
   b. RPA: expand_office(matched_office)
   c. RPA: list_agents_in_office() → discovered_agents
   d. LLM: 从 discovered_agents 中选 count 个最适合 topic 的 → selected_agents
   e. GoalIntent.agents = selected_agents
4. 用最终的 GoalIntent 进入现有 goal_driven 流程
```

### 5. Poffices UI 结构（已确认）

- 左侧：可展开的 Office 列表（Research Office、Business Office、Strategy Office 等）
- 每项格式：`Office 名 (x/y selected)`
- 展开后可见该 office 下的 agent 列表
- 搜索框 "Search agents or offices..." 可搜索

---

## 实现步骤（建议顺序）

1. **RPA 层**：实现 `list_offices`、`expand_office`、`list_agents_in_office`（均从 UI 抓取，不写死）
2. **Goal Interpreter**：扩展解析 `office_intent`、`topic`、`count`，`agents` 可为空
3. **Discovery 服务**：`match_office(office_intent, discovered_offices)`、`select_agents_for_topic(discovered_agents, topic, count)`（LLM 或规则）
4. **Orchestrator**：在 goal_driven 流程前插入 Discovery 阶段
5. **Planner**：支持「无 agents 时先跑 Discovery，再基于 discovered_agents 规划」

---

## 与现有设计的关系

- `allowed_agents` 在 scenario 中可保留为**兜底**：Discovery 失败（如 RPA 未就绪、UI 结构变化）时回退
- 当 goal 中无 office 时，沿用当前逻辑（从 scenario 取 agents 或 Goal Interpreter 解析）
- compound_blocks 仍可用于「已知 agents 后的流程模板」，Discovery 只是把「agents 从哪来」从静态改为动态
- **goal 变化时**：换 office、换 topic、换数量，系统均通过 Discovery 重新获取，无需改配置或代码
