"""B3 单测：DAG 构建、next_steps 拓扑。"""
import pytest

from raft.contracts.models import TaskSpec, WorkflowDAG
from raft.core.dag import build_dag, get_next_steps


def test_build_dag_single_flow_linear() -> None:
    """single_flow 得到线性 DAG：0→1→2。"""
    task = TaskSpec(task_spec_id="t1", description="Linear", initial_state={})
    dag = build_dag(task, "single_flow", max_steps=3)
    assert dag.nodes == [0, 1, 2]
    assert dag.edges == [(0, 1), (1, 2)]


def test_get_next_steps_single_flow() -> None:
    """线性 DAG：初始 next_steps=[0]；完成 0 后 [1]；完成 1 后 [2]。"""
    dag = WorkflowDAG(nodes=[0, 1, 2], edges=[(0, 1), (1, 2)])
    assert get_next_steps(dag, set()) == [0]
    assert get_next_steps(dag, {0}) == [1]
    assert get_next_steps(dag, {0, 1}) == [2]
    assert get_next_steps(dag, {0, 1, 2}) == []


def test_build_dag_multi_flow_branch_join() -> None:
    """multi_flow 至少 4 步时：0→1, 0→2, 1→3, 2→3。"""
    task = TaskSpec(task_spec_id="t1", description="Branch", initial_state={})
    dag = build_dag(task, "multi_flow", max_steps=4)
    assert dag.nodes == [0, 1, 2, 3]
    assert (0, 1) in dag.edges
    assert (0, 2) in dag.edges
    assert (1, 3) in dag.edges
    assert (2, 3) in dag.edges


def test_get_next_steps_multi_flow_parallel() -> None:
    """多分支 DAG：完成 0 后 next_steps 为 [1, 2]（可并行）。"""
    dag = WorkflowDAG(
        nodes=[0, 1, 2, 3],
        edges=[(0, 1), (0, 2), (1, 3), (2, 3)],
    )
    assert get_next_steps(dag, set()) == [0]
    assert sorted(get_next_steps(dag, {0})) == [1, 2]
    assert get_next_steps(dag, {0, 1}) == [2]
    assert get_next_steps(dag, {0, 2}) == [1]
    assert get_next_steps(dag, {0, 1, 2}) == [3]
    assert get_next_steps(dag, {0, 1, 2, 3}) == []
