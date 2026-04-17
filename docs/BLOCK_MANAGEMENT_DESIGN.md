# Block 管理标准化方案

目标：使 Block 管理更标准化、规范化，增删改更灵活，整体更稳定，**且不影响现有功能**。

---

## 1. 现状与问题

### 1.1 当前结构

| 层 | 职责 | 来源 |
|----|------|------|
| **BlockRegistry** | 运行时执行：block_id → Block 实例 | `raft/rpa/blocks.py`，`register_poffices_blocks()` 注册 |
| **block_catalog** | Planner/Agent 元数据：block_id、params、description | `ScenarioSpec.allowed_blocks` 或 `extra.block_catalog` |

### 1.2 问题

- **添加**：需改代码（`register_poffices_blocks`）+ 配置（`allowed_blocks`），两处易不同步
- **删除**：Registry 无 `unregister`；catalog 只能手动改 JSON
- **其他项目接入**：无统一契约与校验，易出错

---

## 2. 设计原则

1. **向后兼容**：不改现有调用路径，`register()`、`execute()`、`resolve_block_catalog()` 行为不变
2. **增量增强**：新能力为可选扩展，旧代码可继续按原方式工作
3. **契约清晰**：定义 Block 最小格式与接入流程，便于外部项目对接

---

## 3. 方案概览

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         Block 管理分层                                   │
├─────────────────────────────────────────────────────────────────────────┤
│  Layer 1: BlockRegistry（执行层）                                        │
│  - register / unregister / register_block / list_blocks / execute       │
│  - 新增：unregister、register_block（便捷）、可选 metadata 存储           │
├─────────────────────────────────────────────────────────────────────────┤
│  Layer 2: BlockCatalog（配置层）                                         │
│  - 来源：ScenarioSpec.allowed_blocks / extra.block_catalog               │
│  - 不变：resolve_block_catalog() 返回格式保持                            │
├─────────────────────────────────────────────────────────────────────────┤
│  Layer 3: 校验与桥接（可选）                                              │
│  - validate_catalog_against_registry()：启动时校验 catalog ⊆ registry    │
│  - build_catalog_from_registry()：从已注册 Block 生成 catalog 条目       │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 4. 具体改动（均向后兼容）

### 4.1 BlockRegistry 增强（`raft/rpa/blocks.py`）

**新增方法（不改现有方法）：**

| 方法 | 说明 |
|------|------|
| `unregister(block_id: str) -> bool` | 移除 block，返回是否成功；不存在时返回 False，不抛错 |
| `register_block(block: RPAFlowBlock) -> None` | 便捷注册：用 `block.block_id` 作为 key，等价于 `register(block.block_id, block)` |

**可选扩展（后续可加）：**

- `get_metadata(block_id) -> dict | None`：若 Block 实现 `catalog_entry` 属性，可返回其元数据，用于自动生成 catalog

**兼容性：**

- `register()`、`get()`、`list_blocks()`、`execute()` 签名与行为不变
- `register_poffices_blocks()` 可继续用 `reg.register("id", block)`，也可改用 `reg.register_block(block)`

---

### 4.2 Block 元数据协议（可选扩展）

为支持「从 Block 自动生成 catalog 条目」，定义可选协议：

```python
# 可选：Block 若实现此方法，可被 build_catalog_from_registry 使用
def catalog_entry(self) -> dict[str, Any]:
    """返回 block_catalog 所需条目：block_id、params、description。"""
    return {"block_id": self.block_id, "params": {}, "description": "..."}
```

- 不实现：不影响现有 Block，不强制修改
- 实现：可用于自动同步 catalog，减少手写配置

---

### 4.3 校验与桥接模块（新建 `raft/core/block_management.py`）

**职责：** 提供可选校验与工具函数，不改变主链路。

| 函数 | 说明 |
|------|------|
| `validate_catalog_against_registry(catalog, registry) -> list[str]` | 校验 catalog 中每个 block_id 在 registry 中已注册；返回未注册的 block_id 列表；空列表表示通过 |
| `build_catalog_from_registry(registry, block_ids=None) -> list[dict]` | 从 registry 中已注册 Block 生成 catalog 条目；若 Block 有 `catalog_entry` 则用其，否则用最小格式 `{block_id, params: {}, description: ""}`；`block_ids` 为 None 时取全部 |

**使用方式：**

