"""BlockRegistry 增强接口 + block_management 工具函数单测。"""
from __future__ import annotations

import pytest

from raft.contracts.models import ExecutionResult
from raft.core.block_management import (
    build_catalog_from_registry,
    run_startup_validation,
    validate_catalog_against_registry,
)
from raft.rpa.blocks import BlockRegistry


# ---------------------------------------------------------------------------
# 测试辅助：最小 Block 实现
# ---------------------------------------------------------------------------

class _MinimalBlock:
    """最简 Block：只有 block_id 和 run。"""

    def __init__(self, block_id: str) -> None:
        self._block_id = block_id

    @property
    def block_id(self) -> str:
        return self._block_id

    def run(self, *, params: dict, context: dict) -> ExecutionResult:
        return ExecutionResult(success=True, error_type=None, raw_response={"block_id": self._block_id})


class _BlockWithCatalogEntry(_MinimalBlock):
    """带 catalog_entry 方法的 Block，用于验证 build_catalog_from_registry 能提取元数据。"""

    def catalog_entry(self) -> dict:
        return {
            "block_id": self._block_id,
            "params": {"query": "string"},
            "description": f"{self._block_id} 的描述",
        }


class _BlockWithBadCatalogEntry(_MinimalBlock):
    """catalog_entry 抛异常时，应降级为最小格式。"""

    def catalog_entry(self) -> dict:
        raise RuntimeError("故意抛错")


# ---------------------------------------------------------------------------
# BlockRegistry：register_block
# ---------------------------------------------------------------------------

class TestRegisterBlock:

    def test_register_block_uses_block_id(self) -> None:
        reg = BlockRegistry()
        block = _MinimalBlock("my_block")
        reg.register_block(block)
        assert reg.get("my_block") is block

    def test_register_block_overwrites_existing(self) -> None:
        reg = BlockRegistry()
        b1 = _MinimalBlock("foo")
        b2 = _MinimalBlock("foo")
        reg.register_block(b1)
        reg.register_block(b2)
        assert reg.get("foo") is b2

    def test_register_block_raises_when_no_block_id(self) -> None:
        reg = BlockRegistry()

        class _NoId:
            pass

        with pytest.raises(AttributeError, match="block_id"):
            reg.register_block(_NoId())

    def test_register_block_raises_when_block_id_empty(self) -> None:
        reg = BlockRegistry()
        block = _MinimalBlock("")
        with pytest.raises(AttributeError, match="block_id"):
            reg.register_block(block)


# ---------------------------------------------------------------------------
# BlockRegistry：unregister
# ---------------------------------------------------------------------------

class TestUnregister:

    def test_unregister_existing_returns_true(self) -> None:
        reg = BlockRegistry()
        reg.register("a", _MinimalBlock("a"))
        assert reg.unregister("a") is True
        assert reg.get("a") is None

    def test_unregister_nonexistent_returns_false(self) -> None:
        reg = BlockRegistry()
        assert reg.unregister("not_exist") is False

    def test_unregister_does_not_affect_others(self) -> None:
        reg = BlockRegistry()
        reg.register("x", _MinimalBlock("x"))
        reg.register("y", _MinimalBlock("y"))
        reg.unregister("x")
        assert reg.get("y") is not None
        assert "x" not in reg.list_blocks()

    def test_unregister_block_no_longer_executable(self) -> None:
        reg = BlockRegistry()
        reg.register("z", _MinimalBlock("z"))
        reg.unregister("z")
        result = reg.execute("z")
        assert result is None


# ---------------------------------------------------------------------------
# 原有接口向后兼容
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:

    def test_register_and_execute_unchanged(self) -> None:
        reg = BlockRegistry()
        block = _MinimalBlock("orig")
        reg.register("orig", block)
        result = reg.execute("orig")
        assert result is not None
        assert result.success is True

    def test_list_blocks_unchanged(self) -> None:
        reg = BlockRegistry()
        reg.register("p", _MinimalBlock("p"))
        reg.register("q", _MinimalBlock("q"))
        assert set(reg.list_blocks()) == {"p", "q"}

    def test_execute_missing_returns_none(self) -> None:
        reg = BlockRegistry()
        assert reg.execute("not_here") is None


# ---------------------------------------------------------------------------
# validate_catalog_against_registry
# ---------------------------------------------------------------------------

