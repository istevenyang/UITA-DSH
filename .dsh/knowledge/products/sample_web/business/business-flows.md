# `sample_web` 业务流转（Business Flows）

描述 `sample_web` 从页面加载、表单输入到状态回显的主干与异常流转。

## 主干与异常流转

| ID | 名称 | 概述 | 代码/配置锚点 | 证据/备注 |
| --- | --- | --- | --- | --- |
| FLOW-001 | 正常关键词提交与回显 | 打开首页 → 输入文本 → 点击提交 → 状态栏展示提交内容 | `cases/sample_web/smoke_search.tcs` | P0 主干链路 |
| FLOW-002 | 空输入提交防御 | 未输入内容直接点击提交 → 保持原状态或提示必填 | `apps/sample_web/rosetta_map.json` | P1 边界流转 |

## 关系表

| 源实体 | 目标实体 | 关系类型 | 证据 |
| --- | --- | --- | --- |
| `uita:doc:.dsh/knowledge/products/sample_web/business/business-flows.md` | `uita:doc:.dsh/knowledge/products/sample_web/architecture/technical-endpoints.md` | `depends_on` | 业务流转依赖 DOM 选择器与前端状态更新契约 |

<!-- kb:auto:start id="five-dim-business-flows" -->
## 自动沉淀业务流转（勿手改本块）

<!-- 由 `ci/sentinel_kb_writeback.py` 自动维护 -->
<!-- kb:auto:end id="five-dim-business-flows" -->
