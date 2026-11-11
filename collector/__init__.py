"""懒加载档案目录采集模块。

公开入口：
    CrawlConfig / Crawler   采集配置与引擎（依赖 PortalDriver 协议）
    open_driver             启动 Playwright 连接本地合成门户
    write_outputs           输出四类可关联结果
"""
from .crawler import CrawlConfig, Crawler, CrawlResult
from .driver import (
    LoadFailed,
    NodeMoved,
    PlaywrightDriver,
    PortalDriver,
    VisibleNode,
)
from .models import Coverage, Edge, FileRecord, PathInstance, TerminatedBranch, UncoveredBranch
from .writer import write_outputs

__all__ = [
    "CrawlConfig",
    "Crawler",
    "CrawlResult",
    "PortalDriver",
    "PlaywrightDriver",
    "VisibleNode",
    "NodeMoved",
    "LoadFailed",
    "open_driver",
    "write_outputs",
    "Coverage",
    "Edge",
    "FileRecord",
    "PathInstance",
    "TerminatedBranch",
    "UncoveredBranch",
]
