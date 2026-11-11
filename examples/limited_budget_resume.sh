#!/usr/bin/env bash
# 从有限预算继续采集的端到端示例（仅访问本地合成门户，不连真实企业文档库）。
#
# 场景：本次只给 1 次展开预算，采集器必须登记未覆盖分支、保留检查点；
#       随后用更大预算 resume，从入口重建路径并补全，已确认路径不丢失、不重复。
set -euo pipefail

PORT="${1:-8155}"
OUT="${2:-/tmp/catalog-demo}"
BASE="http://127.0.0.1:${PORT}"

rm -rf "${OUT}"
mkdir -p "${OUT}"

# 1) 启动本地合成门户（后台），脚本退出时清理。
python3 -m portal --port "${PORT}" >"${OUT}/portal.log" 2>&1 &
PORTAL_PID=$!
trap 'kill ${PORTAL_PID} 2>/dev/null || true' EXIT
sleep 1

echo "================ 第 1 次：预算=1（必然不完整） ================"
python3 -m collector crawl "${BASE}" \
  --out "${OUT}/run1" --checkpoint "${OUT}/run1/checkpoint.json" \
  --budget 1 || true

echo
echo "================ 第 2 次：resume，预算=10（从入口重建路径） ================"
python3 -m collector resume "${BASE}" \
  --out "${OUT}/run2" --checkpoint "${OUT}/run1/checkpoint.json" \
  --budget 10

echo
echo "================ 四类可关联结果 ================"
echo "-- 目录边（前 3 条）--"; head -3 "${OUT}/run2/catalog-edges.jsonl"
echo "-- 文件内容索引 --";       cat "${OUT}/run2/file-index.jsonl"
echo "-- 覆盖状态 --";           cat "${OUT}/run2/coverage.json"
