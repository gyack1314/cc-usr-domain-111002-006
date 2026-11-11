"""采集器在真实浏览器与合成门户上的端到端检查。

覆盖需求要点：
* 共享文件 FILE-shared 的两个正常入口都保留为独立路径实例；
* 同名兄弟目录 north/south 不被合并；
* 快捷回环按当前路径有限结束，输出可追溯引用边，且不影响其他入口；
* 无权节点只记录不可读状态，从不展开；
* 预算/深度超限登记未覆盖，coverage 不能声称完整；
* 失败分支可重试，已确认的其他路径不丢失；
* rebuild-tree 下子树整体重建，驱动按 node-id+路径重新定位；
* moved-tree 下旧路径保留证据并标记需核对，不映射到同名目录；
* 采集端只走可见 DOM，不调用 /__state、/__control 运维入口。
"""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from playwright.sync_api import sync_playwright

from collector import CrawlConfig, Crawler, PlaywrightDriver
from portal.__main__ import running


class BrowserCrawlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = sync_playwright().start()
        cls.browser = cls.runner.chromium.launch(headless=True, args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.runner.stop()

    def setUp(self):
        self.server = running()
        self.base, self.state = self.server.__enter__()
        self.tmp = tempfile.TemporaryDirectory()
        self.out = pathlib.Path(self.tmp.name)
        self.requests: list[str] = []

    def tearDown(self):
        self.tmp.cleanup()
        self.server.__exit__(None, None, None)

    def _crawl(self, **config):
        context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        # 拦截运维入口：采集端若借用 /__state、/__control 取数即测试失败。
        def on_request(request):
            if request.url.rstrip("/").endswith(("/__state", "/__control")):
                self.requests.append(request.url)
        page.on("request", on_request)
        page.goto(self.base)
        page.wait_for_selector('[data-path="/root"]')
        driver = PlaywrightDriver(page)
        cfg = CrawlConfig(checkpoint_path=str(self.out / "ckpt.json"), **config)
        crawler = Crawler(driver, cfg)
        result = crawler.run()
        context.close()
        return crawler, result

    def _set_phase(self, phase):
        # 测试调度可以使用运维入口切换阶段；被测采集端不允许。
        import urllib.request
        req = urllib.request.Request(
            self.base + "/__control",
            data=json.dumps({"phase": phase}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req).read()

    # ------------------------------------------------------------ 基础身份语义
    def test_shared_file_two_entries_and_same_name_siblings(self):
        _, result = self._crawl()
        coverage = result.coverage(CrawlConfig())
        self.assertTrue(coverage.complete)
        # 同名兄弟目录是两个实例，id 不同。
        north = result.paths["root/north"]
        south = result.paths["root/south"]
        self.assertEqual(north.name, south.name)
        self.assertNotEqual(north.node_id, south.node_id)
        # 共享文件的两个正常入口都在，且是两条独立路径实例。
        record = result.files["FILE-shared"]
        placements = {p["instance_id"] for p in record.placements}
        self.assertEqual(placements, {"root/north/f1", "root/south/f2"})
        self.assertEqual(2, len(placements))
        # 文件编号只有一条内容索引，但对应两个节点 id。
        self.assertEqual(1, len(result.files))
        self.assertEqual({"f1", "f2"}, {p["node_id"] for p in record.placements})
        self.assertEqual([], self.requests)

    def test_shortcut_cycle_terminates_finitely_with_traceable_edge(self):
        _, result = self._crawl()
        shortcut = result.paths["root/north/to-root"]
        self.assertEqual("cycle", shortcut.terminal_reason)
        ref_edges = [e for e in result.edges.values() if e.edge_type == "reference"]
        self.assertEqual(1, len(ref_edges))
        edge = ref_edges[0]
        self.assertTrue(edge.cycle)
        self.assertEqual("root", edge.target_node_id)
        self.assertEqual(["root", "north", "to-root", "root"], edge.target_id_path)
        # 回环是有限终止而非未覆盖，且其他入口（south）仍完整采集。
        self.assertEqual(1, result.cycle_count)
        self.assertIn("root/south/f2", result.paths)
        self.assertTrue(all(u.reason != "cycle" for u in result.uncovered))

    def test_denied_node_recorded_but_never_expanded(self):
        context = self.browser.new_context()
        page = context.new_page()
        page.goto(self.base)
        page.wait_for_selector('[data-path="/root"]')
        _, result = self._crawl()
        denied = result.paths["root/private"]
        self.assertEqual("denied", denied.kind)
        self.assertEqual("denied", denied.terminal_reason)
        # DOM 中无权节点始终没有展开按钮。
        self.assertEqual(0, page.locator('[data-node-id=private] [data-expand]').count())
        self.assertEqual(1, result.denied_count)
        context.close()

    # ------------------------------------------------------------ 预算与深度
    def test_budget_limit_marks_uncovered_and_never_claims_complete(self):
        crawler, result = self._crawl(expand_budget=1)
        coverage = result.coverage(crawler.config)
        self.assertFalse(coverage.complete)
        reasons = {u.instance_id: u.reason for u in result.uncovered}
        self.assertEqual("budget-exhausted", reasons["root/north"])
        self.assertEqual("budget-exhausted", reasons["root/south"])
        # 检查点保留待办栈，可供续采。
        with open(self.out / "ckpt.json", encoding="utf-8") as handle:
            ckpt = json.load(handle)
        self.assertTrue(ckpt["stack"])

    def test_resume_with_larger_budget_completes_and_keeps_prior_paths(self):
        crawler, first = self._crawl(expand_budget=1)
        self.assertFalse(first.coverage(crawler.config).complete)
        # 新页面/上下文模拟恢复：DOM 引用全部失效，只靠检查点路径重建。
        context = self.browser.new_context()
        page = context.new_page()
        page.goto(self.base)
        page.wait_for_selector('[data-path="/root"]')
        cfg = CrawlConfig(checkpoint_path=str(self.out / "ckpt.json"), expand_budget=10)
        resumed = Crawler(PlaywrightDriver(page), cfg)
        self.assertTrue(resumed.load_checkpoint())
        result = resumed.run()
        context.close()
        coverage = result.coverage(cfg)
        self.assertTrue(coverage.complete)
        self.assertIn("root/north/f1", result.paths)
        self.assertIn("root/south/f2", result.paths)
        # 路径与边幂等，无重复。
        self.assertEqual(len(result.paths), len(set(result.paths)))

    def test_depth_limit(self):
        crawler, result = self._crawl(max_depth=1)
        self.assertFalse(result.coverage(crawler.config).complete)
        self.assertTrue(all(u.reason == "depth-limit" for u in result.uncovered))
        self.assertNotIn("root/north/f1", result.paths)

    # ------------------------------------------------------------ 失败重试
    def test_failed_branch_retry_keeps_confirmed_paths(self):
        self._set_phase("failed-tree")
        crawler, failed = self._crawl(retry_attempts=1)
        self.assertEqual(1, failed.error_count)
        # 北区与共享文件索引在南区失败时仍然保留。
        self.assertIn("root/north/f1", failed.paths)
        self.assertIn("FILE-shared", failed.files)
        # 切回 base，只重试南区，其他路径原样保留。
        self._set_phase("base")
        context = self.browser.new_context()
        page = context.new_page()
        page.goto(self.base)
        page.wait_for_selector('[data-path="/root"]')
        cfg = CrawlConfig(checkpoint_path=str(self.out / "ckpt.json"), retry_attempts=1)
        retry_crawler = Crawler(PlaywrightDriver(page), cfg)
        retry_crawler.load_checkpoint()
        from collector.cli import _prepare_retry
        _prepare_retry(retry_crawler, ["/root/south"])
        result = retry_crawler.run()
        context.close()
        self.assertTrue(result.coverage(cfg).complete)
        self.assertEqual(0, result.error_count)
        self.assertIn("root/south/f2", result.paths)
        self.assertEqual(7, len(result.paths))

    # ------------------------------------------------------------ 子树重建
    def test_rebuild_tree_relocates_by_node_id_and_path(self):
        self._set_phase("rebuild-tree")
        _, result = self._crawl()
        self.assertTrue(result.coverage(CrawlConfig()).complete)
        self.assertEqual(0, result.needs_review_count)
        # 共享文件两个入口、回环、无权在频繁重建下仍全部采到。
        self.assertIn("root/north/f1", result.paths)
        self.assertIn("root/south/f2", result.paths)
        self.assertEqual(1, result.cycle_count)
        self.assertEqual(1, result.denied_count)

    # ------------------------------------------------------------ 移动核对
    def test_moved_tree_keeps_evidence_and_flags_review(self):
        # 先在 base 完整采集并保存路径证据。
        _, base_result = self._crawl(expand_budget=20)
        self.assertTrue(base_result.coverage(CrawlConfig()).complete)
        self._set_phase("moved-tree")
        context = self.browser.new_context()
        page = context.new_page()
        page.goto(self.base)
        page.wait_for_selector('[data-path="/root"]')
        cfg = CrawlConfig(checkpoint_path=str(self.out / "ckpt.json"), expand_budget=20)
        verifier = Crawler(PlaywrightDriver(page), cfg)
        verifier.load_checkpoint()
        result = verifier.verify_saved_paths()
        context.close()
        self.assertEqual(1, result.needs_review_count)
        review = [u for u in result.uncovered if u.reason == "needs-review"]
        self.assertEqual(["root/north"], [u.instance_id for u in review])
        # 旧路径证据保留：north 仍是原 id，f1 的实例与边仍挂在 north 下，未补到 south。
        self.assertEqual("north", result.paths["root/north"].node_id)
        north_edges = [e for e in result.edges.values()
                       if e.parent_instance_id == "root/north"]
        self.assertIn("f1", {e.child_node_id for e in north_edges})
        # 历史路径实例作为证据保留，但 north 已被标记需核对。
        self.assertIn("root/north/f1", result.paths)
        self.assertEqual("needs-review", result.paths["root/north"].terminal_reason)
        # 同名 south 是独立 id，没有被当成 north 的替身。
        self.assertEqual("south", result.paths["root/south"].node_id)
        self.assertNotIn("root/south/f1", result.paths)


if __name__ == "__main__":
    unittest.main()
