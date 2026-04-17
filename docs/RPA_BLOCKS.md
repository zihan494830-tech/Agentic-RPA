# RPA Block 设计与管理

本文档合并：RPA 流程块化概念、谁调谁、协议与 Poffices 契约、通用 Block 语义、代码位置、Block 管理接口与接入契约。

---

## 1. 概念与谁调谁

**以前**：登录、选 Agent、填 query、取结果等都写在一个 `execute()` 里，用一堆 `if tool_name == "xxx"`。  
**现在**：每段操作拆成一块 **Block**（有 `block_id` 和 `run(params, context)`），由 **BlockRegistry** 按名字查找并执行；B7 只负责「按名字调 run」，不再写死分支。

**数据流**：B9 每步问 B6 → B6 返回 `tool_name` + `params` → B9 调 B7.execute → B7 用 `tool_name` 查 BlockRegistry 执行 Block.run() → ExecutionResult → B5 记轨迹并合并 state → 下一步 B6 再决策。

**对比**：以前加新步骤要改 B7 加分支；现在新写 Block 类并注册即可，B7 不改。每块可单独测、复用。

---

## 2. 协议与 Poffices 契约

- **协议**：`raft/rpa/blocks.py` — `RPAFlowBlock`（需 `block_id`、`run(params, context)` → `ExecutionResult`）、`BlockRegistry`（完整管理接口见第 4 节）、`get_default_block_registry()`。
- **B7 使用**：`raft/rpa/poffices_rpa.py` 的 `execute()` 内先 `BlockRegistry.execute(tool_name, ...)`，有结果即返回，否则走兜底 if/elif。
- **Poffices 已落地**：`poffices_bootstrap.params = {}`；`poffices_query.params = {"query": str}`；空 query 或缺失返回 `validation_error`。`ExecutionResult.error_type` 推荐：`timeout`、`rpa_execution_failed`、`validation_error`、`unknown_tool` 等。
- **编排方式**：当前以 **Agent 驱动** 为主（tool_name = block_id）；可扩展为配置驱动（场景里定义 block 序列/DAG）或条件分支（边上 when）。

---

## 3. 通用 Block 语义（与站点解耦）

为支持「换测 Poffices 上其他 Agent」而不改流程，可采用与站点无关的 block_id：

| 通用 block_id | 语义 | 参数 |
|---------------|------|------|
| `app_ready` | 打开应用并进入可操作状态（登录、选 Agent 等） | `options?: { agent_name?: string }` |
| `send_query` | 在已就绪会话中发送一条查询并触发执行 | `query: string`（必填）, `options?` |
| `get_response` | 等待当前任务完成并取回结果 | `options?` |

Poffices 实现上述三块并注册；`app_ready` 支持 `options.agent_name`（默认 "Market Analysis"）。可保留 `poffices_bootstrap` / `poffices_query` 为别名。B6 的 Block 列表可从配置 `block_catalog` 与 `available_agents` 读取，不再写死 Poffices/Market Analysis。

---

## 4. BlockRegistry 管理接口

`BlockRegistry`（`raft/rpa/blocks.py`）提供完整增删查执行接口：

| 方法 | 说明 |
|------|------|
| `register(block_id, block)` | 按指定 id 注册，允许覆盖 |
| `register_block(block)` | 便捷注册：自动读取 `block.block_id`，等价于 `register(block.block_id, block)` |
| `unregister(block_id) -> bool` | 移除已注册 block；返回 True 表示成功，False 表示 id 不存在（不抛错） |
| `get(block_id) -> block | None` | 获取实例，不存在返回 None |
| `list_blocks() -> list[str]` | 返回已注册 block_id 列表 |
| `execute(block_id, ...)` | 查找并执行，不存在返回 None |

**可选元数据协议**：Block 可实现 `catalog_entry() -> dict` 方法，返回 `block_catalog` 所需条目（至少含 `block_id`、`params`、`description`），便于 `build_catalog_from_registry` 自动提取。

---

## 5. 校验与桥接工具（可选）

`raft/core/block_management.py` 提供两个可选工具函数，**不引入即不影响主链路**：

### 5.1 validate_catalog_against_registry

校验 `block_catalog` 中每个 `block_id` 是否都已在 registry 注册：

```python
from raft.core.block_management import validate_catalog_against_registry
from raft.rpa.blocks import get_default_block_registry

missing = validate_catalog_against_registry(block_catalog, get_default_block_registry())
if missing:
    print(f"以下 block_id 在 catalog 中声明但未注册：{missing}")
```

### 5.2 build_catalog_from_registry

从 registry 中已注册 Block 生成 `block_catalog` 条目列表，辅助初版配置编写：

```python
from raft.core.block_management import build_catalog_from_registry
from raft.rpa.blocks import get_default_block_registry

catalog = build_catalog_from_registry(get_default_block_registry())
# 可直接用作 allowed_blocks 初版，再补充 description
```

### 5.3 run_startup_validation（启动时校验）

在启动脚本或 Orchestrator 中可选调用，设置环境变量 `RAFT_VALIDATE_BLOCKS=1` 时自动打印 warning：

```python
from raft.core.block_management import run_startup_validation
run_startup_validation(block_catalog, registry)  # 默认 warning；abort_on_missing=True 时抛 RuntimeError
```

---

## 6. Block 接入契约

### 6.1 最小 block_catalog 条目格式

```json
{
  "block_id": "唯一标识，与 Registry 中注册名一致",
  "params": {},
  "description": "供 Planner 理解用途的一句话"
}
```

`params` 和 `description` 为可选；缺失时 Planner 仍可工作，但规划质量依赖 block_id 的语义清晰度。

### 6.2 添加 Block 标准流程

1. **实现 Block 类**：满足 `RPAFlowBlock` 协议（`block_id` 属性 + `run(params, context) -> ExecutionResult`）
2. **注册**：`registry.register_block(block)` 或 `registry.register("block_id", block)`
3. **加入 block_catalog**：在 `ScenarioSpec.allowed_blocks` 或 `extra.block_catalog` 中增加对应条目
4. **（可选）校验**：调用 `validate_catalog_against_registry` 确认一致
5. **补单测**：验证 block 在 mock 上下文下的输出符合预期

### 6.3 删除 Block 标准流程

1. **从 block_catalog 移除**：在场景 JSON 的 `allowed_blocks` 或 `extra.block_catalog` 中删除该条目
2. **（可选）从 Registry 移除**：`registry.unregister(block_id)`，避免残留可执行入口

### 6.4 其他项目接入

1. 实现 Block 类（满足 `RPAFlowBlock`）
2. 在项目初始化时注册：`get_default_block_registry().register_block(block)`
3. 在实验配置中提供 `block_catalog`（或 `scenario_spec_path` 引用含 `allowed_blocks` 的场景文件）
4. （可选）调用 `run_startup_validation` 做启动校验

---

## 7. 代码位置

| 文件 | 职责 |
|------|------|
| `raft/rpa/blocks.py` | `RPAFlowBlock` 协议、`BlockRegistry`（完整管理接口）、`get_default_block_registry()` |
| `raft/rpa/poffices_blocks.py` | Poffices Block 实现与注册（`register_poffices_blocks()`） |
| `raft/rpa/poffices_rpa.py` | B7：`execute()` 内优先走 BlockRegistry |
| `raft/core/block_management.py` | 可选工具：校验与 catalog 生成 |
| `docs/BLOCK_MANAGEMENT_DESIGN.md` | Block 管理标准化方案设计文档 |

新增 Block：在对应 `*_blocks.py` 实现并注册；在场景 JSON 中补 `allowed_blocks` 条目；补单测。
