# 02 · 变更提取（100% 骨架 + 需求覆盖矩阵 + 历史缺陷漏斗）

## 1. 提取全量 diff 与 100% 文件骨架

1. 使用 `git diff` / `git show` 获取全量 unified diff，落盘至 `<报告目录>/.cache/diff.raw.txt`（只拉取一次）。
2. 运行确定性骨架脚本生成 100% 文件覆盖表：
   ```bash
   python ci/diff-skeleton.py <报告目录>/.cache/diff.raw.txt \
     --md <报告目录>/02-diff-file-list.md \
     --json <报告目录>/.cache/diff-skeleton.json \
     --change-id "<source_ref>"
   ```
3. 回填 `source_ref`：
   ```bash
   python ci/sentinel_scaffold.py --update --report-dir <报告目录> \
     --source-ref "<source_ref>" --code-diff-type <mr|commit|ci|branch>
   ```
4. **有限深读**：文件数 ≤40 全量深读；>40 按「入口链路符号 → 高风险目录 → 最大 hunk → 符号待定」优先级深读 ≤60 个文件，其余标 `evidence_depth: summary_only`（不得写成「无风险」）。

## 2. 需求覆盖矩阵（RP × diff）

当步骤 1 存在独立需求源且含 `verifiability=observable` 的 RP 时，逐条判定五态（写入 `.cache/requirement-coverage.json` 并追加至 `02-diff-file-list.md::## 需求覆盖矩阵`）：
- `covered`：diff 中找到实现该行为的 `文件::符号` 且逻辑一致。
- `partial`：仅覆盖一部分（写明已覆盖与缺失部分）。
- `missing`：diff 与基线均无实现（须给出检索命令与 0 命中证据）。
- `deferred`：需求明确分期。
- `unverified`：检索受限，无法确认。

## 3. 本地知识图谱平级召回与历史缺陷降噪漏斗

1. **平级召回本地知识库**（`.dsh/knowledge/products/<product>/`）：
   调用 `kb_recall` 工具或运行：
   ```bash
   python ci/sentinel_kb_flywheel.py --product-code <product> recall --query "<变更模块与符号关键词>" --out <报告目录>/.cache/local-recall.json
   ```
2. **检索同模块历史缺陷或已知陷阱**：结合 `.dsh/knowledge/products/<product>/utest-rules/04-known-pitfalls.md`、`defects/<product>-defect-chain.md` 以及可选接入的**缺陷管理平台**（备注：可忽略/可自行接入，用于增加风险分析的业务关联准确性），落盘 `.cache/defect-funnel.json` 后运行：
```bash
python ci/sentinel_ledger.py funnel --funnel <报告目录>/.cache/defect-funnel.json \
  --md <报告目录>/.cache/defect-funnel.md
```
将计数与候选分析写入 `02-related-defects.md`。
