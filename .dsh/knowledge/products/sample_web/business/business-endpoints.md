# `sample_web` 业务端点（Business Endpoints）

记录 `sample_web` 示例站点的核心页面入口、交互控件与业务反馈端点。

## 入口与弹窗

| ID | 名称 | 概述 | 代码/配置锚点 | 证据/备注 |
| --- | --- | --- | --- | --- |
| EP-001 | 首页欢迎面板 | 展示示例欢迎标题与操作表单入口 | `apps/sample_web/rosetta_map.json#home.heading` | 基础冒烟必测入口 |
| EP-002 | 文本输入与提交表单 | 支持输入关键词并触发结果区域回显 | `apps/sample_web/rosetta_map.json#home.submit_btn` | 校验非空输入与回显一致性 |

## 关系表

| 源实体 | 目标实体 | 关系类型 | 证据 |
| --- | --- | --- | --- |
| `uita:doc:.dsh/knowledge/products/sample_web/business/business-endpoints.md` | `uita:doc:.dsh/knowledge/products/sample_web/business/business-flows.md` | `triggers` | 首页提交按钮触发关键词提交与结果回显业务流 |
| `uita:doc:.dsh/knowledge/products/sample_web/business/business-endpoints.md` | `tcs:case:cases/sample_web/smoke_search.tcs` | `tested_by` | 冒烟用例覆盖首页欢迎面板与表单交互 |

<!-- kb:auto:start id="five-dim-business-endpoints" -->
## 自动沉淀业务端点（勿手改本块）

<!-- 由 `ci/sentinel_kb_writeback.py` 自动维护 -->
<!-- kb:auto:end id="five-dim-business-endpoints" -->
