"""从有限预算继续采集的可复现示例（编程接口版）。

仅连接本地合成门户：先用 1 次展开预算采集（产生未覆盖分支与检查点），
再用更大预算从检查点恢复，从入口重建路径补全。运行：

    python3 examples/limited_budget_resume.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# 允许从工作区根目录直接运行本示例脚本。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright

from collector import CrawlConfig, Crawler, PlaywrightDriver
from portal.__main__ import running


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="catalog-demo-"))
    ckpt = str(work / "checkpoint.json")
    with running() as (base, _state):
        with sync_playwright() as runner:
            browser = runner.chromium.launch(headless=True, args=["--no-sandbox"])

            # ---- 第一次：预算 1，采集必然不完整 ----
            page = browser.new_page()
            page.goto(base)
            page.wait_for_selector('[data-path="/root"]')
            first = Crawler(
                PlaywrightDriver(page),
                CrawlConfig(expand_budget=1, checkpoint_path=ckpt),
            ).run()
            first_cov = first.coverage(CrawlConfig(expand_budget=1))
            print("第一次 complete =", first_cov.complete)
            print("未覆盖分支 =", [(u.instance_id, u.reason) for u in first.uncovered])
            page.close()

            # ---- 第二次：新页面（DOM 引用全部失效），从检查点恢复并放宽预算 ----
            page = browser.new_page()
            page.goto(base)
            page.wait_for_selector('[data-path="/root"]')
            cfg = CrawlConfig(expand_budget=10, checkpoint_path=ckpt)
            crawler = Crawler(PlaywrightDriver(page), cfg)
            crawler.load_checkpoint()
            result = crawler.run()
            page.close()
            browser.close()

    coverage = result.coverage(cfg)
    print("恢复后 complete =", coverage.complete)
    print("共享文件入口 =", [p["instance_id"] for p in result.files["FILE-shared"].placements])
    print("有限终止 =", [(t.instance_id, t.reason) for t in result.terminated])
    print("展开次数 =", coverage.expansions_used)
    out = work / "result"
    from collector import write_outputs
    files = write_outputs(result, cfg, str(out))
    print("结果目录 =", json.dumps(files, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
