# 00 · 执行守卫（防幻觉 / 防漂移 / 防超请求）

本文件与 9 步状态机同级。**违反任一守卫项 = 该步 `blocked`**。

## G1 双闸机制（每步）

- **机器闸**：本步必需产物存在且非空 + 必需字段出现 + 计数自洽（如 `ci/sentinel_ledger.py`、`ci/sentinel_test_design.py`、`ci/sentinel_evidence.py`）。
- **声明闸**：`summary.md` 记录 `gate: pass/fail`、机器闸命令、输出摘要、本步回合数。
- 两者不一致时以机器闸为准，并记入 `summary.md::guard_conflict`。
- **无头收尾闸（H7）**：流程结束必须输出单行 `SENTINEL_FINAL: status=<success|partial|blocked|failed> steps_done=<n>/9 report_dir=<相对路径|none> reason=<一句话>`。

## G2 幻觉三查（每步落盘后）

1. **证据存在性**：产物中出现的 `文件::符号`、测试文件路径、报告路径必须真实存在；不存在的记入 `unverified[]` 并降级为 `hypothesis_only`。
2. **命令可复现性**：所有「已执行 / 已验证 / 已消除」结论必须对上本轮真实调用记录（JUnit XML / 命令输出）；无记录的记入 `no_call_record[]` 并删除结论。
3. **计数自洽**：由 `ci/sentinel_ledger.py` 与 `ci/sentinel_test_design.py` 校验，严禁手工填 0 或凑数。

## G3 计数自洽公式

| 步骤 | 公式 |
|---|---|
| 2 | `dedup_candidates == filtered_content_empty + filtered_content_unrelated + filtered_status + filtered_stale + filtered_module + filtered_unmappable + filtered_unfalsifiable + excluded_weak + candidate_overflow + adopted_strong + adopted_medium + review_pending` |
| 5 | `风险总数 = 单测消除 + 单测+真机 + 真机消除 + 接受类（含 ignored_unreachable_count） + 未消除`；`device_required[]` 与 `.cache/ledger-out.json::device_required` 逐字一致 |
| 6 | 设计用例数 == 机器执行记录条目数；由 `ci/sentinel_test_design.py seal` 派生 `verified_coverage[]` |
| 7 | `### R<n>` 集合 == 步骤 5 `device_required[]`；每条具备前提核对 + 操作记录 + before/after 截图 + `verdict` 三态 |
| 9 | `total_count = pending_count + covered_count`；`covered_count = #(items eliminated/ignored) + ignored_unreachable.count` |

## G4 防漂移四条

1. **步骤锁**：进入第 N 步前，第 N-1 步 gate 必须已记录在 `summary.md`。
2. **落盘白名单**：工作区只允许写入 `report/sentinel/<story_id>+<timestamp>/` 与 `.dsh/` 知识/记忆目录；被测产品仓只允许写入本轮新建测试文件（步骤 9 清理并核对 `git status --porcelain`）。
3. **编号不可变**：`R<n>`、`T<n>`、`batch-<nn>`、`step_id` 一经落盘不得重排或复用。
4. **范围锁**：不得修改被测仓生产源码与测试框架配置。

## G6 红类型判定表

| 现象 | 判定 | 处理 |
|---|---|---|
| `Cannot find module`、命令不存在、`No test files found`、收集期崩溃、无 JUnit 测试汇总 | 环境红 | 修复测试运行命令或 mock 后重跑；**不得计入缺陷或有效对抗红** |
| 输出含测试汇总且 `failed > 0`，含 `AssertionError` 等业务断言差异 | 断言红 | 可作有效对抗红或缺陷证据，须保留原始输出与 JUnit XML |
