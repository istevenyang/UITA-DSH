# 报告结构规范（`risk-report.html` 四模块契约）

`risk-report.html` 固定由四个顶层模块组成，顺序不可变：

1. **M1 · 结论横幅（Conclusion Banner）**
   - 四态结论徽章：`通过`（`pending_count == 0` 且无真实缺陷）/ `有条件通过（待真机/人工确认）` / `阻塞` / `未通过（发现真实缺陷）`。
   - 判定依据行（`.cbasis`）：展示 `单测 N/M 通过（未通过 a · 跳过 b · 未执行 c）`，数值与 `utest-defects.json::summary` 严格同源；当 `pending_count > 0` 时逐条点名待确认项业务描述。
2. **M2 · 本次改动与遗留风险简述（Executive Brief）**
   - 简述本次变更范围与核心结论；
   - 按高/中/低风险分组列出未消除风险（`pending_count`），并汇总「已覆盖（行为覆盖）M 项」。
3. **M3 · 详细分析（Risk Funnel & Unit Test Evidence）**
   - M3.1 变更文件骨架与深读覆盖率；
   - M3.2 风险漏斗表（风险总数 → 噪音剔除 → 行为级单测覆盖 → 真机消除 → 低风险略过/不可达忽略 → 未消除）；
   - M3.3 需求覆盖矩阵与覆盖未验证清单；
   - M3.4 行为级单测设计与红绿对抗证据表（`T<n>`、关联 `R<n>`、GIVEN/WHEN/THEN、defective 红 / correct 绿 JUnit 结果）。
4. **M4 · 真机验证信息（Device Verification）**
   - 展示 `device_required[]` 中每条风险的前置核对、逐步操作与前后截图对比、量化判定结论（`pass` / `fail` / `inconclusive`）。
