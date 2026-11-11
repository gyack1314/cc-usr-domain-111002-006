"""四类结果的落盘写入器，全部使用 JSONL/JSON，字段可互相 JOIN。"""
from __future__ import annotations

import json
from pathlib import Path

from .crawler import CrawlConfig, CrawlResult


def write_outputs(result: CrawlResult, config: CrawlConfig, out_dir: str) -> dict[str, str]:
    """写出 edges/files/paths/coverage 四类结果，返回文件路径映射。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths_file = out / "path-instances.jsonl"
    edges_file = out / "catalog-edges.jsonl"
    files_file = out / "file-index.jsonl"
    coverage_file = out / "coverage.json"

    with paths_file.open("w", encoding="utf-8") as handle:
        for item in result.paths.values():
            handle.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")
    with edges_file.open("w", encoding="utf-8") as handle:
        for edge in result.edges.values():
            handle.write(json.dumps(edge.to_dict(), ensure_ascii=False) + "\n")
    with files_file.open("w", encoding="utf-8") as handle:
        for record in result.files.values():
            handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
    with coverage_file.open("w", encoding="utf-8") as handle:
        json.dump(result.coverage(config).to_dict(), handle, ensure_ascii=False, indent=2)

    return {
        "paths": str(paths_file),
        "edges": str(edges_file),
        "files": str(files_file),
        "coverage": str(coverage_file),
    }
