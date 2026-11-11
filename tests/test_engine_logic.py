"""用内存假驱动验证遍历引擎的纯逻辑，不启动浏览器。

假驱动模拟门户契约：折叠无子节点、展开后才有子节点、快捷节点带 target、
无权节点不可展开。重点验证“按当前路径判环、不做全局去重”。
"""
from __future__ import annotations

import unittest

from collector.crawler import CrawlConfig, Crawler
from collector.driver import LoadFailed, PortalDriver, VisibleNode


class FakeDriver(PortalDriver):
    def __init__(self, nodes: dict, fail: set[str] | None = None):
        self.nodes = nodes
        self.expanded: set[str] = set()
        self.fail = fail or set()
        self.expand_calls = 0

    def _key(self, id_path: list[str]) -> str:
        return "/" + "/".join(id_path)

    def root_path_key(self) -> str:
        return "/root"

    def _snapshot(self, node_id: str, path_key: str, loaded: str) -> VisibleNode:
        node = self.nodes[node_id]
        return VisibleNode(
            node_id=node["id"], name=node["name"], kind=node["kind"],
            path_key=path_key, loaded=loaded,
            target=node.get("target"), file_id=node.get("file"),
        )

    def locate(self, expected_path_key: str) -> VisibleNode:
        ids = [part for part in expected_path_key.split("/") if part]
        node_id = ids[-1]
        if node_id not in self.nodes:
            from collector.driver import NodeMoved
            raise NodeMoved(expected_path_key)
        loaded = "true" if self._key(ids) in self.expanded else "false"
        return self._snapshot(node_id, expected_path_key, loaded)

    def expand(self, expected_path_key: str) -> str:
        self.expand_calls += 1
        ids = [part for part in expected_path_key.split("/") if part]
        if ids[-1] in self.fail:
            raise LoadFailed(expected_path_key)
        self.expanded.add(expected_path_key)
        return "true"

    def children(self, parent_path_key: str) -> list[VisibleNode]:
        ids = [part for part in parent_path_key.split("/") if part]
        node = self.nodes[ids[-1]]
        if node["kind"] == "shortcut":
            node = self.nodes[node["target"]]
        if parent_path_key not in self.expanded:
            return []
        out = []
        for child_id in node.get("children", []):
            child = self.nodes[child_id]
            out.append(VisibleNode(
                node_id=child["id"], name=child["name"], kind=child["kind"],
                path_key=parent_path_key + "/" + child_id, loaded="false",
                target=child.get("target"), file_id=child.get("file"),
            ))
        return out

    def close(self):
        return None


# 两个目录入口都通过快捷方式指向同一个共享目录 shared，shared 内有回指入口的快捷方式。
GRAPH = {
    "root": {"id": "root", "name": "根", "kind": "directory",
             "children": ["dirA", "dirB", "secret"]},
    "dirA": {"id": "dirA", "name": "入口甲", "kind": "directory", "children": ["linkSharedA"]},
    "dirB": {"id": "dirB", "name": "入口乙", "kind": "directory", "children": ["linkSharedB"]},
    "linkSharedA": {"id": "linkSharedA", "name": "共享目录", "kind": "shortcut",
                    "target": "shared"},
    "linkSharedB": {"id": "linkSharedB", "name": "共享目录", "kind": "shortcut",
                    "target": "shared"},
    "shared": {"id": "shared", "name": "共享区", "kind": "directory",
               "children": ["doc", "backToA"]},
    "doc": {"id": "doc", "name": "图纸", "kind": "file", "file": "FILE-X"},
    "backToA": {"id": "backToA", "name": "回到甲", "kind": "shortcut", "target": "dirA"},
    "secret": {"id": "secret", "name": "机密", "kind": "denied"},
}


