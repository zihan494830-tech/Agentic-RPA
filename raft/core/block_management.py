"""
Block 管理工具：校验与桥接。

提供两个可选工具函数，不影响主链路：
  validate_catalog_against_registry  — 校验 block_catalog 中的 block_id 是否都已在 registry 注册
  build_catalog_from_registry        — 从 registry 中已注册 Block 生成 catalog 条目列表

使用方式（均为可选，不引入即不影响现有行为）：

  1. 启动时校验：
     from raft.core.block_management import validate_catalog_against_registry
     from raft.rpa.blocks import get_default_block_registry
     missing = validate_catalog_against_registry(block_catalog, get_default_block_registry())
     if missing:
         logger.warning("block_catalog 中有未注册的 block_id: %s", missing)

  2. 生成 catalog 初版（辅助开发）：
     from raft.core.block_management import build_catalog_from_registry
     catalog = build_catalog_from_registry(get_default_block_registry())

  3. 环境变量开关：
     RAFT_VALIDATE_BLOCKS=1  — 在 Orchestrator 启动时自动校验（打 warning，不终止运行）
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from raft.rpa.blocks import BlockRegistry

logger = logging.getLogger(__name__)


def validate_catalog_against_registry(
    block_catalog: list[dict[str, Any]],
    registry: "BlockRegistry",
) -> list[str]:
    """
    校验 block_catalog 中每个 block_id 是否都已在 registry 中注册。

    :param block_catalog: 形如 [{"block_id": "...", ...}, ...] 的条目列表。
    :param registry:      BlockRegistry 实例（如 get_default_block_registry()）。
    :returns:             未注册的 block_id 列表；空列表表示全部通过。

    示例::

        missing = validate_catalog_against_registry(catalog, registry)
        if missing:
            print(f"以下 block 在 catalog 中存在但未注册：{missing}")
    """
    registered = set(registry.list_blocks())
    missing: list[str] = []
    for item in block_catalog:
        if not isinstance(item, dict):
            continue
        bid = item.get("block_id")
        if isinstance(bid, str) and bid.strip() and bid not in registered:
            missing.append(bid)
    return missing


def build_catalog_from_registry(
    registry: "BlockRegistry",
    block_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    从 registry 中已注册的 Block 生成 block_catalog 条目列表。

    每个条目至少包含 block_id、params、description。
    若 Block 实现了 ``catalog_entry() -> dict`` 方法，则直接使用其返回值；
    否则生成最小格式 ``{"block_id": ..., "params": {}, "description": ""}``.

    :param registry:   BlockRegistry 实例。
    :param block_ids:  指定要导出的 block_id 列表；为 None 时导出全部。
    :returns:          catalog 条目列表，可直接用作 block_catalog 或 allowed_blocks 的初版。

    示例::

        catalog = build_catalog_from_registry(get_default_block_registry())
        # 可写入 JSON 或作为 allowed_blocks 初版
    """
    ids_to_export = block_ids if isinstance(block_ids, list) else registry.list_blocks()
    result: list[dict[str, Any]] = []
    for bid in ids_to_export:
        block = registry.get(bid)
        if block is None:
            continue
        # 优先使用 Block 自身声明的元数据
        entry_fn = getattr(block, "catalog_entry", None)
        if callable(entry_fn):
            try:
                entry = dict(entry_fn())
                entry.setdefault("block_id", bid)
                result.append(entry)
                continue
            except Exception:
                pass
        # 最小格式兜底
        result.append({"block_id": bid, "params": {}, "description": ""})
    return result


def run_startup_validation(
    block_catalog: list[dict[str, Any]],
    registry: "BlockRegistry",
    *,
    abort_on_missing: bool = False,
) -> None:
    """
    启动时可选校验入口：若环境变量 RAFT_VALIDATE_BLOCKS=1 或 abort_on_missing=True 时执行校验。

    - 默认：只打 warning，不中断程序。
    - abort_on_missing=True：有未注册 block 时抛 RuntimeError。

    :param block_catalog:     待校验的 catalog 列表。
    :param registry:          BlockRegistry 实例。
    :param abort_on_missing:  True 时若有缺失则抛 RuntimeError。
    """
    if not (abort_on_missing or os.environ.get("RAFT_VALIDATE_BLOCKS") == "1"):
        return

    missing = validate_catalog_against_registry(block_catalog, registry)
    if not missing:
        logger.debug("[BlockManagement] block_catalog 与 registry 一致，共 %d 个 block", len(registry.list_blocks()))
        return

    msg = f"[BlockManagement] block_catalog 中以下 block_id 未在 registry 注册：{missing}"
    if abort_on_missing:
        raise RuntimeError(msg)
    logger.warning(msg)
