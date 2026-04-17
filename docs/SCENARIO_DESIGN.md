# Scenario 设计：已从标签升级为场景规范

本文档说明 `scenario` 在当前代码中的实现状态。现在它不再只是实验里的字符串标签，而是已经升格为可加载、可校验、可驱动规划与出题的 `ScenarioSpec`。

---

## 1. 当前实现概览

### 1.1 数据模型

当前 `ExperimentConfig` 已新增以下场景相关字段：

- `scenario`：兼容旧配置的场景名称字符串。
- `scenario_id`：显式场景 ID。
- `scenario_spec_path`：场景规范文件路径。
- `scenario_spec`：解析后的 `ScenarioSpec` 对象。

当前 `ScenarioSpec` 已支持以下结构：

| 字段 | 作用 |
|------|------|
| `id` / `name` | 场景唯一标识与展示名称 |
| `description` / `narrative` | 场景描述；用于任务描述、query prompt、报告展示 |
| `task_spec_ids` | 本场景允许的任务规范列表 |
| `allowed_agents` | 本场景允许的 Agent 列表 |
| `suggested_agents` | 本场景建议优先测试的 Agent 列表 |
| `allowed_blocks` | 本场景允许使用的 Block 集合 |
| `flow_template` | 推荐流程模板，当前为步骤序列模板 |
| `constraints` | 结构化约束，当前支持 `required_blocks` / `forbidden_blocks` / `notes` |

### 1.2 场景规范加载方式

当前加载优先级如下：

1. `ExperimentConfig.scenario_spec` 内联对象
2. `ExperimentConfig.scenario_spec_path`
3. `scenario_id` 或 `scenario` 对应的同目录 `scenarios/<id>.json`

也就是说，旧配置仍可只保留 `scenario: "poffices-agent"`，但如果同目录存在 `poffices-agent.json`，B1 加载时会自动解析并补全 `scenario_spec`。

### 1.3 示例

当前仓库已提供独立场景规范文件：

- `scenarios/poffices-agent.json`

对应实验配置：

- `scenarios/experiment_poffices.json`

其中 `experiment_poffices.json` 已通过 `scenario_spec_path` 引用 `poffices-agent.json`，原先散落在 `extra` 里的 `block_catalog` / `available_agents` 已迁到场景规范中。

---

## 2. 当前已接入的联动点

`ScenarioSpec` 已经进入运行主链路，而不只是报告标签。

### 2.1 B1 配置加载

`load_experiment_config()` 现在会在解析实验配置时自动尝试解析场景规范，并将以下字段写回 `ExperimentConfig`：

- `scenario_id`
- `scenario_spec_path`
- `scenario_spec`

因此，B1 返回的 `experiment_config` 已经可以直接包含完整场景规范。

### 2.2 任务描述生成

`Orchestrator._get_initial_state_for_run()` 在生成任务描述时，会优先把场景规范展开为 `scenario_prompt`，内容包括：

- 场景 ID / 名称
- 场景描述
- 允许的 Agent
- 允许的 Block
- 流程模板步骤
- 必须包含 / 禁止使用的约束

若 `TaskSpec.description` 为空，还会优先使用 `ScenarioSpec.description` 或 `narrative` 作为默认任务描述。

### 2.3 Query 建议器

`query_suggester` 现在已支持 `scenario_context`，并接入以下路径：

- 单轮 query 生成
- 多轮 query 生成
- 带 `query_rationale` 的多轮出题
- 多 Agent query 批量生成

这意味着 query 不再只依赖 `TaskSpec.description`，而是显式受场景规范约束。

### 2.4 Agent 选择

当前 Agent 相关逻辑已优先读取场景规范：

- `resolve_allowed_agents()`：优先用 `ScenarioSpec.allowed_agents`
- `resolve_suggested_agents()`：优先用 `ScenarioSpec.suggested_agents`，其次退回 `allowed_agents`
- `resolve_agent_under_test()`：若实验未显式指定 `agent_under_test`，可从 `suggested_agents` 推导默认待测 Agent

### 2.5 Block 集合与流程规划

`goal_driven` 模式下，规划器已优先消费场景规范：

- `allowed_blocks`：作为主 `block_catalog`
- `flow_template`：可直接生成默认规则计划
- `constraints`：会传入 LLM planner prompt，并用于执行前校验

当前规则规划器支持直接从 `flow_template.steps` 生成默认线性计划；模板中的 `$query`、`$agent_name` 会在运行时替换为实际值。

### 2.6 运行前约束校验

当前已实现 `validate_scenario_run()`，在 run 开始前校验：

- `task_spec.task_spec_id` 是否在 `ScenarioSpec.task_spec_ids` 中
- `agent_under_test` / `agents_to_test` 是否都属于 `allowed_agents`
- `required_blocks` 是否都在当前 Block 集合中
- `forbidden_blocks` 是否被错误配置进当前 Block 集合

若不满足，会直接抛出 `ValueError`，阻止 run 继续执行。

### 2.7 轨迹与报告

当前场景信息已进入落盘与报告链路：

- 单轮轨迹 `extra` 中会写入 `scenario`、`scenario_id`
- 若存在场景规范，还会写入完整 `scenario_spec`
- HTML 报告中会显式展示 `ScenarioSpec` 摘要，包括允许 Agent、允许 Block、流程模板和约束

---

## 3. 当前兼容策略

当前实现仍保持对旧配置的兼容。

### 3.1 保持兼容的点

- 仍保留 `ExperimentConfig.scenario` 字段
- 若没有场景规范文件，系统仍可运行
- `extra.block_catalog` 与 `extra.available_agents` 仍可作为兜底来源

### 3.2 当前优先级

在已接入场景规范的逻辑中，优先级一般为：

1. `ScenarioSpec`
2. `ExperimentConfig.extra`
3. 代码中的默认兜底

因此，当前推荐把与“场景能力”相关的配置尽量收敛到 `ScenarioSpec`，把 `extra` 留给 run 级开关和临时覆盖项。

---

## 4. 当前边界与未完成项

虽然 `scenario` 已经升格为“场景规范”，但仍有一些边界是当前实现刻意保留的：

- `allowed_blocks` 目前是具体 Block 列表，不是按 catalog 引用，也没有 capability-level 语义抽象。
- `flow_template` 当前主要服务于线性默认计划，还没有升级为更通用的 DAG / schema 约束语言。
- `constraints` 当前只实现了 `required_blocks` / `forbidden_blocks` 两类硬校验，尚未扩展到更复杂的顺序、依赖或语义规则。
- CLI 传入的 `--agent` 仍可显式覆盖默认值；若与 `allowed_agents` 冲突，会在 run 前校验阶段报错。

---

## 5. 小结

当前代码状态下，`scenario` 已不再只是“给 LLM 的一句话标签”，而是：

- 可独立落盘的配置文件
- `ExperimentConfig` 中的一等业务对象
- 任务描述、query、Agent、Block、流程模板、执行约束的共同来源

这意味着后续若继续扩展“自主建 Block”或“更复杂的新流程生成”，已经有一套可复用的场景规范入口，不需要再从纯字符串标签重新起步。
