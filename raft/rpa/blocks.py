"""
RPA 流程块（Block）抽象与注册表。
将 RPA 流程拆成可复用的 Block，由配置或 Agent 按条件组合调用，详见 docs/RPA_FLOW_BLOCKS_DESIGN.md。

Block 管理接口（完整）：
  register(block_id, block)   — 按指定 block_id 注册
  register_block(block)       — 便捷注册：自动读取 block.block_id
  unregister(block_id)        — 移除已注册 block，返回是否成功
  get(block_id)               — 按 block_id 获取实例
  list_blocks()               — 返回已注册 block_id 列表
  execute(block_id, ...)      — 查找并执行，不存在返回 None

可选：Block 实现 catalog_entry() -> dict 可被 build_catalog_from_registry 自动提取元数据。
"""
from typing import Any, Protocol

from raft.contracts.models import ExecutionResult


class RPAFlowBlock(Protocol):
    """单个 RPA 流程块协议：给定参数与上下文，执行一段子流程并返回统一 ExecutionResult。"""

    @property
    def block_id(self) -> str:
        """块唯一标识，与 tool_name / 配置中的 block_id 对齐。"""
        ...

    def run(
        self,
        *,
        params: dict[str, Any],
        context: dict[str, Any],
    ) -> ExecutionResult:
        """
        执行本块。
        :param params: 本块所需参数（如 query、username），通常来自 tool_call.params 或 state。
        :param context: 当前上下文（如 state 快照、page/session 引用、step_index 等）。
        :return: 统一 ExecutionResult，供闭环 1 回传。
        """
        ...


class BlockRegistry:
    """
    Block 注册表：block_id → 实现实例。

    管理接口：
      - register(block_id, block)：按指定 id 注册，允许覆盖
      - register_block(block)：便捷注册，自动读取 block.block_id
      - unregister(block_id)：移除 block，返回是否成功
      - get(block_id)：获取实例，不存在返回 None
      - list_blocks()：返回已注册 block_id 列表
      - execute(block_id, ...)：执行，不存在返回 None
    """

    def __init__(self) -> None:
        self._blocks: dict[str, Any] = {}  # block_id -> RPAFlowBlock

    def register(self, block_id: str, block: Any) -> None:
        """按指定 block_id 注册一个 Block（允许覆盖已有注册）。"""
        self._blocks[block_id] = block

    def register_block(self, block: Any) -> None:
        """便捷注册：自动读取 block.block_id 并注册，等价于 register(block.block_id, block)。
        要求 block 具有 block_id 属性；若不存在则抛 AttributeError。
        """
        bid = getattr(block, "block_id", None)
        if not isinstance(bid, str) or not bid.strip():
            raise AttributeError(
                f"register_block 失败：block {block!r} 缺少有效 block_id 属性"
            )
        self._blocks[bid] = block

    def unregister(self, block_id: str) -> bool:
        """移除已注册的 block。
        :returns: True 表示成功移除；False 表示该 block_id 不存在（不抛错）。
        """
        if block_id in self._blocks:
            del self._blocks[block_id]
            return True
        return False

    def get(self, block_id: str) -> Any | None:
        """按 block_id 获取 Block 实例，不存在返回 None。"""
        return self._blocks.get(block_id)

    def list_blocks(self) -> list[str]:
        """返回已注册的 block_id 列表（顺序与注册顺序一致）。"""
        return list(self._blocks.keys())

    def execute(
        self,
        block_id: str,
        *,
        params: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> ExecutionResult | None:
        """若存在该 block 则执行并返回 ExecutionResult，否则返回 None（调用方可回退到原有 if/elif 逻辑）。"""
        block = self.get(block_id)
        if block is None:
            return None
        return block.run(params=params or {}, context=context or {})


# 全局默认注册表，便于 B7 或场景配置统一使用；也可按场景建多个 Registry 实例。
_default_registry: BlockRegistry | None = None


def get_default_block_registry() -> BlockRegistry:
    """获取默认 Block 注册表（懒创建）。"""
    global _default_registry
    if _default_registry is None:
        _default_registry = BlockRegistry()
    return _default_registry
