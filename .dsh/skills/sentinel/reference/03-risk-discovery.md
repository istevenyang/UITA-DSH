# 03 · 风险发现（三来源 + 需求覆盖缺口）

## 四大风险来源

1. **① 历史缺陷回归**：结合 `02-related-defects.md` 与 `04-known-pitfalls.md` 中通过可证伪门槛的强/中关联候选，映射至本次 `文件::符号`。
2. **② 功能流程与边界**：梳理变更代码块的函数调用链、输入状态、异常分支、并发/时序、空值与边界条件。
3. **③ 页面样式与交互状态**：梳理 UI 组件映射、交互状态机与数据绑定；纯后端/逻辑变更标 `ui_surface: none`。
4. **④ 需求覆盖缺口（`risk_type=requirement_gap`）**：步骤 2 覆盖矩阵中 `missing` / `partial` 且三证齐全的 RP，转化为缺口风险 `R<n>`（默认 `reachability=ui_reachable`）。

## 最小追溯链与可达性判定

每条风险 `R<n>` 必须包含完整追溯链：
`RP/变更意图 → diff 批次 → 文件::符号 → 代码块功能 → 页面表现或 none → 历史缺陷或缺省 → R<n> → reachability → 建议路由`

| `reachability` | 含义 | 证据要求 |
|---|---|---|
| `ui_reachable` | 用户界面存在入口且可操作触达 | 给出页面/组件/路由/入口证据 |
| `device_reachable` | 无 UI 入口，但真机/进程/系统层可复现 | 给出真机或系统触发路径证据 |
| `unreachable` | 真实使用场景无法触发（如未接线的新增模块或静态死分支） | 逐项给出检索与静态不可达依据；严禁把「预算不足/未测到」标为 `unreachable` |

写入 `03-risk-discovery.md` 并通过 `ci/sentinel_evidence.py append` 记录证据。
