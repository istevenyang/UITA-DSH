---
name: ui-automation
description: 使用 DSH 与 TCS (TestCaseScript) 编写、调试与固化跨平台 UI 自动化测试用例的标准技能规程。
---

# UI Automation Skill (DSH + TCS / TestCaseScript)

## 核心流程
1. **环境准备**：调用 `app_prepare` 启动或附着被测应用（如 `sample_web`）。
2. **先观察后行动**：调用 `observe_snapshot` 获取窗口状态、截图与可访问性摘要。
3. **语义优先定位**：优先使用 `apps/<app>/rosetta_map.json` 中登记的元素语义名调用 `locate` / `act_click` / `act_type`。
4. **三态断言验证**：每步交互后使用 `assert_exist` / `assert_text` 验证结果（`true` / `false` / `unknown`）。
5. **固化为确定性资产**：探索通过后将步骤导出为 Steps IR（`.ir.json`）并编译为 `.tcs` 用例，后续回归通过 `dsh replay` 或 `uita --mode runner` 零 LLM 成本执行。
