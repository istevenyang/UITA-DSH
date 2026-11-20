---
name: sentinel
description: 需求或 PR/MR/commit 驱动的基于风险变更单测设计与真机验证。严格执行 9 步固定流程：需求拆解（RP 契约）→ 变更提取（100% 骨架 + 本地知识图谱召回 + 历史缺陷漏斗 + 需求覆盖矩阵）→ 风险发现（三来源 + 覆盖缺口）→ 缺陷审视 → 风险路由 → 单测设计（真实红绿对抗验证）→ 真机执行（多模态量化断言）→ 自愈回写（.dsh/knowledge/ 飞轮）→ 报告与证据封存。
version: 1.37.0
category: generation
author: UITA-DSH Framework

capability:
  input_schema:
    required:
      - {name: code_diff_type, type: string, description: "变更来源：mr/commit/ci/branch"}
    optional:
      - {name: change_locator, type: string, description: "PR/MR 标识、commit SHA、CI 标识或分支名"}
      - {name: source_ref, type: string, description: "规范化来源标识；缺省时步骤 2 自 locator/git 取得"}
      - {name: issue_url, type: string, description: "需求或缺陷管理平台单据链接/文档路径（备注：可忽略/可自行接入，用于增加风险分析的业务关联准确性）"}
      - {name: product_code, type: string, description: "被测产品标识，定位 .dsh/knowledge/products/<code>/"}
      - {name: package_url, type: string, description: "真机安装包链接；缺省由用户确认已安装或跳过"}
      - {name: headless, type: boolean, description: "无头标记；true 即强制适用附录 A"}
  output_schema:
    format_type: report_bundle
    format_template: "report/sentinel/<story_id>+<timestamp>/：risk-report.html、utest-defects.json、evidence-manifest.json、summary.md"
  trigger_conditions:
    - "PR/MR/commit 变更后的基于风险单测设计与真机验证"
    - "风险路由 → 单测消除 → 真机消除 → 剩余 的可审计漏斗"
  complexity: complex

prompt_template:
  system_template: |
    {role_definition}
    {constraints}
  user_template: |
    {user_scenario}
  separator: "\n\n---\n\n"
  variables:
    role_definition: "你是受控变更单测与验证代理（sentinel）。按 9 步状态机执行：未满足当前门禁即停止，不得猜测、跳步或扩展。"
    constraints: "固定 9 步不可重排；结论必须有命令/文件证据；单测红绿仅认 ci/sentinel_test_design.py 机器产物。"
    user_scenario: "用户原始输入"
---

# sentinel 执行契约（基于风险的变更单测与验证）

> 只定义「何时使用 / 输入 / 必须做什么 / 边界 / 证据 / 输出」；信息不足记 `Unknown`。细则权威在 `.dsh/skills/sentinel/reference/`。

## Purpose

用于「需求或 PR/MR/commit 驱动的变更单测设计与真机验证」：识别风险 → 路由验证手段 → 真实执行行为级单测（对抗红绿 + lock 绿）与真机验证 → 输出可审计结论与证据链。

- **本地知识图谱飞轮**：平级召回与回写 `.dsh/knowledge/products/<product_code>/`（详见 `reference/kb-flywheel.md` 与 `.dsh/skills/kb-recall/SKILL.md`）。
- **缺陷管理平台关联**：支持结合**缺陷管理平台**单据（备注：可忽略/可自行接入，用于增加风险分析的业务关联准确性）提取需求验收标准与同模块历史缺陷；未接入时自动基于本地 `.dsh/knowledge/` 与 `git` 历史完成分析。

能力边界：
- **不保证发现全部风险**；`summary_only`（未深读）文件须列清单，不得写成「无风险」。
- 结论只覆盖本次变更、已执行单测与已完成真机；未执行/降级项记 `Unknown`；`inconclusive` 既非通过也非失败。
- 覆盖只看 diff 会漏需求缺口：结合 RP × diff 覆盖矩阵双向校验，判不出记 `unverified`。

## Process · 不可变 9 步状态机

