# 懒加载档案目录采集模块（collector）

基于 Python + Playwright 的目录采集器，配合 `web/` 下的本地合成门户运行，**不访问真实企业文档库**。
它只读取用户可见的 DOM 状态并点击页面按钮，不调用 `/__state`、`/__control` 等运维入口，也不直接抓取 `data.json`。

## 要解决的问题

- 目录树折叠时子节点不在 DOM，必须真正展开才能读到子节点；
- 快捷入口让同一份文件出现在多个分类位置，需要保留用户实际看到的每一条路径；
- 不能因为“路径文本相同 / 名称相同 / 目标相同”就把不同实例混为一谈；
- 快捷引用可能形成环，需要按当前路径判断并有限结束，而不是全局见过一次就跳过；
- 无权节点只记录可见名称与不可读状态，不尝试获取隐藏子节点；
- 预算/深度受限、加载失败、目录移动都要可追溯，不能输出“目录已完整”。

## 身份模型：为什么不会混淆

- **节点身份 = 根到当前的 node-id 链**（`id_path`），实例 id 形如 `root/north/f1`。
- 同名兄弟目录 `north`、`south` 都叫“工程资料”，但 node-id 不同，因此是两个实例。
- 文件编号（`FILE-shared`）与节点 id（`f1`、`f2`）是两回事：两个目录中的同一文件产生
  **两条路径实例、两条 contains 边**，而文件内容索引按编号只归并为一条，内含两个 placement。
- **不做全局去重**：同一节点经另一合法目录入口（含快捷引用）到达时，仍产生新实例与边。
- 环只按**当前下行路径**判断：node-id 已在本路径祖先位置出现时，输出可追溯 reference 边并终止该分支，
  其他入口不受影响。

## 四类可关联结果

写入 `--out` 目录，字段之间可用实例 id / 文件编号 JOIN：

| 文件 | 内容 | 关联键 |
| --- | --- | --- |
| `path-instances.jsonl` | 路径实例：每次出现在某分类路径上的节点 | `instance_id`、`parent_instance_id`、`id_path` |
| `catalog-edges.jsonl` | 目录边：`contains`（父子）与 `reference`（快捷引用，含 `target_id_path`、`cycle`） | `parent_instance_id`、`child_instance_id`、`target_node_id` |
| `file-index.jsonl` | 文件内容索引：按文件编号归并，含全部 placement | `file_id`、`placements[].instance_id` |
| `coverage.json` | 覆盖状态：`complete`、预算用量、`uncovered`（未覆盖）、`terminated`（回环/无权有限终止） | 各分支 `instance_id` |

`coverage.complete` 只取决于 `uncovered`（预算/深度/加载失败/需核对）。回环与无权属于规则内的
**有限终止**，列在 `terminated` 中，不会把一次正常采集误判为不完整；只要存在 `uncovered`，就绝不声称目录完整。

## 命令用法

```sh
# 全新采集（可限深度/预算，留检查点）
python3 -m collector crawl http://127.0.0.1:8090 \
  --out out/run --checkpoint out/run/checkpoint.json --budget 2 --max-depth 5

# 从检查点继续：从入口按已保存 id 路径重新展开，不复活 DOM 引用
python3 -m collector resume http://127.0.0.1:8090 \
  --out out/run --checkpoint out/run/checkpoint.json --budget 20

# 只重试加载失败分支，其他已确认路径原样保留
python3 -m collector retry http://127.0.0.1:8090 \
  --out out/run --checkpoint out/run/checkpoint.json --retry-path /root/south

# 目录移动/目标失效核对：重建每条已保存路径并比对结构，保留旧路径证据并标记 needs-review
python3 -m collector verify http://127.0.0.1:8090 \
  --out out/run --checkpoint out/run/checkpoint.json --budget 20
```

从有限预算继续采集的完整示例：`examples/limited_budget_resume.py`（编程接口）与
`examples/limited_budget_resume.sh`（命令行）。

## 子树重建与节点失效的处理

门户在 `rebuild-tree` 阶段会整体重建 `#tree`，缓存的元素句柄会指向同名新节点。驱动因此：

- 不持有跨操作的 locator/element，每次都按 `data-path` 在当前 DOM 重新定位；
- 重新核对 `data-node-id` 与路径末段 id，对不上即抛 `NodeMoved`，标记 `needs-review`；
- 展开后等待节点 `data-loaded` 进入 `true`/`error` 终态，再读取子节点。

`moved-tree` 下目录被清空/移动时，`verify` 会发现“重新展开的子节点 ≠ 已保存路径证据”，
保留原路径与边作为证据并标记需核对，**不会把旧子节点补挂到任何同名目录**。

## 测试

```sh
python3 -m unittest discover -s tests -v
```

- `tests/test_portal.py`：合成门户自身行为（7 项）。
- `tests/test_collector.py`：真实浏览器端到端，覆盖共享文件双入口、回环有限结束、无权不展开、
  预算/深度、失败重试、子树重建重新定位、移动核对，并断言采集端不访问运维入口（9 项）。
- `tests/test_engine_logic.py`：内存假驱动的纯遍历逻辑，重点验证按当前路径判环、非全局去重（5 项）。
