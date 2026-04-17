# ART 统一 Block API 契约

为便于**移植为 API 到其他平台**，所有 Block（B1–B9）的对外接口采用统一的 API 格式。同一套约定适用于进程内调用与未来 HTTP/gRPC 暴露。

---

## 1. 原则

- **格式统一**：所有 Block 的请求/响应、错误表示、版本信息使用同一套结构。
- **平台无关**：契约只描述数据结构与语义，不绑定传输方式（本地调用 / REST / gRPC 等）。
- **契约先行**：先定契约再实现，建议用 OpenAPI 或 Pydantic 等与代码同源，便于生成文档与多语言客户端。

---

## 2. 请求信封（Block 入参统一包装）

所有对 Block 的调用建议携带以下**元数据**（具体字段名可按实现调整，语义保持一致）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `request_id` | string | 建议 | 调用方生成的请求唯一标识，便于链路追踪与日志关联。 |
| `block_id` | string | 建议 | 被调 Block 标识（如 `B1`, `B6`），便于路由与监控。 |
| `api_version` | string | 建议 | 契约版本，如 `v1`，便于多版本并存与迁移。 |
| `payload` | object | 是 | Block 业务入参，由各 Block 自行定义。 |
| `options` | object | 否 | 可选参数（如超时、语言、调试开关等）。 |

示例（JSON）：

```json
{
  "request_id": "req-550e8400-e29b-41d4-a716-446655440000",
  "block_id": "B1",
  "api_version": "v1",
  "payload": { "config_path": "/scenarios/invoice.json", "task_spec_id": "task-001" },
  "options": { "timeout_sec": 30 }
}
```

---

## 3. 响应信封（Block 出参统一包装）

所有 Block 的返回建议使用统一**响应结构**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `request_id` | string | 建议 | 与请求中的 `request_id` 一致。 |
| `block_id` | string | 建议 | 与请求中的 `block_id` 一致。 |
| `code` | string 或 int | 是 | 业务/传输层结果码，成功时建议固定值（如 `0` 或 `"ok"`）。 |
| `data` | object | 条件 | 成功时放置 Block 业务出参；失败时可为空或省略。 |
| `error` | object | 条件 | 失败时放置统一错误结构（见下）；成功时可为空或省略。 |

示例（成功）：

```json
{
  "request_id": "req-550e8400-e29b-41d4-a716-446655440000",
  "block_id": "B1",
  "code": "ok",
  "data": { "experiment_config": { ... }, "task_spec": { ... } }
}
```

示例（失败）：

```json
{
  "request_id": "req-550e8400-e29b-41d4-a716-446655440000",
  "block_id": "B1",
  "code": "config_not_found",
  "data": null,
  "error": {
    "code": "config_not_found",
    "message": "Experiment config file not found",
    "details": { "path": "/scenarios/invoice.json" }
  }
}
```

---

## 4. 统一错误表示

所有 Block 在返回错误时，`error` 建议采用统一结构，便于其他平台统一解析与展示：

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | string | 机器可读错误码，如 `config_not_found`, `timeout`, `rpa_execution_failed`。 |
| `message` | string | 人类可读简短说明。 |
| `details` | object | 可选，额外上下文（如路径、堆栈、子错误列表等）。 |

错误码建议按 Block 或域划分前缀（如 `B1.xxx`, `B7.xxx`），避免跨 Block 冲突；平台侧可按 `code` 做重试、降级或告警策略。

---

## 5. 版本与兼容

- **api_version**：在请求中携带，用于声明客户端期望的契约版本；服务端可按版本路由或做兼容逻辑。
- **向后兼容**：新增字段使用可选；废弃字段在文档中标明废弃时间与替代方案，避免破坏现有调用方。

---

## 6. 与现有契约的关系

- **ExecutionResult**（B7 产出）、**TaskSpec**、**ExperimentConfig** 等仍按 [实施计划](IMPLEMENTATION_PLAN.md) 中的定义实现；它们作为各 Block **payload / data** 的业务内容，放入上述请求/响应信封即可。
- 进程内调用时，可先实现「逻辑层」按信封格式入参/出参，再在移植为 API 时增加传输层（HTTP/gRPC），无需改 Block 内部逻辑。
- **当前 HTTP 暴露**：B1 `POST /api/v1/b1/load_config`（payload 含 `config_path` / `task_spec_path` / `task_spec_id`）；B8 `POST /api/v1/b8/evaluate`（请求体 JSON：`trajectory`、`task_spec`、可选 `run_id`，返回 RunMetrics）；B9 `POST /api/v1/b9/run`（payload 含 `experiment_config + task_spec` 或文件路径）。详见 [GUIDE.md](GUIDE.md) 服务地址与 API 节。

---

## 7. 当前已实现的业务模型补充

下面补充当前代码里已经落地、且与 API 直接相关的业务字段，尤其是本次新增的 `ScenarioSpec`。

### 7.1 BlockRequest / BlockResponse（实际 Pydantic 模型）

当前代码中的统一信封模型为：

| 模型 | 字段 |
|------|------|
| `BlockRequest` | `request_id`、`block_id`、`api_version`、`payload`、`options` |
| `ApiError` | `code`、`message`、`details` |
| `BlockResponse` | `request_id`、`block_id`、`code`、`data`、`error` |

说明：

- `api_version` 当前默认值为 `v1`
- `code` 当前实际使用字符串值，如 `ok`、`config_not_found`、`task_spec_not_found`
- `error` 为空表示成功

### 7.2 ExperimentConfig（当前已实现）