- 启动时（可选）：在 Orchestrator 或 run 入口调用 `validate_catalog_against_registry`，若有未注册 block 则打 warning 或按配置 abort
- 开发时：用 `build_catalog_from_registry` 辅助生成 `allowed_blocks` 初版，再手工补充 description

---

### 4.4 配置与解析（保持不变）

- `resolve_block_catalog(config)`：逻辑不变，仍从 `ScenarioSpec.allowed_blocks` 或 `extra.block_catalog` 解析
- `ScenarioSpec.allowed_blocks`、`extra.block_catalog` 格式不变

---

## 5. Block 接入契约（文档化）

### 5.1 最小 block_catalog 条目格式

```json
{
  "block_id": "string，唯一标识，与 Registry 中注册名一致",
  "params": "可选，dict，描述参数 schema",
  "description": "可选，string，供 Planner 理解用途"
}
```

### 5.2 添加 Block 标准流程

1. **实现 Block 类**：满足 `RPAFlowBlock` 协议（`block_id` 属性 + `run(params, context) -> ExecutionResult`）
2. **注册到 Registry**：`registry.register(block_id, block)` 或 `registry.register_block(block)`
3. **加入 block_catalog**：在 `ScenarioSpec.allowed_blocks` 或 `extra.block_catalog` 中增加对应条目
4. **（可选）校验**：启动时调用 `validate_catalog_against_registry` 确保 catalog 与 registry 一致

### 5.3 删除 Block 标准流程

1. **从 block_catalog 移除**：在场景 JSON 或 `extra.block_catalog` 中删除该 block 条目
2. **（可选）从 Registry 移除**：`registry.unregister(block_id)`，避免残留可执行入口

### 5.4 其他项目接入

1. 实现自己的 Block 类（满足 `RPAFlowBlock`）
2. 在项目初始化时调用 `get_default_block_registry().register_block(block)` 或 `register(block_id, block)`
3. 在实验配置中提供 `block_catalog`（或通过 `scenario_spec_path` 引用含 `allowed_blocks` 的场景）
4. （可选）使用 `validate_catalog_against_registry` 做启动校验

---

## 6. 实施步骤（分阶段，零影响）

### Phase A：Registry 增强（低风险）

1. 在 `BlockRegistry` 中新增 `unregister(block_id)` 和 `register_block(block)`
2. 补充单测
3. 不修改 `register_poffices_blocks()` 和任何调用方

### Phase B：校验与桥接（可选模块）

1. 新建 `raft/core/block_management.py`，实现 `validate_catalog_against_registry`、`build_catalog_from_registry`
2. 在 `run_poffices_agent.py` 或 Orchestrator 中增加**可选**启动校验（如环境变量 `RAFT_VALIDATE_BLOCKS=1` 时启用）
3. 默认不启用，不影响现有运行

### Phase C：文档与契约

1. 在 `docs/RPA_BLOCKS.md` 或新建 `docs/BLOCK_ADAPTER_GUIDE.md` 中补充：
   - 最小 block_catalog 格式
   - 添加/删除标准流程
   - 其他项目接入示例
2. 在 `BLOCK_MANAGEMENT_DESIGN.md`（本文档）中记录设计决策

### Phase D（可选）：Block 自动生成 catalog

1. 为部分 Block 实现 `catalog_entry` 方法
2. 提供脚本或工具，从 registry 生成 `allowed_blocks` 初版
3. 不改变现有场景配置的加载逻辑

---

## 7. 兼容性检查清单

| 检查项 | 说明 |
|--------|------|
| `BlockRegistry.register()` | 不变 |
| `BlockRegistry.get()` | 不变 |
| `BlockRegistry.list_blocks()` | 不变 |
| `BlockRegistry.execute()` | 不变 |
| `get_default_block_registry()` | 不变 |
| `register_poffices_blocks()` | 不变，可选择性改用 `register_block` |
| `resolve_block_catalog()` | 不变 |
| `poffices_rpa.execute()` | 不变，仍通过 `get_default_block_registry().execute()` |
| 场景 JSON 格式 | 不变 |
| 现有测试 | 全部通过 |

---

## 8. 总结

- **标准化**：通过契约文档与校验函数统一添加/删除流程
- **规范化**：BlockRegistry 增加 unregister、register_block，补齐管理能力
- **灵活性**：其他项目可按契约接入，可选校验降低配置错误
- **稳定性**：所有改动为增量、可选，现有功能不受影响
