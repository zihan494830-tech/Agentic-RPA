# LLM Planner Block 语义增强设计

## 1. 问题

当前 LLM planner 只拿到 `block_catalog` 的 `block_id`、简短 `description`、`params`，缺少：

- **流程归属**：每个 block 属于哪种流程（单 Agent / 多 Agent 协作 / 多 Agent 线性）
- **前置条件与副作用**：执行后页面状态如何变化，会覆盖什么
- **互斥规则**：哪些 block 不能混用（如 app_ready 会覆盖 agent_master_select 的多 Agent 选择）
- **恢复建议**：哪些 block 失败时用什么恢复，`refresh_page` 的副作用与慎用场景

导致 LLM 容易生成错误计划（如协作流程中插入 app_ready/send_query，或滥用 refresh_page）。

---

## 2. 设计目标

让 planner 的 prompt 包含**每个 block 的详细语义**，使 LLM 能：

1. 根据 goal 选择**正确的流程类型**，不混用
2. 理解 **app_ready** 会选单 Agent、会覆盖多 Agent 选择
3. 理解 **refresh_page** 会清空页面状态，仅在特定恢复场景使用
4. 按 **flow_type** 输出符合语义的步骤序列

---

## 3. 方案：Block 语义 Schema

### 3.1 在 scenario 中新增 `block_semantics`

在 `poffices-agent.json` 中增加 `block_semantics`，为每个 block 补充规划所需信息：

