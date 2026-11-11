"""目录采集结果模型。

四类结果通过稳定字段互相关联：

* 目录边 edges：描述“父实例 -> 子实例/引用目标”的可见关系；
* 文件内容索引 files：按文件编号 file_id 归并，文件编号与节点 id 是两回事；
* 路径实例 paths：每一次出现在某条分类路径上的节点都有一条独立实例；
* 覆盖状态 coverage：本次预算/深度下的完整性与未覆盖分支清单。

同一份文件出现在两个目录中会产生两条路径实例、两条边，但文件内容索引只有一条。
路径相同、名称相同或目标相同都不构成合并依据——身份以节点 id 的完整路径为准。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Literal

NodeKind = Literal["directory", "file", "shortcut", "denied"]
EdgeType = Literal["contains", "reference"]
TerminalReason = Literal[
    "cycle",
    "denied",
    "load-error",
    "depth-limit",
    "budget-exhausted",
    "needs-review",
]


@dataclass(frozen=True)
class PathInstance:
    """节点在某条用户可见分类路径上的一次出现。

    instance_id 由根到当前的节点 id 链拼成；同名兄弟目录的 id 不同，
    因此即使 visible_path 文本完全一致，实例也不会被合并。
    """

    instance_id: str
    node_id: str
    name: str
    kind: NodeKind
    id_path: list[str]
    visible_path: list[str]
    depth: int
    parent_instance_id: str | None
    # 快捷引用链：从当前实例到被指向目标的引用边（可为多跳），用于追溯。
    via_reference_chain: list[str] = field(default_factory=list)
    # 终止型实例的原因（无权、回环、加载失败、深度限制、需核对）。
    terminal_reason: TerminalReason | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Edge:
    """目录边。

    contains 边连接父实例与直接子实例；reference 边连接快捷实例与其目标节点，
    target_id_path 是目标的绝对 id 路径（解析时的当前路径上下文），
    即使目标形成环，引用边本身也可追溯。
    """

    edge_id: str
    edge_type: EdgeType
    parent_instance_id: str
    child_instance_id: str | None
    child_node_id: str
    child_name: str
    child_kind: NodeKind
    # reference 边专有：被指向节点的 id 与其解析上下文绝对路径。
    target_node_id: str | None = None
    target_id_path: list[str] | None = None
    cycle: bool = False
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FileRecord:
    """文件内容索引条目，按文件编号归并（同一编号只出现一次）。"""

    file_id: str
    name: str
    # 该文件编号下的每一个节点实例，保留各自的分类路径。
    placements: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UncoveredBranch:
    """未覆盖分支的可追溯记录。"""

    instance_id: str
    node_id: str
    name: str
    id_path: list[str]
    visible_path: list[str]
    reason: TerminalReason
    detail: str


@dataclass
class TerminatedBranch:
    """结构上有限终止的分支：快捷回环或无权节点。

    这类分支已经按规则走到尽头，不属于“未覆盖”；单独列出便于审计追溯。
    """

    instance_id: str
    node_id: str
    name: str
    id_path: list[str]
    visible_path: list[str]
    reason: Literal["cycle", "denied"]
    detail: str


@dataclass
class Coverage:
    """覆盖状态。complete=False 时绝不输出“目录已完整”。

    complete 只取决于 uncovered（预算/深度/加载失败/需核对）；
    terminated 中的回环与无权分支是规则内的有限终止，不影响完整性。
    """

    complete: bool
    expansions_used: int
    expand_budget: int | None
    max_depth: int | None
    path_instance_count: int
    edge_count: int
    file_count: int
    cycle_count: int
    denied_count: int
    error_count: int
    needs_review_count: int
    uncovered: list[UncoveredBranch] = field(default_factory=list)
    terminated: list[TerminatedBranch] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["uncovered"] = [asdict(item) for item in self.uncovered]
        data["terminated"] = [asdict(item) for item in self.terminated]
        return data
