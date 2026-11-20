# 07 · 真机执行（UITA / DSH 多模态验证）

真机执行仅针对步骤 5 产出的 `device_required[]`（影响业务或界面可感知、且单测未消除的风险）。若 `device_required: []` 为空，本步直接记录无待真机项并过闸。

## 1. 生命周期与驱动纪律

- 被测应用启动、观测、操作、关闭一律通过 UITA runtime（`uita` / `dsh`）完成。
- 每条 `R<n>` 按步骤 5 的「前提 / 步骤 / 预期」三要素执行，在 `07-device-verification.md` 产出 `### R<n> · <标题>` 段落（集合必须与 `device_required[]` 严格一致）。

## 2. 逐步五段式闭环

每个操作步记录：
1. **before 截图**：`device/R<n>-s<k>-before.png`
2. **判读与定位**：目标元素或可见范围 bbox 中心坐标
3. **操作执行**：通过引擎执行点击/输入/交互
4. **after 截图**：`device/R<n>-s<k>-after.png`（必须与 before 不同）
5. **比对判定**：输出 `step_verdict: pass | fail | inconclusive` 及量化依据，汇总得出该风险的 `verdict: pass | fail | inconclusive`。

若运行环境无图形会话或缺少被测包无法启动，按合规 `device_unexecuted` 记录原因码与阻塞证据，继续步骤 8/9 产出 `partial` 报告。
