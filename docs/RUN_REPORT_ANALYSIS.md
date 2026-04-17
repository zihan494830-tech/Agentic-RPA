# 单次运行报告分析：做了什么、是否重试、为何慢

## 1. 这次运行做了什么（按时间顺序）

| 阶段 | 动作 | 说明 |
|------|------|------|
| **Discovery** | office_intent=research, topic=石油价格, count=3 | 解析 goal |
| | 选中 3 个 agent | Market Analysis, Business Forecasting Objective, Supply Chain Evaluation（由 LLM 从 allowed_agents 中选） |
| **[1/3]** | 生成任务描述 | 调用 LLM（task_description_suggester） |
| **[2/3]** | 为 3 个 Agent 生成 query | 调用 LLM（query_suggester.suggest_queries_for_agents），生成 3 条测试问句 |
| **[3/3]** | 构建 RPA 计划 | 使用 compound_blocks 的 multi_agent_linear_block，展开为 9 步（3 agent × 3 步） |
| **执行** | 计划步数 9 | app_ready(A1) → send_query → get_response → app_ready(A2) → send_query → get_response → app_ready(A3) → send_query → get_response |
| | Agent 1 (Market Analysis) | **get_response 失败**（未等到「Document generation is completed」或超时） |
| **重试** | 恢复计划 | 见下文 |
| **结束** | 步数=8，失败 | 未跑完 3 个 agent，整轮记为失败 |
| **报告** | LLM 多轮分析总结 | 再调一次 LLM（约 1–3 分钟） |

---

## 2. 步骤失败有没有尝试重试？

**有。** 失败时触发了「重规划」并插入了恢复步骤。

- **配置**：`replan_on_failure: true`，`max_replans: 2`（experiment_poffices_dynamic 继承默认）。
- **失败步**：`get_response`（等待 Poffices 生成完成并取回结果）。
- **恢复计划**（`build_recovery_plan` 规则）：
  - 因失败的是 `get_response`，且 block_catalog 中有 `refresh_page`、`wait_output_complete`、`get_response`，所以生成：
    - `refresh_page` → `wait_output_complete` → `get_response`
  - 若再次失败，还可能再生成一次恢复（如 app_ready → send_query → wait_output_complete 等），直到 `replan_count >= max_replans` 或恢复计划为空才停止。

你看到的「每轮摘要」里 8 步为：

`app_ready → send_query → get_response → refresh_page → wait_output_complete → app_ready → send_query → wait_output_complete`

对应的是：**第一次 3 步（Agent1）→ get_response 失败 → 插入 3 步恢复（refresh + wait + get_response）→ 可能 get_response 再次失败后又插入了 app_ready + send_query + wait_output_complete**，所以总步数变成 8，且整轮仍被判为失败。

结论：**有重试/恢复**，但恢复后仍未能成功拿到结果，因此最终显示失败。

---

## 3. 为什么执行这么慢？

| 来源 | 耗时大致来源 |
|------|----------------|
| **Discovery** | 4 次 RPA（bootstrap、list_offices、expand_office、list_agents_in_office）+ 2 次 LLM（match_office、select_agents_for_topic） |
| **[1/3] 任务描述** | 1 次 LLM |
| **[2/3] 为 3 个 Agent 出题** | 1 次 LLM（suggest_queries_for_agents） |
| **[3/3] 构建计划** | 可能 1 次 LLM（use_llm_planner: true） |
| **RPA 执行** | 每步 `get_response` 会**一直等到页面出现「Document generation is completed」**（默认最多 query_wait_sec，如 120s），Poffices 后台生成文档本身就要时间 |
| **恢复步骤** | 失败后插入 refresh_page、wait_output_complete、get_response（或 again app_ready→send_query→wait），再次等待页面/生成，整体步数变多 |
| **报告** | 1 次 LLM 多轮分析总结（约 1–3 分钟） |

所以慢的主要是：**多次 LLM 调用 + 每步 get_response 的长时间等待 + 失败后的恢复步骤 + 报告 LLM**。

---

## 4. 明确了要测哪些 agent 之后，是不是「直接调验证过的 block」就行？

**是。** 当前设计已经是：**一旦确定了 agents，计划就是由验证过的 compound block 展开出来的**。

- 多 Agent 时用的是 `multi_agent_linear_block`：
  - 每个 agent 固定 3 步：`app_ready` → `send_query` → `get_response`
  - 来自 `scenarios/poffices-agent.json` 的 compound_blocks，是预先定义好的流程，不是每步临时再让 LLM 选工具。
- 因此「慢」**不是因为没在用验证过的 block**，而是：
  1. **前面多了几轮 LLM**：任务描述、为 3 个 agent 出题、可能还有 LLM 规划；
  2. **每个 get_response 都要等 Poffices 真正生成完**，这是业务等待时间；
  3. **失败后的恢复**又加了几步（refresh、wait、再次 get 或 again send_query+wait）。

若要加速，可以考虑：

- **减少前置 LLM**：例如在 goal 已很明确时，跳过或简化「任务描述」、甚至用固定/模板 query，少调 1～2 次 LLM。
- **缩短 get_response 等待**：适当调小 `query_wait_sec` 或改进「完成」检测逻辑（在保证能取到结果的前提下）。
- **报告**：默认 mini 模式不调 LLM 总结；需完整报告时加 `--full-report`（会多 1–3 分钟）。
- **失败策略**：对 get_response 超时是否立刻做一长串恢复（refresh + wait + get_response again），可做成可配置或更保守，避免一次失败就拖很久。

---

## 5. 小结

| 问题 | 结论 |
|------|------|
| 这次报告里做了什么？ | Discovery 定 3 个 agent → 3 次 LLM（任务描述、3 条 query、计划）→ 9 步 RPA（3× app_ready/send_query/get_response）→ Agent1 的 get_response 失败 → 插入恢复步骤 → 共 8 步后结束，整轮失败 → 再跑一次 LLM 做报告总结。 |
| 步骤失败有没有重试？ | 有；get_response 失败后触发了 replan，插入了 refresh_page、wait_output_complete、get_response（以及可能又一次 app_ready→send_query→wait），但恢复后仍失败。 |
| 为什么这么慢？ | 多次 LLM（Discovery + 任务描述 + 出题 + 可能规划 + 报告）+ 每个 get_response 等文档生成 + 失败恢复带来的额外步骤与等待。 |
| 是否已用验证过的 block？ | 是；多 Agent 计划就是 multi_agent_linear_block 展开的 app_ready→send_query→get_response，没有每步临时乱选工具。 |