class EngineLogicTests(unittest.TestCase):
    def test_same_target_reached_via_two_entries_not_globally_deduped(self):
        driver = FakeDriver(GRAPH)
        result = Crawler(driver, CrawlConfig()).run()
        # shared 经甲、乙两个合法入口到达，产生两条独立路径，而不是见过一次就跳过。
        self.assertIn("root/dirA/linkSharedA", result.paths)
        self.assertIn("root/dirB/linkSharedB", result.paths)
        # 同一文件编号只有一条内容索引；经不同入口/引用链产生多条 placement，
        # 每条 placement 的实例路径都不同（不做全局去重）。
        self.assertEqual(1, len(result.files))
        placements = result.files["FILE-X"].placements
        self.assertGreaterEqual(len(placements), 2)
        self.assertEqual(len(placements), len({p["instance_id"] for p in placements}))
        # 两个一级入口各自能到达该文件。
        prefixes = {p["instance_id"].split("/")[1] for p in placements}
        self.assertEqual({"dirA", "dirB"}, prefixes)
        # 回环按当前路径在两个入口各自有限终止。
        cycles = [t for t in result.terminated if t.reason == "cycle"]
        self.assertGreaterEqual(len(cycles), 2)
        self.assertTrue(result.coverage(CrawlConfig()).complete)

    def test_cycle_judged_by_current_path_only(self):
        driver = FakeDriver(GRAPH)
        result = Crawler(driver, CrawlConfig()).run()
        # 回环边记录完整解析上下文（target_id_path）与目标 id，可追溯。
        ref = [e for e in result.edges.values() if e.edge_type == "reference" and e.cycle]
        self.assertTrue(ref)
        for edge in ref:
            self.assertTrue(edge.target_id_path)
            self.assertEqual(edge.target_node_id, edge.target_id_path[-1])
            self.assertEqual(edge.parent_instance_id,
                             "/".join(edge.target_id_path[:-1]))
            # 回环成因之一可追溯：目标是祖先，或当前路径上已有同名节点重复。
            target_is_ancestor = edge.target_node_id in edge.target_id_path[:-1]
            id_chain = edge.target_id_path[:-1]
            has_repeat = len(id_chain) != len(set(id_chain))
            self.assertTrue(target_is_ancestor or has_repeat)
        # 回环是有限终止：引擎不会无限展开，所有回环实例都带 cycle 终止原因。
        self.assertTrue(all(
            result.paths[t.instance_id].terminal_reason == "cycle"
            for t in result.terminated if t.reason == "cycle"
        ))
        # 两个入口都各自产生回环，证明不是全局见过一次就跳过。
        entry_of = {t.id_path[1] for t in result.terminated if t.reason == "cycle"}
        self.assertEqual({"dirA", "dirB"}, entry_of)

    def test_denied_is_terminal_and_never_expanded(self):
        driver = FakeDriver(GRAPH)
        result = Crawler(driver, CrawlConfig()).run()
        self.assertEqual("denied", result.paths["root/secret"].terminal_reason)
        # 假驱动不会收到对 denied 节点的展开调用（其路径键不出现在 expanded）。
        self.assertNotIn("/root/secret", driver.expanded)

    def test_budget_and_depth_leave_traceable_uncovered(self):
        driver = FakeDriver(GRAPH)
        result = Crawler(driver, CrawlConfig(expand_budget=1)).run()
        cov = result.coverage(CrawlConfig(expand_budget=1))
        self.assertFalse(cov.complete)
        self.assertTrue(all(u.reason == "budget-exhausted" for u in result.uncovered))

        driver2 = FakeDriver(GRAPH)
        depth_result = Crawler(driver2, CrawlConfig(max_depth=1)).run()
        self.assertFalse(depth_result.coverage(CrawlConfig(max_depth=1)).complete)
        self.assertTrue(all(u.reason == "depth-limit" for u in depth_result.uncovered))

    def test_load_failure_is_isolated_and_retryable(self):
        driver = FakeDriver(GRAPH, fail={"dirB"})
        result = Crawler(driver, CrawlConfig(retry_attempts=0)).run()
        self.assertEqual(1, result.error_count)
        # 甲入口与共享文件仍完整，失败只隔离在乙分支。
        self.assertIn("root/dirA/linkSharedA", result.paths)
        self.assertIn("FILE-X", result.files)
        self.assertTrue(any(u.instance_id == "root/dirB" and u.reason == "load-error"
                            for u in result.uncovered))


if __name__ == "__main__":
    unittest.main()
