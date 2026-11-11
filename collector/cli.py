"""命令行入口：crawl / resume / retry。

示例
----
有限预算采集并留检查点::

    python -m collector crawl http://127.0.0.1:8090 \
        --out out/run1 --checkpoint out/run1/checkpoint.json --budget 2

从检查点继续（保留已确认路径，只处理剩余栈）::

    python -m collector resume http://127.0.0.1:8090 \
        --checkpoint out/run1/checkpoint.json --out out/run1 --budget 10

只重试加载失败的指定分支，其他路径原样保留::

    python -m collector retry http://127.0.0.1:8090 \
        --checkpoint out/run1/checkpoint.json --out out/run1 \
        --retry-path /root/south
"""
from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

from .crawler import CrawlConfig, Crawler, _Frame, _instance_id
from .driver import open_driver
from .writer import write_outputs


def _common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("url", help="门户入口 URL（仅通过可见 DOM 采集）")
    parser.add_argument("--out", required=True, help="四类结果输出目录")
    parser.add_argument("--checkpoint", help="检查点 JSON 路径（建议与 --out 同目录）")
    parser.add_argument("--max-depth", type=int, default=None, help="最大路径深度，root 为 0")
    parser.add_argument("--budget", type=int, default=None, help="展开节点预算")
    parser.add_argument("--retries", type=int, default=2, help="加载失败的同分支重试次数")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口（默认无头）")


def _load_crawler(driver, args: argparse.Namespace) -> Crawler:
    config = CrawlConfig(
        max_depth=args.max_depth,
        expand_budget=args.budget,
        retry_attempts=args.retries,
        checkpoint_path=args.checkpoint,
    )
    crawler = Crawler(driver, config)
    return crawler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="懒加载档案目录采集器")
    sub = parser.add_subparsers(dest="command", required=True)
    p_crawl = sub.add_parser("crawl", help="从入口开始全新采集")
    _common_options(p_crawl)
    p_resume = sub.add_parser("resume", help="从检查点继续未完成采集")
    _common_options(p_resume)
    p_retry = sub.add_parser("retry", help="只重试加载失败分支，保留其他已确认路径")
    _common_options(p_retry)
    p_retry.add_argument("--retry-path", action="append", default=[],
                         help="只重试指定 id 路径（可多次传入）；缺省重试全部 load-error 分支")
    p_verify = sub.add_parser("verify", help="从入口重建并核对已保存路径，识别移动/失效目录")
    _common_options(p_verify)

    args = parser.parse_args(argv)
    runner = browser = driver = None
    try:
        runner, browser, driver = open_driver(args.url, headless=not args.headed)
        crawler = _load_crawler(driver, args)

        if args.command in ("resume", "retry", "verify"):
            if not args.checkpoint or not Path(args.checkpoint).is_file():
                parser.error("resume/retry/verify 需要已存在的 --checkpoint 文件")
            crawler.load_checkpoint()
            if args.command == "retry":
                _prepare_retry(crawler, args.retry_path)

        if args.command == "verify":
            result = crawler.verify_saved_paths()
        else:
            result = crawler.run()
        files = write_outputs(result, crawler.config, args.out)
        coverage = result.coverage(crawler.config)
        print(json.dumps({
            "command": args.command,
            "complete": coverage.complete,
            "expansions_used": coverage.expansions_used,
            "path_instances": coverage.path_instance_count,
            "edges": coverage.edge_count,
            "files": coverage.file_count,
            "cycles": coverage.cycle_count,
            "denied": coverage.denied_count,
            "load_errors": coverage.error_count,
            "needs_review": coverage.needs_review_count,
            "uncovered_branches": len(coverage.uncovered),
            "outputs": files,
        }, ensure_ascii=False, indent=2))
        if not coverage.complete:
            print("提示：覆盖不完整，存在未覆盖分支，不能视为目录已完整。")
        return 0
    finally:
        if browser is not None:
            browser.close()
        if runner is not None:
            runner.stop()


def _prepare_retry(crawler: Crawler, retry_paths: list[str]) -> None:
    """把失败分支从覆盖记录中摘除并重新入栈；其他路径与结果原样保留。"""
    # 命令行传入的是带前导 / 的路径键，统一去掉前导 / 得到实例 id 再比较。
    wanted = {path.lstrip("/") for path in retry_paths}
    remaining = []
    requeued = 0
    for branch in crawler.result.uncovered:
        if branch.reason != "load-error":
            remaining.append(branch)
            continue
        if wanted and branch.instance_id not in wanted:
            remaining.append(branch)
            continue
        # 重新入栈：恢复时由入口重新展开建立路径，不依赖任何 DOM 引用。
        crawler._stack.append(_Frame(
            path_key="/" + "/".join(branch.id_path),
            id_path=list(branch.id_path),
            visible_path=list(branch.visible_path),
            depth=len(branch.id_path) - 1,
            via_chain=[],
        ))
        # 去掉该分支的未覆盖登记与去重键，允许本次重新登记。
        crawler._uncovered_keys.discard(
            branch.instance_id + "|load-error"
        )
        requeued += 1
    crawler.result.uncovered = remaining
    # 同名的路径实例终止原因清掉，等待本次结果重新写入。
    for frame in list(crawler._stack):
        instance_id = _instance_id(frame.id_path)
        existed = crawler.result.paths.get(instance_id)
        if existed is not None and existed.terminal_reason == "load-error":
            crawler.result.paths[instance_id] = replace(
                existed, terminal_reason=None, detail=None
            )
    crawler.result.notes.append(f"重试 {requeued} 个加载失败分支，其他已确认路径保持不变")


if __name__ == "__main__":
    raise SystemExit(main())
