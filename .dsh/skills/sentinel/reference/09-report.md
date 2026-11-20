# 09 · 报告生成、证据归并与清理

## 1. 对账与产物生成

1. **复核单测封存与风险账本对账**：
   ```bash
   python ci/sentinel_test_design.py seal --report-dir <报告目录>
   python ci/sentinel_ledger.py risk \
     --ledger <报告目录>/.cache/risk-ledger.json \
     --out <报告目录>/.cache/ledger-out.json \
     --device-records <报告目录>/07-device-verification.md
   ```
2. **生成 `utest-defects.json` 与 `risk-report.html`**：
   - `utest-defects.json`：包含 `summary`（含 `risk_ledger`、`eliminated_counts`、`ignored_unreachable`、`uneliminated_snapshot`）与 `defects[]`（断言红或真机 fail 的真实缺陷）。
   - `risk-report.html`：包含固定四模块（M1 结论徽章与依据 / M2 遗留风险简述 / M3 风险漏斗与单测详情 / M4 真机验证信息）。
3. **归并并校验过程证据清单 `evidence-manifest.json`**：
   ```bash
   python ci/sentinel_evidence.py append --report-dir <报告目录> --step-id report \
     --evidence-key report:artifacts --category conclusion --verification-method command \
     --strength definitive --polarity supporting --fact-summary "报告与缺陷账本同目录齐备" \
     --artifact utest-defects.json --artifact risk-report.html

   python ci/sentinel_evidence.py merge --report-dir <报告目录> \
     --run-key "<source_ref>#<timestamp>" --code-revision <40位SHA> \
     --knowledge-version profile-v1 --model <model_name>

   python ci/sentinel_evidence.py check --report-dir <报告目录>
   ```

## 2. 临时资产清理与收尾

- 将本轮在被测仓新建的临时测试文件归档至 `<报告目录>/tests/` 后，清理被测仓临时文件与临时 worktree，核对 `git status --porcelain` 干净并记入 `summary.md::burn_list`。
- 将 `summary.md` 的 9 行步骤状态全部更新为终态（`done` / `blocked` / `skipped` / `failed`），输出单行终局令牌：
  ```
  SENTINEL_FINAL: status=<success|partial|blocked|failed> steps_done=9/9 report_dir=<报告目录> reason=<一句话总结>
  ```
