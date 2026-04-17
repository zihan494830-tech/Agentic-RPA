"""B3: TaskSpec + route_type → DAG；(DAG, completed_steps) → next_steps。"""
from raft.contracts.models import RouteType, TaskSpec, WorkflowDAG


def build_dag(
    task_spec: TaskSpec,
    route_type: RouteType,
    *,
    max_steps: int = 10,
) -> WorkflowDAG:
    """
    TaskSpec + route_type → DAG。
    single_flow: 线性 0→1→2→…→max_steps-1。
    multi_flow: 示例拓扑 0→1, 0→2, 1→3, 2→3（分支后汇聚），步数由 max_steps 控制。
    """
    if route_type == "single_flow":
        nodes = list(range(max_steps))
        edges = [(i, i + 1) for i in range(max_steps - 1)]
        return WorkflowDAG(nodes=nodes, edges=edges)

    # multi_flow: 简单多分支示例
    # 0 → 1, 0 → 2; 1 → 3, 2 → 3; 3 → 4 ... 即前几步分支再汇聚，后面线性
    nodes = list(range(max_steps))
    edges: list[tuple[int, int]] = []
    if max_steps >= 4:
        edges = [(0, 1), (0, 2), (1, 3), (2, 3)]
        for i in range(3, max_steps - 1):
            edges.append((i, i + 1))
    elif max_steps == 3:
        edges = [(0, 1), (0, 2)]
    elif max_steps == 2:
        edges = [(0, 1)]
    return WorkflowDAG(nodes=nodes, edges=edges)


def get_next_steps(dag: WorkflowDAG, completed_steps: set[int]) -> list[int]:
    """
    (DAG, completed_steps) → 可执行的下一步序号列表。
    某步可执行当且仅当其所有前驱已在 completed_steps 中；且该步尚未完成。
    """
    completed = set(completed_steps)
    # 有入边的节点：to_step 的前驱是 from_step
    predecessors: dict[int, set[int]] = {n: set() for n in dag.nodes}
    for from_s, to_s in dag.edges:
        predecessors[to_s].add(from_s)
    # 无入边的节点视为入口，前驱为空
    next_steps: list[int] = []
    for node in dag.nodes:
        if node in completed:
            continue
        if predecessors[node].issubset(completed):
            next_steps.append(node)
    return sorted(next_steps)
