# 08 · 自愈回写

仅当历史缺陷或本轮发现的真实缺陷同时满足以下条件时，回写至本地知识库与记忆：
1. 已映射到明确的 `文件::符号` 与代码块功能；
2. 已有步骤 6 真实行为级单测红绿（`verified`）或步骤 7 真机验证证据；
3. 非环境错误、非弱关联假设。

## 回写动作

1. **知识沉淀（`kb:auto` 受控回写）**：通过 `kb_writeback` 工具或 `python ci/sentinel_kb_writeback.py --product-code <product_code> pitfall/defect-chain/five-dim` 将已验证缺陷形态与断言契约写入 `.dsh/knowledge/products/<product_code>/` 对应文件的 `<!-- kb:auto:start/end -->` 围栏区（递增 `rules-meta.json` 中的 `rules_version`，手写区字节零改动）。
2. **历史记忆追加**：将本轮 `source_ref`、风险计数、单测覆盖计数追加至 `.dsh/memory/sentinel/history.json`（不含敏感凭据或内部 token）。
3. 落盘 `08-self-heal-writeback.md` 并记录过程证据。