当前 `ExperimentConfig` 已包含以下场景相关字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `experiment_id` | string | 实验标识 |
| `scenario` | string | 兼容旧配置的场景名称 |
| `scenario_id` | string \| null | 显式场景 ID |
| `scenario_spec_path` | string \| null | 场景规范文件路径 |
| `scenario_spec` | object \| null | 已解析的场景规范对象 |
| `task_spec_ids` | string[] | 本实验包含的任务列表 |
| `extra` | object | 扩展字段 |

### 7.3 ScenarioSpec（当前已实现）

当前 `ScenarioSpec` 已是正式业务对象，可出现在：

- B1 返回的 `experiment_config.scenario_spec`
- B9 直接传入的 `experiment_config.scenario_spec`
- 轨迹日志 / 报告的扩展字段中

其结构如下：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 场景唯一标识 |
| `name` | string | 场景名称 |
| `description` | string | 场景描述 / 默认任务描述来源 |
| `narrative` | string | 供 LLM 使用的场景叙述 |
| `task_spec_ids` | string[] | 本场景允许的任务规范列表 |
| `allowed_agents` | string[] | 本场景允许的 Agent 列表 |
| `suggested_agents` | string[] | 本场景建议优先测试的 Agent 列表 |
| `allowed_blocks` | object[] | 本场景允许的 Block 集合 |
| `flow_template` | object \| null | 推荐流程模板 |
| `constraints` | object \| null | 结构化约束 |
| `extra` | object | 扩展字段 |

其中：

- `flow_template` 当前对应 `ScenarioFlowTemplate`
- `constraints` 当前对应 `ScenarioConstraints`

### 7.4 ScenarioFlowTemplate / ScenarioConstraints

#### ScenarioFlowTemplate

| 字段 | 类型 | 说明 |
|------|------|------|
| `template_id` | string | 模板 ID |
| `description` | string | 模板说明 |
| `steps` | object[] | 推荐步骤列表；每步至少可含 `block_id` / `tool_name` / `params` |
| `extra` | object | 扩展字段 |

#### ScenarioConstraints

| 字段 | 类型 | 说明 |
|------|------|------|
| `required_blocks` | string[] | 必须出现的 Block |
| `forbidden_blocks` | string[] | 禁止使用的 Block |
| `notes` | string[] | 补充约束说明 |
| `extra` | object | 扩展字段 |

---

## 8. B1 / B8 / B9 当前行为说明

### 8.1 B1 `POST /api/v1/b1/load_config`

输入仍走 `BlockRequest` 信封，`payload` 支持：

- `config_path`
- `task_spec_path`
- `task_spec_id`

当前行为：

- 若只传 `config_path`，返回 `experiment_config`
- 若只传 `task_spec_path + task_spec_id`，返回 `task_spec`
- 若同时传，则两者都返回
- 加载 `experiment_config` 时，会自动尝试解析 `scenario_spec`

因此，当前 B1 返回的 `data.experiment_config` 可能已经包含：

- `scenario_id`
- `scenario_spec_path`
- `scenario_spec`

### 8.2 B8 `POST /api/v1/b8/evaluate`

这是当前唯一未使用 `BlockRequest` 信封的 HTTP 接口，直接接收普通 JSON：

```json
{
  "trajectory": [...],
  "task_spec": {...},
  "run_id": "optional"
}
```

返回：

```json
{
  "code": "ok",
  "data": { "...RunMetrics..." },
  "error": null
}
```

### 8.3 B9 `POST /api/v1/b9/run`

当前 `payload` 支持两种模式：

1. 文件加载模式

```json
{
  "config_path": "scenarios/experiment_poffices.json",
  "task_spec_path": "scenarios/task_specs.json",
  "task_spec_id": "task-poffices-query",
  "max_steps": 5
}
```

2. 直接传对象模式

```json
{
  "experiment_config": { "...ExperimentConfig..." },
  "task_spec": { "...TaskSpec..." },
  "max_steps": 5
}
```

当前行为补充：

- 若 `experiment_config` 中已包含 `scenario_spec`，B9 会直接使用
- 若走文件加载模式，B9 会先通过 B1 逻辑解析场景规范
- 若场景规范存在，运行前会校验任务、Agent、Block 是否满足约束
- 违反约束时，当前会返回错误响应；实现上该错误由 `ValueError` 触发，接口层目前统一映射到 `task_spec_not_found`，因此调用方应结合 `error.message` 判断具体原因

---

## 9. 场景规范文件示例

当前推荐把场景能力单独放到 `scenarios/<scenario_id>.json`。例如：

```json
{
  "id": "poffices-agent",
  "description": "在 Poffices 场景下测试指定 Agent 的 Query 能力",
  "task_spec_ids": ["task-poffices-query"],
  "allowed_agents": ["Research Proposal", "Market Analysis"],
  "allowed_blocks": [
    { "block_id": "app_ready", "params": {} },
    { "block_id": "send_query", "params": { "query": "string" } },
    { "block_id": "get_response", "params": {} }
  ],
  "flow_template": {
    "template_id": "default-query-flow",
    "steps": [
      { "block_id": "app_ready", "params": { "options": { "agent_name": "$agent_name" } } },
      { "block_id": "send_query", "params": { "query": "$query" } },
      { "block_id": "get_response", "params": {} }
    ]
  },
  "constraints": {
    "required_blocks": ["app_ready", "send_query", "get_response"],
    "forbidden_blocks": ["poffices_query"]
  }
}
```

---

## 10. 落地建议

- 当前信封模型和核心 HTTP 接口已经落地，后续可继续把 B8 也统一到 `BlockRequest / BlockResponse`。
- 若后续扩展 API，建议把“场景约束错误”从通用 `ValueError` 细分为独立错误码，例如 `scenario_validation_failed`。
- 若需要跨平台对接，建议基于当前 Pydantic 模型生成 OpenAPI，而不是再维护一份手写契约。

按此契约实现后，所有 Block 的 API 格式统一，便于整体移植到其他平台。
