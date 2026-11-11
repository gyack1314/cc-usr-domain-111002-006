"""懒加载目录树的路径采集引擎。

关键约束（与业务契约一一对应）：

1. 折叠节点的子节点不在 DOM，目录/快捷节点必须真正点击展开后才能读到子节点；
2. 节点身份是根到当前的 node-id 链，不做全局“见过即跳过”：同一节点经另一合法
   目录入口到达时仍然产生新的路径实例与边；
3. 环只按“当前路径”判断：快捷目标的 id 已在本次下行的 id 链上时，输出可追溯的
   引用边并终止该分支，不影响其他入口；
4. 无权节点只记录可见名称与不可读状态，绝不尝试展开其隐藏子节点；
5. 深度/预算超限时记录未覆盖分支，coverage.complete 保持 False；
6. 门户重建子树导致节点失效时标记 needs_review，保留原路径证据，不补到相似目录；
7. 检查点只保存路径数据，恢复时从入口重新展开建立路径，不复活 DOM 引用。
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .driver import LoadFailed, NodeMoved, PortalDriver, VisibleNode
from .models import (
    Coverage,
    Edge,
    FileRecord,
    PathInstance,
    TerminatedBranch,
    UncoveredBranch,
)


@dataclass
class CrawlConfig:
    max_depth: int | None = None          # root 位于深度 0；None 表示不限
    expand_budget: int | None = None      # 展开动作总预算；None 表示不限
    retry_attempts: int = 2               # 加载失败时同一分支的额外尝试次数
    checkpoint_path: str | None = None    # 检查点文件；None 表示不持久化


@dataclass
class _Frame:
    """迭代 DFS 的栈帧：只保存可序列化的路径信息，不保存 DOM 引用。"""

    path_key: str
    id_path: list[str]
    visible_path: list[str]
    depth: int
    via_chain: list[str]
    attempts: int = 0


@dataclass
class CrawlResult:
    edges: dict[str, Edge] = field(default_factory=dict)
    paths: dict[str, PathInstance] = field(default_factory=dict)
    files: dict[str, FileRecord] = field(default_factory=dict)
    uncovered: list[UncoveredBranch] = field(default_factory=list)
    terminated: list[TerminatedBranch] = field(default_factory=list)
    expansions_used: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def cycle_count(self) -> int:
        return sum(1 for item in self.terminated if item.reason == "cycle")

    @property
    def denied_count(self) -> int:
        return sum(1 for item in self.terminated if item.reason == "denied")

    @property
    def error_count(self) -> int:
        return sum(1 for item in self.uncovered if item.reason == "load-error")

    @property
    def needs_review_count(self) -> int:
        return sum(1 for item in self.uncovered if item.reason == "needs-review")

    def coverage(self, config: CrawlConfig) -> Coverage:
        complete = not self.uncovered
        return Coverage(
            complete=complete,
            expansions_used=self.expansions_used,
            expand_budget=config.expand_budget,
            max_depth=config.max_depth,
            path_instance_count=len(self.paths),
            edge_count=len(self.edges),
            file_count=len(self.files),
            cycle_count=self.cycle_count,
            denied_count=self.denied_count,
            error_count=self.error_count,
            needs_review_count=self.needs_review_count,
            uncovered=list(self.uncovered),
            terminated=list(self.terminated),
            notes=list(self.notes),
        )


def _instance_id(id_path: list[str]) -> str:
    return "/".join(id_path)


def _edge_id(parent_instance: str, child_node_id: str, edge_type: str) -> str:
    return f"{edge_type}:{parent_instance}>{child_node_id}"


class Crawler:
    def __init__(self, driver: PortalDriver, config: CrawlConfig):
        self.driver = driver
        self.config = config
        self.result = CrawlResult()
        self._stack: list[_Frame] = []
        # 检查点中未覆盖分支的去重键，恢复后避免重复登记。
        self._uncovered_keys: set[str] = set()

    # ------------------------------------------------------------------ 输出/检查点
    def _write_checkpoint(self) -> None:
        if not self.config.checkpoint_path:
            return
        payload = {
            "config": {
                "max_depth": self.config.max_depth,
                "expand_budget": self.config.expand_budget,
                "retry_attempts": self.config.retry_attempts,
            },
            "expansions_used": self.result.expansions_used,
            "edges": [edge.to_dict() for edge in self.result.edges.values()],
            "paths": [item.to_dict() for item in self.result.paths.values()],
            "files": [item.to_dict() for item in self.result.files.values()],
            "uncovered": [asdict_uncovered(item) for item in self.result.uncovered],
            "terminated": [asdict_terminated(item) for item in self.result.terminated],
            "stack": [frame.__dict__.copy() for frame in self._stack],
        }
        target = Path(self.config.checkpoint_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".ckpt-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def load_checkpoint(self) -> bool:
        """从检查点恢复已确认结果与待办栈。返回是否成功加载。"""
        path = self.config.checkpoint_path
        if not path or not Path(path).is_file():
            return False
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.result.expansions_used = payload["expansions_used"]
        for data in payload["edges"]:
            edge = Edge(**data)
            self.result.edges[edge.edge_id] = edge
        for data in payload["paths"]:
            item = PathInstance(**data)
            self.result.paths[item.instance_id] = item
        for data in payload["files"]:
            self.result.files[data["file_id"]] = FileRecord(**data)
        for data in payload["uncovered"]:
            branch = UncoveredBranch(**data)
            self.result.uncovered.append(branch)
            self._uncovered_keys.add(branch.instance_id + "|" + branch.reason)
        for data in payload.get("terminated", []):
            self.result.terminated.append(TerminatedBranch(**data))
        self._stack = [_Frame(**frame) for frame in payload.get("stack", [])]
        self.result.notes.append(f"从检查点恢复：{path}")
        return True

    # ------------------------------------------------------------------ 记录辅助
    def _record_path(self, node: VisibleNode, frame: _Frame,
                     terminal_reason: str | None = None,
                     detail: str | None = None) -> PathInstance:
        instance = PathInstance(
            instance_id=_instance_id(frame.id_path),
            node_id=node.node_id,
            name=node.name,
            kind=node.kind,
            id_path=list(frame.id_path),
            visible_path=list(frame.visible_path),
            depth=frame.depth,
            parent_instance_id=_instance_id(frame.id_path[:-1]) if len(frame.id_path) > 1 else None,
            via_reference_chain=list(frame.via_chain),
            terminal_reason=terminal_reason,
            detail=detail,
        )
        # 幂等上写：同一实例的路径数据是确定性的，后续只补充终止原因等状态。
        self.result.paths[instance.instance_id] = instance
        return instance

    def _record_edge(self, edge: Edge) -> None:
        self.result.edges.setdefault(edge.edge_id, edge)

    def _record_file(self, node: VisibleNode, frame: _Frame) -> None:
        if not node.file_id:
            return
        record = self.result.files.get(node.file_id)
        if record is None:
            record = FileRecord(file_id=node.file_id, name=node.name)
            self.result.files[node.file_id] = record
        placement = {
            "instance_id": _instance_id(frame.id_path),
            "node_id": node.node_id,
            "visible_path": " / ".join(frame.visible_path),
        }
        if placement not in record.placements:
            record.placements.append(placement)

    def _add_uncovered(self, frame: _Frame, reason: str, detail: str) -> None:
        key = _instance_id(frame.id_path) + "|" + reason
        if key in self._uncovered_keys:
            return
        self._uncovered_keys.add(key)
        self.result.uncovered.append(
            UncoveredBranch(
                instance_id=_instance_id(frame.id_path),
                node_id=frame.id_path[-1],
                name=frame.visible_path[-1],
                id_path=list(frame.id_path),
                visible_path=list(frame.visible_path),
                reason=reason,
                detail=detail,
            )
        )

    def _clear_uncovered_for(self, id_path: list[str]) -> None:
        """帧被重新处理（续采/重试）时，丢弃该实例此前的未覆盖登记。

        旧登记只是上一轮预算/深度下的快照；本轮成功展开后必须移除，
        否则已覆盖分支会永久残留在 coverage.uncovered 中。
        """
        instance_id = _instance_id(id_path)
        kept = []
        for branch in self.result.uncovered:
            if branch.instance_id == instance_id:
                self._uncovered_keys.discard(
                    branch.instance_id + "|" + branch.reason
                )
            else:
                kept.append(branch)
        self.result.uncovered = kept

    def _add_terminated(self, frame: _Frame, reason: str, detail: str) -> None:
        """记录规则内的有限终止（回环/无权），幂等。"""
        instance_id = _instance_id(frame.id_path)
        if any(item.instance_id == instance_id and item.reason == reason
               for item in self.result.terminated):
            return
        self.result.terminated.append(
            TerminatedBranch(
                instance_id=instance_id,
                node_id=frame.id_path[-1],
                name=frame.visible_path[-1],
                id_path=list(frame.id_path),
                visible_path=list(frame.visible_path),
                reason=reason,
                detail=detail,
            )
        )

    # ------------------------------------------------------------------ 主流程
    def run(self) -> CrawlResult:
        if not self._stack:
            root_key = self.driver.root_path_key()
            try:
                root = self.driver.locate(root_key)
            except NodeMoved as exc:
                # 入口本身失效：不猜测相似目录，直接需核对并结束。
                root_frame = _Frame(
                    path_key="/root", id_path=["root"],
                    visible_path=["入口"], depth=0, via_chain=[],
                )
                self._add_uncovered(root_frame, "needs-review", f"入口节点失效：{exc}")
                self._write_checkpoint()
                return self.result
            frame = _Frame(
                path_key=root_key,
                id_path=[root.node_id],
                visible_path=[root.name],
                depth=0,
                via_chain=[],
            )
            self._record_path(root, frame)
            self._stack.append(frame)

        while self._stack:
            # 先落盘再取帧：检查点必须保留待办栈，预算/深度受限的帧才能在续采时重处理。
            self._write_checkpoint()
            # 预算在 pop 前判断：耗尽时剩余帧原样留在栈中（已登记为未覆盖），
            # 不弹出、不处理，等待续采获得新预算。
            if self._budget_exhausted():
                break
            frame = self._stack.pop()
            # 恢复采集：DOM 已随新页面生命周期重置，先从入口按已保存路径重新展开。
            if not self._reestablish_path(frame):
                self._write_checkpoint()
                continue
            self._process_frame(frame)
            self._write_checkpoint()
        return self.result

    def _reestablish_path(self, frame: _Frame) -> bool:
        """从入口逐级重新展开 frame 的祖先路径，使目标节点重新出现在 DOM。

        只依据检查点中的 id 路径重新定位，绝不复活旧元素句柄。任何一级对不上
        （目录已移动/目标失效）都保留原路径证据并标记 needs_review，不补到同名目录。
        """
        # 祖先键：/root, /root/north ...（不含 frame 自身）。
        ancestor_keys = ["/" + "/".join(frame.id_path[: index + 1])
                         for index in range(len(frame.id_path) - 1)]
        for key in ancestor_keys:
            try:
                node = self.driver.locate(key)
            except NodeMoved as exc:
                self._mark_needs_review(
                    _Frame(
                        path_key=key,
                        id_path=frame.id_path[: key.count("/")],
                        visible_path=frame.visible_path[: key.count("/")],
                        depth=key.count("/") - 1,
                        via_chain=[],
                    ),
                    f"恢复路径时祖先失效：{exc}",
                )
                return False
            if node.loaded == "true":
                continue
            if self._budget_exhausted():
                self._add_uncovered(frame, "budget-exhausted",
                                    "恢复路径重建时展开预算已用尽")
                return False
            try:
                self.result.expansions_used += 1
                self.driver.expand(key)
            except LoadFailed as exc:
                self._mark_needs_review(
                    _Frame(
                        path_key=key,
                        id_path=frame.id_path[: key.count("/")],
                        visible_path=frame.visible_path[: key.count("/")],
                        depth=key.count("/") - 1,
                        via_chain=[],
                    ),
                    f"恢复路径时展开失败：{exc}",
                )
                return False
            except NodeMoved as exc:
                self._mark_needs_review(frame, f"恢复路径时节点失效：{exc}")
                return False
        return True

    def _process_frame(self, frame: _Frame) -> None:
        # 本轮重新处理该实例：先清掉上一轮留下的未覆盖快照与终止状态，再按当前结果登记。
        self._clear_uncovered_for(frame.id_path)
        try:
            node = self.driver.locate(frame.path_key)
        except NodeMoved as exc:
            self._mark_needs_review(frame, str(exc))
            return
        # 重新处理成功定位：清除上一轮（如预算受限）留下的终止原因。
        existed = self.result.paths.get(_instance_id(frame.id_path))
        if existed is not None and existed.terminal_reason is not None:
            self.result.paths[existed.instance_id] = PathInstance(
                **{**existed.to_dict(), "terminal_reason": None, "detail": None}
            )

        if node.kind == "file":
            self._record_path(node, frame)
            self._record_file(node, frame)
            return

        if node.kind == "denied":
            # 只记录可见名称与不可读状态，不尝试展开。
            self._record_path(node, frame, terminal_reason="denied",
                              detail="门户标记为不可读取，未尝试展开")
            self._add_terminated(frame, "denied", "门户标记为不可读取，未尝试展开")
            return

        if self.config.max_depth is not None and frame.depth >= self.config.max_depth:
            self._record_path(node, frame, terminal_reason="depth-limit",
                              detail=f"达到最大深度 {self.config.max_depth}，分支未展开")
            self._add_uncovered(frame, "depth-limit",
                                f"深度 {frame.depth} 达到上限 {self.config.max_depth}")
            return

        # 通用回环守卫：仅按“当前路径”判断。若本节点 id 已作为祖先出现在本次下行
        # 的 id 链中（含快捷节点 id 自身重复、引用解析后落回祖先目录两种情形），
        # 有限终止该分支。不使用全局集合，因此同一节点经另一合法入口到达时仍正常采集。
        if node.kind in ("directory", "shortcut") and node.node_id in frame.id_path[:-1]:
            self._record_path(
                node, frame, terminal_reason="cycle",
                detail=f"节点 {node.node_id} 已在当前路径 {_instance_id(frame.id_path[:-1])} 上"
            )
            self._add_terminated(
                frame, "cycle",
                f"当前路径回环：{_instance_id(frame.id_path)}，分支有限终止"
            )
            self._record_cycle_edge(node, frame)
            return

        if node.kind == "shortcut":
            self._handle_shortcut(node, frame)
            return

        if node.kind == "directory":
            self._expand_and_enqueue(node, frame)
            return

        # 未知类型保守处理为需核对，不静默丢弃。
        self._mark_needs_review(frame, f"未知节点类型 {node.kind}")

    def _record_cycle_edge(self, node: VisibleNode, frame: "_Frame") -> None:
        """回环守卫命中时补记一条可追溯引用边（若尚不存在）。

        快捷节点指向其 data-target；目录祖先环指向目录自身 id。
        """
        target_id = node.target if node.kind == "shortcut" else node.node_id
        edge = Edge(
            edge_id=_edge_id(_instance_id(frame.id_path), target_id, "reference"),
            edge_type="reference",
            parent_instance_id=_instance_id(frame.id_path),
            child_instance_id=None,
            child_node_id=node.node_id,
            child_name=node.name,
            child_kind=node.kind,
            target_node_id=target_id,
            target_id_path=list(frame.id_path) + [target_id],
            cycle=True,
            detail="当前路径祖先环，分支在此终止",
        )
        self._record_edge(edge)

    # ------------------------------------------------------------------ 快捷引用
    def _handle_shortcut(self, node: VisibleNode, frame: _Frame) -> None:
        target_id = node.target
        self._record_path(node, frame)
        if not target_id:
            self._mark_needs_review(frame, "快捷引用缺少 data-target")
            return
        # 环按“当前路径”判断：只看本次下行的 id 链，不使用全局集合。
        is_cycle = target_id in frame.id_path
        edge = Edge(
            edge_id=_edge_id(_instance_id(frame.id_path), target_id, "reference"),
            edge_type="reference",
            parent_instance_id=_instance_id(frame.id_path),
            child_instance_id=None,
            child_node_id=node.node_id,
            child_name=node.name,
            child_kind="shortcut",
            target_node_id=target_id,
            target_id_path=list(frame.id_path) + [target_id],
            cycle=is_cycle,
            detail="目标祖先已在当前路径上，分支在此终止" if is_cycle else None,
        )
        self._record_edge(edge)
        if is_cycle:
            # 终止该分支：不展开快捷目标，但其他入口不受影响。
            self._record_path(
                node, frame, terminal_reason="cycle",
                detail=f"引用目标 {target_id} 已在当前路径 {_instance_id(frame.id_path)} 上"
            )
            self._add_terminated(
                frame, "cycle",
                f"快捷引用回环：{_instance_id(frame.id_path)} -> {target_id}，分支有限终止"
            )
            return

        # 合法引用：展开快捷节点，门户会把目标的子节点挂到该实例下。
        self._expand_and_enqueue(node, frame)

    # ------------------------------------------------------------------ 展开与入栈
    def _expand_and_enqueue(self, node: VisibleNode, frame: _Frame) -> None:
        if self._budget_exhausted():
            # 主循环已在 pop 前拦截预算耗尽；走到这里属于恢复重建等兜底路径，
            # 只补登记未覆盖，帧本身仍保留在栈中由检查点持久化。
            self._record_path(node, frame, terminal_reason="budget-exhausted",
                              detail=f"展开预算 {self.config.expand_budget} 已用尽")
            self._add_uncovered(frame, "budget-exhausted",
                                f"展开预算 {self.config.expand_budget} 已用尽，分支未展开")
            return

        try:
            loaded = self._expand_with_retry(frame)
        except LoadFailed as exc:
            self._record_path(node, frame, terminal_reason="load-error", detail=str(exc))
            self._add_uncovered(frame, "load-error",
                                f"重试 {frame.attempts} 次后仍失败：{exc}")
            return
        except NodeMoved as exc:
            self._mark_needs_review(frame, str(exc))
            return

        if loaded != "true":
            self._mark_needs_review(frame, f"展开后状态异常：{loaded}")
            return

        try:
            children = self.driver.children(frame.path_key)
        except NodeMoved as exc:
            self._mark_needs_review(frame, str(exc))
            return

        # 子节点入栈（逆序压栈以保持门户展示顺序的遍历顺序）。
        pending: list[_Frame] = []
        for child in children:
            child_id_path = frame.id_path + [child.node_id]
            child_visible = frame.visible_path + [child.name]
            child_frame = _Frame(
                path_key=frame.path_key + "/" + child.node_id,
                id_path=child_id_path,
                visible_path=child_visible,
                depth=frame.depth + 1,
                via_chain=list(frame.via_chain),
            )
            contains = Edge(
                edge_id=_edge_id(_instance_id(frame.id_path), child.node_id, "contains"),
                edge_type="contains",
                parent_instance_id=_instance_id(frame.id_path),
                child_instance_id=_instance_id(child_id_path),
                child_node_id=child.node_id,
                child_name=child.name,
                child_kind=child.kind,
            )
            self._record_edge(contains)
            if child.kind == "file":
                self._record_path(child, child_frame)
                self._record_file(child, child_frame)
            elif child.kind == "denied":
                # 无权占位：只记录可见名称与不可读状态，不入栈、不展开。
                self._record_path(
                    child, child_frame, terminal_reason="denied",
                    detail="门户标记为不可读取，未尝试展开"
                )
                self._add_terminated(
                    child_frame, "denied", "门户标记为不可读取，未尝试展开"
                )
            elif self._budget_exhausted():
                # 预算超限：登记未覆盖，同时把帧保留在栈中并写入检查点。
                # 续采获得预算后会重新处理该帧：成功则清除此登记；目录已移动则转 needs-review。
                self._record_path(
                    child, child_frame, terminal_reason="budget-exhausted",
                    detail=f"展开预算 {self.config.expand_budget} 已用尽，分支未展开"
                )
                self._add_uncovered(
                    child_frame, "budget-exhausted",
                    f"展开预算 {self.config.expand_budget} 已用尽，分支未展开"
                )
                pending.append(child_frame)
            else:
                self._record_path(child, child_frame)
                pending.append(child_frame)

        for child_frame in reversed(pending):
            self._stack.append(child_frame)

    def _budget_exhausted(self) -> bool:
        return self.config.expand_budget is not None and \
            self.result.expansions_used >= self.config.expand_budget

    def _expand_with_retry(self, frame: _Frame) -> str:
        while True:
            try:
                self.result.expansions_used += 1
                return self.driver.expand(frame.path_key)
            except LoadFailed as exc:
                if frame.attempts >= self.config.retry_attempts:
                    raise
                frame.attempts += 1
                self.result.notes.append(
                    f"分支 {frame.path_key} 第 {frame.attempts} 次重试：{exc}"
                )

    def _mark_needs_review(self, frame: _Frame, detail: str) -> None:
        self._add_uncovered(frame, "needs-review", detail)
        instance_id = _instance_id(frame.id_path)
        existed = self.result.paths.get(instance_id)
        if existed is not None:
            self.result.paths[instance_id] = PathInstance(
                **{**existed.to_dict(), "terminal_reason": "needs-review", "detail": detail}
            )

    # ------------------------------------------------------------------ 移动核对
    def verify_saved_paths(self) -> CrawlResult:
        """从入口重建并核对检查点中已保存的每条目录路径。

        不依赖任何 DOM 引用复活节点：逐级按 id 路径重新展开，再比对当前子节点
        与已保存的 contains 边。目录已移动/目标失效时保留原路径证据并标记
        needs-review，绝不补挂到同名目录。文件/无权/回环实例不参与展开核对。
        """
        directory_instances = [
            item for item in self.result.paths.values()
            if item.kind in ("directory", "shortcut")
            and item.terminal_reason in (None, "budget-exhausted", "depth-limit")
        ]
        for saved in sorted(directory_instances, key=lambda item: len(item.id_path)):
            frame = _Frame(
                path_key="/" + "/".join(saved.id_path),
                id_path=list(saved.id_path),
                visible_path=list(saved.visible_path),
                depth=saved.depth,
                via_chain=list(saved.via_reference_chain),
            )
            # 逐级重建祖先路径；任何一级对不上即需核对，原路径证据保留在 paths/edges 中。
            if not self._reestablish_path(frame):
                continue
            try:
                node = self.driver.locate(frame.path_key)
            except NodeMoved as exc:
                self._mark_needs_review(frame, str(exc))
                continue
            if node.loaded != "true":
                # 仍折叠的目录：尝试展开后再比对（受预算约束）。
                if self._budget_exhausted():
                    self._add_uncovered(frame, "budget-exhausted",
                                        "核对时预算不足，目录未能重新展开")
                    continue
                try:
                    self.result.expansions_used += 1
                    self.driver.expand(frame.path_key)
                except (LoadFailed, NodeMoved) as exc:
                    self._mark_needs_review(frame, f"核对时展开失败：{exc}")
                    continue
            try:
                actual = self.driver.children(frame.path_key)
            except NodeMoved as exc:
                self._mark_needs_review(frame, str(exc))
                continue
            expected = {
                edge.child_node_id for edge in self.result.edges.values()
                if edge.edge_type == "contains"
                and edge.parent_instance_id == saved.instance_id
            }
            actual_ids = {child.node_id for child in actual}
            if expected and expected != actual_ids:
                self._mark_needs_review(
                    frame,
                    "重新展开后的子节点与已保存路径证据不一致："
                    f"期望 {sorted(expected)}，实际 {sorted(actual_ids)}"
                )
        self._write_checkpoint()
        return self.result


def asdict_uncovered(branch: UncoveredBranch) -> dict[str, Any]:
    return {
        "instance_id": branch.instance_id,
        "node_id": branch.node_id,
        "name": branch.name,
        "id_path": list(branch.id_path),
        "visible_path": list(branch.visible_path),
        "reason": branch.reason,
        "detail": branch.detail,
    }


def asdict_terminated(branch: TerminatedBranch) -> dict[str, Any]:
    return {
        "instance_id": branch.instance_id,
        "node_id": branch.node_id,
        "name": branch.name,
        "id_path": list(branch.id_path),
        "visible_path": list(branch.visible_path),
        "reason": branch.reason,
        "detail": branch.detail,
    }