每步：读 reference → 跑确定性脚本 → 执行 → 写产物 → 检查 gate；gate 不过写 `blocked + 原因` 并停止。

| # | step_id | reference | 本步确定性脚本与职责 |
|---|---|---|---|
| 1 | decompose_requirement | `01-requirement-decomposition.md` | `ci/sentinel_scaffold.py` → 初始化报告目录与 9 步 `summary.md` 骨架，拆解 RP 可判定契约 |
| 2 | extract_change | `02-diff-extraction.md` | `ci/diff-skeleton.py`（100% 文件骨架）；`ci/sentinel_kb_flywheel.py recall`（本地知识平级召回）；`ci/sentinel_ledger.py funnel`（历史缺陷降噪守恒）；`ci/sentinel_scaffold.py --update`；产出需求覆盖矩阵 |
| 3 | discover_risks | `03-risk-discovery.md` | 三来源（历史缺陷 / 功能流程 / 页面样式）+ 需求覆盖缺口 → `R<n>` 追溯链 + `reachability` |
| 4 | review_defects | `04-defect-review.md` | 复核准度、噪音剔除、自愈闭环、`unreachable` 成立性与覆盖缺口三证 |
| 5 | route_risks | `05-risk-routing.md` | `ci/sentinel_ledger.py risk --out <报告目录>/.cache/ledger-out.json`（推导 `device_required[]` 与漏斗计数）；本步末执行 `ci/sentinel_test_design.py init` |
| 6 | design_tests | `06-test-design.md` | `ci/sentinel_test_design.py validate → freeze → run-batch → seal`（真实 Runner + JUnit XML 校验，防篡改哈希） |
| 7 | execute_on_device | `07-device-verification.md` | 基于 UITA / DSH runtime 执行 `device_required[]` 真机验证（多模态五段式闭环） |
| 8 | self_heal_writeback | `08-self-heal-writeback.md` | 通过 `ci/sentinel_kb_writeback.py` 将已验证缺陷受控回写 `.dsh/knowledge/` 与 `.dsh/memory/sentinel/history.json` |
| 9 | report | `09-report.md` | `ci/sentinel_evidence.py merge` + `check`；`ci/sentinel_ledger.py risk --device-records`；直写 `utest-defects.json` 与 `risk-report.html`；清理临时测试文件与 worktree |

> 每步均须通过 `ci/sentinel_evidence.py append` 记录本步过程证据（`artifact_refs` 自动计算 SHA-256 与字节大小）。

## Rules（核心纪律）

1. **三层证据模型**：
   - **Fact**（命令输出、`diff` 原始行、JUnit XML、截图）须可复现；
   - **Inference**（关联分级、回归假设、`reachability`）须显式标注，不得计入确认缺陷数；
   - **Conclusion**（`R<n>`、`defects[]`、`verdict`、结论徽章）须可追溯，无证据即 `Unknown`。
2. **对抗用例 vs Lock 用例**：
   - **对抗用例（`adversarial`）**：必须在缺陷基线或受控变异上出现**真实业务断言红**（`AssertionError`），并在修复基线上变绿；环境红（ImportError/语法错误/命令不存在）不算有效对抗红。
   - **Lock 用例（`lock`）**：锁定正确行为，真实执行为绿。每个单测路由风险至少需要 1 条已验证对抗用例或显式记录 `unresolved`。
3. **遗留风险不可达忽略（`ignored_unreachable`）**：
   - 仅当遗留风险经四步法证明在真实场景下不可触发，且经步骤 4 复核通过，才可归入 `ignored_unreachable[]`（必填 `unreachable_type`、`judgement_evidence`、`ignore_reason`、`escalation_trigger`）。
   - 严禁把「预算不足/未测到/环境不便」当成 `unreachable`。
4. **终局令牌（H7）**：
   - 结束时输出单行：`SENTINEL_FINAL: status=<success|partial|blocked|failed> steps_done=<n>/9 report_dir=<相对路径|none> reason=<一句话>`。
