# 项目状态与变更

当前配置与入口、近期变更、历史（模块化与测试修复）合一。

---

## 一、当前状态

**项目状态**：B1–B9 与闭环 1 已贯通；**RPA 第三等级（L3，智能 RPA）已基本实现**（动态场景、`--goal`、ScenarioSpec 与 L3 规划器接入主线）。更深闭环与 L3 细部增强见 [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)、[LEVEL3_INTELLIGENT_RPA.md](LEVEL3_INTELLIGENT_RPA.md)，**后续优化择机推进**。

### 配置与入口

- **实验配置**：`scenarios/experiment_poffices.json`。`extra.agent_type: "llm"` 表示默认 LLM 驱动 Block 决策；`agent_provider: "qwen"` 等；`use_llm_query`、`use_llm_task_description` 控制出题。
- **命令**：统一入口 `python run_poffices_agent.py --runs N`（`N=1` 为单轮，`N>1` 为多轮，支持 `--strategy rule|auto|deepen|diversify`）；`--config experiment_poffices_dynamic` 为动态场景（L3 规划 + goal），`--goal "..."` 覆盖 goal；`--no-llm-agent` 强制规则 Agent，`--llm-provider qwen` 覆盖提供商。轨迹与报告统一在 `logs/poffices/`（含 `run_report.html`；单轮自动本轮视图）。

### 链路

实验配置 → `create_poffices_agent(config)` → PofficesLLMAgent / PofficesAgent → Orchestrator 每步 state + last_execution_result → Agent.run() → tool_calls → PofficesRPA.execute → BlockRegistry.execute(block_id, …) → Block.run() → ExecutionResult → B5 → B8 评估与落盘、多轮时 LLM 总结报告。

### Bootstrap 幂等

登录/Agent Master/ Business Office 展开/Market Analysis 选中/Enable Agent Master Mode/Apply 均有「先检测再操作」；开关误关已修复（`_ensure_agent_master_mode_on` 自我纠正）。详见 `raft/rpa/poffices_bootstrap.py`。

### 测试

`pytest tests/test_poffices_agent.py tests/test_poffices_blocks.py tests/test_poffices_llm_agent.py -v`；全量可 `pytest tests/ --ignore=tests/test_poffices_rpa_adapter.py -q`。

---

## 二、近期变更

| 主题 | 说明 |
|------|------|
| **LLM 驱动 Agent** | `raft/agents/poffices_llm_agent.py`：根据 state + last_execution_result 决定调哪个 Block；解析/API 异常时 fallback 到 PofficesAgent。 |
| **配置驱动 Agent 类型** | `raft/agents/factory.py`：`create_poffices_agent(config, cli_...)`，优先级 CLI > 配置 > 默认 rule；`experiment_poffices.json` 的 `extra.agent_type`、`agent_provider`。 |
| **Bootstrap 幂等** | `_is_business_office_expanded`、`_is_apply_needed` 等；已展开/已选中/已开启则跳过。 |
| **Enable Agent Master 开关** | 检测逻辑改为取最后匹配节点再找 switch；`_ensure_agent_master_mode_on` 点击后校验，误关则再点一次纠正。 |

涉及：`raft/agents/poffices_llm_agent.py`、`factory.py`、`raft/rpa/poffices_bootstrap.py`，`scenarios/experiment_poffices.json`，`run_poffices_agent*.py`，`tests/test_poffices_llm_agent.py`。

---

## 三、历史（模块化与测试修复）

- **Poffices 模块化**：契约（ExecutionErrorType、error_type 约定）；`poffices_blocks.py` 中 Block 校验与注册；B7 优先 BlockRegistry，fallback 带可观测标识；`poffices_agent.py` 状态驱动决策；统一入口 `run_poffices_agent.py`（`--runs` 控制轮数）；单测补齐（test_poffices_agent、test_poffices_blocks、test_poffices_rpa_adapter）。
- **B2 路由 3 个失败**：`router.py` 中曾用 suggested_rounds > 1 覆盖 route_type 为 multi_flow；已改为 route_type 仅由 extra/描述/LLM 决定。
- **早期集成用例**：Orchestrator 未传入 mock_rpa，导致评估与预期不符；已改为显式传入 `MockRPA()`。

验证：Poffices 相关 10 passed；曾失败用例 14 passed。