class TestValidateCatalog:

    def _make_reg(self, *ids: str) -> BlockRegistry:
        reg = BlockRegistry()
        for bid in ids:
            reg.register(bid, _MinimalBlock(bid))
        return reg

    def test_all_registered_returns_empty(self) -> None:
        reg = self._make_reg("a", "b")
        catalog = [{"block_id": "a"}, {"block_id": "b"}]
        assert validate_catalog_against_registry(catalog, reg) == []

    def test_missing_block_returned(self) -> None:
        reg = self._make_reg("a")
        catalog = [{"block_id": "a"}, {"block_id": "missing"}]
        assert validate_catalog_against_registry(catalog, reg) == ["missing"]

    def test_empty_catalog_returns_empty(self) -> None:
        reg = self._make_reg("a")
        assert validate_catalog_against_registry([], reg) == []

    def test_non_dict_items_ignored(self) -> None:
        reg = self._make_reg("a")
        catalog = [{"block_id": "a"}, "not_a_dict", 42]  # type: ignore[list-item]
        assert validate_catalog_against_registry(catalog, reg) == []

    def test_item_without_block_id_ignored(self) -> None:
        reg = self._make_reg("a")
        catalog = [{"block_id": "a"}, {"description": "no id"}]
        assert validate_catalog_against_registry(catalog, reg) == []

    def test_multiple_missing_all_returned(self) -> None:
        reg = self._make_reg("a")
        catalog = [{"block_id": "missing1"}, {"block_id": "missing2"}]
        result = validate_catalog_against_registry(catalog, reg)
        assert set(result) == {"missing1", "missing2"}


# ---------------------------------------------------------------------------
# build_catalog_from_registry
# ---------------------------------------------------------------------------

class TestBuildCatalog:

    def test_minimal_block_gets_minimal_entry(self) -> None:
        reg = BlockRegistry()
        reg.register("simple", _MinimalBlock("simple"))
        catalog = build_catalog_from_registry(reg)
        assert len(catalog) == 1
        assert catalog[0]["block_id"] == "simple"
        assert "params" in catalog[0]
        assert "description" in catalog[0]

    def test_block_with_catalog_entry_used(self) -> None:
        reg = BlockRegistry()
        reg.register("rich", _BlockWithCatalogEntry("rich"))
        catalog = build_catalog_from_registry(reg)
        assert catalog[0]["description"] == "rich 的描述"
        assert catalog[0]["params"] == {"query": "string"}

    def test_bad_catalog_entry_falls_back_to_minimal(self) -> None:
        reg = BlockRegistry()
        reg.register("bad", _BlockWithBadCatalogEntry("bad"))
        catalog = build_catalog_from_registry(reg)
        assert catalog[0]["block_id"] == "bad"
        assert catalog[0]["params"] == {}

    def test_block_ids_filter(self) -> None:
        reg = BlockRegistry()
        reg.register("a", _MinimalBlock("a"))
        reg.register("b", _MinimalBlock("b"))
        reg.register("c", _MinimalBlock("c"))
        catalog = build_catalog_from_registry(reg, block_ids=["a", "c"])
        ids = [e["block_id"] for e in catalog]
        assert set(ids) == {"a", "c"}

    def test_nonexistent_id_in_filter_skipped(self) -> None:
        reg = BlockRegistry()
        reg.register("a", _MinimalBlock("a"))
        catalog = build_catalog_from_registry(reg, block_ids=["a", "ghost"])
        assert len(catalog) == 1
        assert catalog[0]["block_id"] == "a"

    def test_empty_registry_returns_empty(self) -> None:
        reg = BlockRegistry()
        assert build_catalog_from_registry(reg) == []


# ---------------------------------------------------------------------------
# run_startup_validation
# ---------------------------------------------------------------------------

class TestRunStartupValidation:

    def _make_reg(self, *ids: str) -> BlockRegistry:
        reg = BlockRegistry()
        for bid in ids:
            reg.register(bid, _MinimalBlock(bid))
        return reg

    def test_no_env_no_abort_does_nothing(self) -> None:
        """默认不启用，即便有缺失也不报错。"""
        reg = self._make_reg("a")
        catalog = [{"block_id": "missing"}]
        run_startup_validation(catalog, reg)  # 不应抛错

    def test_abort_on_missing_raises(self) -> None:
        reg = self._make_reg("a")
        catalog = [{"block_id": "missing"}]
        with pytest.raises(RuntimeError, match="missing"):
            run_startup_validation(catalog, reg, abort_on_missing=True)

    def test_abort_on_missing_no_error_when_all_registered(self) -> None:
        reg = self._make_reg("a", "b")
        catalog = [{"block_id": "a"}, {"block_id": "b"}]
        run_startup_validation(catalog, reg, abort_on_missing=True)  # 不应抛错

    def test_env_var_triggers_warning(self, monkeypatch, caplog) -> None:
        import logging
        monkeypatch.setenv("RAFT_VALIDATE_BLOCKS", "1")
        reg = self._make_reg("a")
        catalog = [{"block_id": "missing_x"}]
        with caplog.at_level(logging.WARNING, logger="raft.core.block_management"):
            run_startup_validation(catalog, reg)
        assert "missing_x" in caplog.text
