"""门户驱动抽象与 Playwright 实现。

采集器只依赖 :class:`PortalDriver` 协议，不直接持有 Playwright 元素句柄：
门户会重建子树，缓存的 locator/element 在下一次操作时可能指向同名新节点，
因此每次操作都按“node-id + 期望绝对路径”在当前 DOM 上重新定位并核对。

采集端只读取用户可见的 DOM 状态并点击页面按钮；不调用 /__state、/__control
等运维入口，也不直接抓取 data.json。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

# data-loaded 的三种可见状态：未展开、展开完成、加载失败。
LOADED_FALSE = "false"
LOADED_TRUE = "true"
LOADED_ERROR = "error"


@dataclass(frozen=True)
class VisibleNode:
    """当前 DOM 中一个 treeitem 的可见快照（不含其折叠的子节点）。"""

    node_id: str
    name: str
    kind: str
    path_key: str
    loaded: str
    target: str | None
    file_id: str | None


class PortalDriver(ABC):
    """门户操作协议。路径键约定为以 / 分隔的节点 id 绝对路径，如 /root/north。"""

    @abstractmethod
    def root_path_key(self) -> str:
        """入口节点的路径键。恢复采集时也从入口重新建立路径。"""

    @abstractmethod
    def locate(self, expected_path_key: str) -> VisibleNode:
        """按期望绝对路径在当前 DOM 重新定位节点并核对 node-id。

        路径末段必须与期望节点 id 一致；节点不存在或路径对不上时抛出 NodeMoved。
        """

    @abstractmethod
    def expand(self, expected_path_key: str) -> str:
        """点击展开按钮并等待加载状态落定，返回最终 data-loaded 值。

        每次调用都重新定位，不保留跨操作的元素引用。
        """

    @abstractmethod
    def children(self, parent_path_key: str) -> list[VisibleNode]:
        """读取已展开节点当前在 DOM 中的直接子节点。"""

    @abstractmethod
    def close(self) -> None:
        """释放浏览器资源。"""


class NodeMoved(RuntimeError):
    """节点已移动/失效：当前 DOM 中找不到与期望 id 路径一致的节点。"""


class LoadFailed(RuntimeError):
    """节点展开后门户显示加载失败（data-loaded=error）。"""


def _selector(path_key: str) -> str:
    # data-path 以 / 开头，CSS 属性选择器整体加引号。
    return f'[data-path="{path_key}"]'


class PlaywrightDriver(PortalDriver):
    def __init__(self, page: Any, status_selector: str = "#status"):
        self._page = page
        self._status = status_selector

    def root_path_key(self) -> str:
        page = self._page
        page.wait_for_selector('[role="treeitem"][data-path="/root"]', state="attached")
        node = self.locate("/root")
        return node.path_key

    def locate(self, expected_path_key: str) -> VisibleNode:
        page = self._page
        locator = page.locator(_selector(expected_path_key))
        count = locator.count()
        if count == 0:
            raise NodeMoved(f"当前 DOM 中不存在路径 {expected_path_key}，目录可能已移动")
        if count > 1:
            raise NodeMoved(f"路径 {expected_path_key} 在当前 DOM 中出现 {count} 次，无法唯一定位")
        # 重新核对 node-id：路径末段必须就是节点 id，防止同名节点顶替。
        expected_id = expected_path_key.rstrip("/").rsplit("/", 1)[-1]
        actual_id = locator.get_attribute("data-node-id")
        if actual_id != expected_id:
            raise NodeMoved(
                f"路径 {expected_path_key} 上的节点 id 为 {actual_id}，期望 {expected_id}"
            )
        actual_path = locator.get_attribute("data-path")
        if actual_path != expected_path_key:
            raise NodeMoved(
                f"节点路径已变化：期望 {expected_path_key}，实际 {actual_path}"
            )
        return VisibleNode(
            node_id=actual_id,
            name=locator.locator(".row > span").first.inner_text().strip(),
            kind=locator.get_attribute("data-kind"),
            path_key=actual_path,
            loaded=locator.get_attribute("data-loaded"),
            target=locator.get_attribute("data-target"),
            file_id=(locator.locator("[data-file-id]").first.get_attribute("data-file-id")
                     if locator.locator("[data-file-id]").count() else None),
        )

    def expand(self, expected_path_key: str) -> str:
        page = self._page
        node = self.locate(expected_path_key)
        if node.loaded == LOADED_TRUE:
            return LOADED_TRUE
        button = page.locator(_selector(expected_path_key)).locator("[data-expand]")
        if button.count() == 0:
            # 无权/文件节点没有展开按钮，调用方不应走到这里。
            raise NodeMoved(f"节点 {expected_path_key} 没有展开按钮")
        button.first.click()
        # 等待该路径节点进入终态。门户可能在点击后整体重建 #tree，旧元素被替换，
        # 因此等待条件始终在“当前 DOM”上按 data-path 重新查询，不持有旧元素句柄。
        try:
            page.wait_for_function(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    const state = el.getAttribute('data-loaded');
                    return state === 'true' || state === 'error';
                }""",
                arg=_selector(expected_path_key),
                timeout=10000,
            )
        except Exception as exc:
            raise LoadFailed(f"等待节点 {expected_path_key} 展开超时") from exc
        # 重建核对：按 node-id + 父路径在新 DOM 上重新定位，防止同名节点顶替。
        node = self.locate(expected_path_key)
        if node.loaded == LOADED_ERROR:
            raise LoadFailed(f"节点 {expected_path_key} 加载失败")
        return node.loaded

    def children(self, parent_path_key: str) -> list[VisibleNode]:
        page = self._page
        parent = page.locator(_selector(parent_path_key))
        # 仅直接子 group，避免把深层后代混进来。
        group = parent.locator(':scope > [role="group"]')
        items = group.locator(':scope > [role="treeitem"]')
        result: list[VisibleNode] = []
        for index in range(items.count()):
            item = items.nth(index)
            path_key = item.get_attribute("data-path")
            result.append(
                VisibleNode(
                    node_id=item.get_attribute("data-node-id"),
                    name=item.locator(".row > span").first.inner_text().strip(),
                    kind=item.get_attribute("data-kind"),
                    path_key=path_key,
                    loaded=item.get_attribute("data-loaded"),
                    target=item.get_attribute("data-target"),
                    file_id=(item.locator("[data-file-id]").first.get_attribute("data-file-id")
                             if item.locator("[data-file-id]").count() else None),
                )
            )
        return result

    def close(self) -> None:
        return None


def open_driver(url: str, headless: bool = True) -> tuple[Any, Any, PlaywrightDriver]:
    """启动 Chromium 并打开门户，返回 (playwright, browser, driver)，由调用方关闭。"""
    from playwright.sync_api import sync_playwright

    runner = sync_playwright().start()
    browser = runner.chromium.launch(headless=headless, args=["--no-sandbox"])
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page.goto(url)
    page.wait_for_selector(_selector("/root"))
    return runner, browser, PlaywrightDriver(page)