```json
{
  "block_semantics": {
    "flow_types": {
      "single_agent": {
        "description": "单 Agent 测试：选一个 agent，发 query，取结果",
        "steps": ["app_ready", "send_query", "get_response"],
        "when": "agents_to_test 长度为 1，且非协作模式"
      },
      "multi_agent_linear": {
        "description": "多 Agent 分别测试：依次对每个 agent 执行 app_ready→send_query→get_response",
        "steps": "对每个 agent 迭代 [app_ready, send_query, get_response]",
        "when": "agents_to_test 长度 > 1，collaboration_mode=false"
      },
      "agent_master_collaboration": {
        "description": "多 Agent 协作：选多个 agent 进 Flow，执行一次 query 产出一份报告",
        "steps": ["discovery_bootstrap", "agent_master_select_agents_for_flow", "agent_master_run_flow_once"],
        "when": "agents_to_test 长度 > 1，collaboration_mode=true"
      }
    },
    "blocks": [
      {
        "block_id": "app_ready",
        "description": "打开应用并进入可操作状态；可选指定要测试的 Agent。",
        "flow_type": "single_agent",
        "semantic_detail": "在单 Agent 模式下选择「一个」agent 并 Apply。会进入单 Agent 查询界面。若在 agent_master 协作流程中调用，会覆盖/破坏已选的多 Agent 状态，导致错误。",
        "precondition": "应用已打开或需先 bootstrap",
        "side_effect": "选择单个 agent，清空或覆盖多 Agent 选择",
        "params": { "options.agent_name": "单个 Agent 名称，来自 agents_to_test[0] 或 agent_name" },
        "do_not_use_in": ["agent_master_collaboration"]
      },
      {
        "block_id": "send_query",
        "description": "在已就绪的会话中发送一条查询并触发执行。",
        "flow_type": "single_agent",
        "semantic_detail": "在单 Agent 模式下向当前选中的「一个」agent 发送 query。协作流程中不应使用，协作由 agent_master_run_flow_once 负责发 query。",
        "precondition": "已完成 app_ready（单 Agent）或 agent_master_select（协作）",
        "side_effect": "触发生成，页面进入生成中状态",
        "params": { "query": "必填，来自 initial_state.query 或 queries_per_agent" },
        "do_not_use_in": ["agent_master_collaboration"]
      },
      {
        "block_id": "get_response",
        "description": "等待当前任务完成并取回结果内容。",
        "flow_type": "single_agent",
        "semantic_detail": "等待生成完毕并提取响应。单 Agent 与多 Agent 线性流程的收尾步；协作流程由 agent_master_run_flow_once 内部完成等待与提取。",
        "precondition": "已发送 query，页面在生成中或已完成",
        "side_effect": "无，只读",
        "params": {}
      },
      {
        "block_id": "discovery_bootstrap",
        "description": "登录并打开 Agent Master 面板。",
        "flow_type": "agent_master_collaboration",
        "semantic_detail": "协作流程的入口：打开 Agent Master 界面，为后续 select_agents 做准备。仅用于协作流程。",
        "precondition": "无",
        "side_effect": "进入 Agent Master 面板",
        "params": {},
        "do_not_use_in": []
      },
      {
        "block_id": "agent_master_select_agents_for_flow",
        "description": "清空右侧 Selected Agents，按顺序添加指定 Agent 列表，Apply。",
        "flow_type": "agent_master_collaboration",
        "semantic_detail": "在 Agent Master 中配置多 Agent 协作流：Clear All → 按顺序 Add 每个 agent → Apply。之后必须紧跟 agent_master_run_flow_once，中间不能插入 app_ready 或 send_query，否则会破坏多 Agent 选择。",
        "precondition": "已完成 discovery_bootstrap",
        "side_effect": "右侧 Selected Agents 已配置，Apply 生效",
        "params": { "agents": "Agent 名称列表，来自 agents_to_test" },
        "must_follow_with": "agent_master_run_flow_once",
        "do_not_insert_between": ["app_ready", "send_query"]
      },
      {
        "block_id": "agent_master_run_flow_once",
        "description": "在已配置的 Agent Master Flow 下执行 query，自动点 Next 直到完成，提取最终报告。",
        "flow_type": "agent_master_collaboration",
        "semantic_detail": "协作流程的收尾：在已选多 Agent 的 Flow 下发送 query、自动步进直到完成、提取报告。内部完成「发 query + 等待 + 取结果」，无需再调用 send_query 或 get_response。",
        "precondition": "已完成 agent_master_select_agents_for_flow，且中间未插入 app_ready/send_query",
        "side_effect": "产出一份协作报告",
        "params": { "query": "协作任务描述，来自 initial_state.query" },
        "params": {}
      },
      {
        "block_id": "wait_output_complete",
        "description": "仅等待页面出现「生成完毕」标识，不提取内容。",
        "flow_type": "recovery",
        "semantic_detail": "恢复用：get_response 超时时可先 wait_output_complete 再 get_response。不用于主流程规划。",
        "precondition": "页面在生成中",
        "side_effect": "无",
        "params": { "timeout_sec": "可选" }
      },
      {
        "block_id": "refresh_page",
        "description": "刷新当前页面。",
        "flow_type": "recovery",
        "semantic_detail": "【慎用】会整页刷新，清空已选 agent、表单、生成中的内容。仅当 get_response 长时间超时且 wait_output_complete 无效时考虑。刷新后需重新执行 app_ready 或 agent_master_select 等，否则后续步骤会失败。协作流程中 refresh 会丢失多 Agent 配置，恢复成本高。",
        "precondition": "无",
        "side_effect": "清空页面所有状态（agent 选择、表单、生成结果）",
        "params": {},
        "use_with_caution": true,
        "after_refresh_must": "重新执行 app_ready 或 discovery_bootstrap + agent_master_select_agents_for_flow"
      }
    ]
  }
}
```

### 3.2 规划器 prompt 构建

在 `_llm_plan` 中，将 `block_catalog` 替换或扩展为「规划专用」的 block 语义描述，例如：

