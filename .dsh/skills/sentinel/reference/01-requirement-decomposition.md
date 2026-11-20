# 01 · 需求拆解（RP 可判定契约）

## 输入分支

1. **有需求文档 / 缺陷管理平台单据链接（`issue_url`，备注：可忽略/可自行接入，用于增加风险分析的业务关联准确性）**：
   - 提取需求正文与验收标准，拆解为可判定的需求点（`RP1`、`RP2`...）。
   - 正文为空、冲突或与 diff 无共同锚点时，记录 `requirement_evidence_missing` / `requirement_evidence_conflict` / `requirement_diff_mismatch`，降级为 `MR + diff` 推导。
2. **仅代码变更（PR/MR/commit-only，无 `issue_url`）**：
   - 从 PR/MR 标题、描述、commit message 与 diff 提取变更意图，标注来源为 `MR + diff`，需求覆盖矩阵记 `not_applicable`。

## RP 可判定契约（每条 RP 必填字段）

| 字段 | 必填 | 含义 |
|---|---|---|
| `rp_id` | 是 | `RP1`…`RP<n>`，本轮内唯一稳定编号 |
| `expected_behavior` | 是 | 一句可观察的行为预期（UI 变化 / 接口返回 / 状态或产物变化） |
| `observable_surface` | 是 | 可观察面：`ui` / `api` / `file` / `log` / `none` |
| `acceptance_anchor` | 是 | 需求侧事实锚点或 `MR + diff` 来源说明 |
| `verifiability` | 是 | `observable`（可判定）/ `unverifiable`（不可判定 + 具体原因） |
| `scope_flag` | 否 | `in_scope`（默认）/ `deferred`（须有明确分期依据） |

## 产出与过程证据

- 先运行 `python ci/sentinel_scaffold.py --story-id <id> --code-diff-type <mr|commit|ci|branch>` 初始化报告目录与 `summary.md`。
- 写入 `01-requirement-decomposition.md`。
- 记录过程证据：`python ci/sentinel_evidence.py append --report-dir <报告目录> --step-id decompose_requirement --evidence-key ctx:requirement --category context --verification-method file --strength definitive --polarity supporting --fact-summary "需求拆解与RP契约落盘" --artifact 01-requirement-decomposition.md`
