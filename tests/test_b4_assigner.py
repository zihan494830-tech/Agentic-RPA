"""B4 单测：步骤→Agent 分配、路由表。"""
import pytest

from raft.contracts.models import AgentRole
from raft.core.scheduler import assign_step


def test_assign_step_roles_cycle() -> None:
    """按步序号轮询 planner → execution → verifier。"""
    a0 = assign_step(0)
    a1 = assign_step(1)
    a2 = assign_step(2)
    a3 = assign_step(3)
    assert a0.agent_role == "planner"
    assert a1.agent_role == "execution"
    assert a2.agent_role == "verifier"
    assert a3.agent_role == "planner"


def test_assign_step_tool_target_rpa() -> None:
    """默认 tool_target 为 rpa。"""
    a = assign_step(0)
    assert a.tool_target == "rpa"


def test_assign_step_index_set() -> None:
    """step_index 与入参一致。"""
    a = assign_step(5)
    assert a.step_index == 5
