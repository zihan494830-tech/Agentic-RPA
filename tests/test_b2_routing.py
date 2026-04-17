"""B2 单测：难度与 route_type 边界、路由分支。"""
import pytest

from raft.contracts.models import TaskSpec
from raft.core.difficulty import route


def test_route_extra_route_type_single_flow() -> None:
    """extra 中指定 route_type 为 single_flow 时使用其值。"""
    task = TaskSpec(
        task_spec_id="t1",
        description="Any",
        initial_state={},
        extra={"route_type": "single_flow"},
    )
    r = route(task)
    assert r.route_type == "single_flow"


def test_route_extra_route_type_multi_flow() -> None:
    """extra 中指定 route_type 为 multi_flow 时使用其值。"""
    task = TaskSpec(
        task_spec_id="t1",
        description="Any",
        initial_state={},
        extra={"route_type": "multi_flow"},
    )
    r = route(task)
    assert r.route_type == "multi_flow"


def test_route_rule_by_description_multi_flow() -> None:
    """描述含「多步」「分支」等关键词时规则选 multi_flow。"""
    task = TaskSpec(
        task_spec_id="t1",
        description="任务包含多步与分支",
        initial_state={},
    )
    r = route(task)
    assert r.route_type == "multi_flow"


def test_route_rule_by_description_single_flow() -> None:
    """描述不含多步/分支时规则选 single_flow。"""
    task = TaskSpec(
        task_spec_id="t1",
        description="简单线性任务",
        initial_state={},
    )
    r = route(task)
    assert r.route_type == "single_flow"


def test_route_difficulty_bounded() -> None:
    """难度在 [0,1] 范围内。"""
    task = TaskSpec(
        task_spec_id="t1",
        description="x" * 300,
        initial_state={},
    )
    r = route(task)
    assert 0 <= r.difficulty <= 1