```python
def _build_planner_block_semantics(spec: ScenarioSpec) -> str:
    """从 scenario 的 block_semantics 或 allowed_blocks 构建供 LLM 阅读的 block 语义文本。"""
    semantics = getattr(spec, "block_semantics", None) or {}
    flow_types = semantics.get("flow_types", {})
    blocks = semantics.get("blocks", [])

    parts = ["## 流程类型（三选一，不可混用）"]
    for ft_id, ft in flow_types.items():
        parts.append(f"- **{ft_id}**: {ft.get('description')}")
        parts.append(f"  步骤: {ft.get('steps')}")
        parts.append(f"  适用: {ft.get('when')}")

    parts.append("\n## Block 详细语义")
    for b in blocks:
        parts.append(f"\n### {b['block_id']}")
        parts.append(f"- 描述: {b.get('description')}")
        parts.append(f"- 流程归属: {b.get('flow_type')}")
        if b.get("semantic_detail"):
            parts.append(f"- 详细说明: {b['semantic_detail']}")
        if b.get("do_not_use_in"):
            parts.append(f"- 禁止在以下流程中使用: {b['do_not_use_in']}")
        if b.get("must_follow_with"):
            parts.append(f"- 必须紧跟: {b['must_follow_with']}")
        if b.get("do_not_insert_between"):
            parts.append(f"- 与以下 block 之间不得插入其他步骤: {b['do_not_insert_between']}")
        if b.get("use_with_caution"):
            parts.append(f"- 【慎用】{b.get('semantic_detail', '')}")
        parts.append(f"- params: {b.get('params', {})}")

    return "\n".join(parts)
```

在 user_prompt 中，将原来的 `可用 blocks: {json.dumps(block_catalog)}` 改为上述语义文本，或在其后追加。

### 3.3 约束强化

在 prompt 的「约束」部分显式加入：

```
流程选择规则：
- 若 initial_state.collaboration_mode=true 且 agents_to_test 长度>1 → 必须使用 agent_master_collaboration 流程，且仅使用 discovery_bootstrap、agent_master_select_agents_for_flow、agent_master_run_flow_once，不得插入 app_ready、send_query、get_response。
- 若 agents_to_test 长度=1 → 使用 single_agent 流程：app_ready → send_query → get_response。
- 若 agents_to_test 长度>1 且 collaboration_mode=false → 使用 multi_agent_linear：对每个 agent 迭代 app_ready → send_query → get_response。

refresh_page：仅在恢复计划中且 get_response 多次失败时考虑；主流程规划中不要使用。
```

---

## 4. 实现步骤建议

| 步骤 | 内容 |
|------|------|
| 1 | 在 `ScenarioSpec` 或 `poffices-agent.json` 中增加 `block_semantics` 结构（可先与 `allowed_blocks` 并存） |
| 2 | 在 `raft/core/config/scenario.py` 中增加 `resolve_block_semantics(config)`，返回供 planner 使用的语义文本或结构化数据 |
| 3 | 修改 `_llm_plan`：用 `resolve_block_semantics` 的输出来构建 prompt 中的 block 说明，替代或补充原有 `block_catalog` |
| 4 | 在 prompt 的约束部分加入「流程选择规则」与「refresh_page 慎用」说明 |
| 5 | 用 `--force-llm-plan` 跑协作 goal，验证生成的计划不再混入 app_ready/send_query |

---

## 5. 备选：轻量级增强

若暂不引入完整 `block_semantics`，可先在 `allowed_blocks` 的 `description` 中加长文案，并在 scenario 的 `constraints.notes` 中补充流程规则，例如：

```json
"notes": [
  "默认流程必须先完成 app_ready，再发送 query，最后获取响应。",
  "恢复流程只能使用本场景允许的 Block。",
  "协作流程（多 Agent 产出一份报告）必须且仅使用 discovery_bootstrap → agent_master_select_agents_for_flow → agent_master_run_flow_once，中间不得插入 app_ready 或 send_query。",
  "app_ready 会选择单个 agent，会覆盖 agent_master 的多 Agent 配置，协作流程中禁止使用。",
  "refresh_page 会清空页面状态，主流程规划中不要使用，仅用于失败恢复。"
]
```

然后确保 `scenario_context` 或 `constraints` 完整传入 planner，使 LLM 能读到这些说明。

---

## 6. 小结

| 改进点 | 作用 |
|--------|------|
| **block_semantics** | 每个 block 的 flow_type、semantic_detail、side_effect、do_not_use_in 等，让 LLM 理解「能做什么、会破坏什么」 |
| **flow_types** | 明确三种流程的步骤与适用条件，避免混用 |
| **约束强化** | 在 prompt 中显式写出「协作流程不得插入 app_ready/send_query」「refresh_page 慎用」 |
| **轻量备选** | 先扩写 description 与 constraints.notes，快速缓解问题 |
