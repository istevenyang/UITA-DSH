# 05 · 风险路由与账本推导

将每条风险 `R<n>` 路由至验证手段，写入 `.cache/risk-ledger.json` 与 `05-risk-routing.md`：

| 风险类型 | 路由（`route`） |
|---|---|
| 纯逻辑 / 状态转移 / 分支计算 | `unit`（行为级单测） |
| 平台差异 / 核心链路需双保险 | `unit+device`（单测 + 真机） |
| 纯 UI 视觉 / 跨窗口交互 / 需求覆盖缺口（`requirement_gap`） | `device`（真机验证） |
| 历史已验证产品事实 / 低影响且界面不可感知 | `accept`（接受类） |
| 遗留风险真实场景不可达（经步骤 4 四步法复核通过） | `ignore`（写入 `ignored_unreachable[]`） |

## 确定性账本推导与步骤 6 初始化（必跑）

1. 运行风险账本推导器：
   ```bash
   python ci/sentinel_ledger.py risk \
     --ledger <报告目录>/.cache/risk-ledger.json \
     --out <报告目录>/.cache/ledger-out.json
   ```
   将输出的 `device_required: [...]` 字面量原样写入 `05-risk-routing.md`（全文唯一一处赋值）。
2. 初始化步骤 6 单测目标与意图骨架：
   ```bash
   python ci/sentinel_test_design.py init --report-dir <报告目录>
   ```
