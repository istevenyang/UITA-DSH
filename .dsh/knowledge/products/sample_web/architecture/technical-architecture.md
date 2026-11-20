# `sample_web` 技术架构（Technical Architecture）

描述 `sample_web` 被测前端页面与 UITA Web Engine + Rosetta 映射层的交互架构。

## 模块与调用链

| ID | 名称 | 概述 | 代码/配置锚点 | 证据/备注 |
| --- | --- | --- | --- | --- |
| ARCH-001 | Web Engine 与 Rosetta 映射解耦层 | TCS 用例通过语义化别名访问 DOM，选择器收敛在 `rosetta_map.json` | `apps/sample_web/rosetta_map.json` | 避免在 TCS 中硬编码脆弱选择器 |

<!-- kb:auto:start id="five-dim-technical-architecture" -->
## 自动沉淀技术架构（勿手改本块）

<!-- 由 `ci/sentinel_kb_writeback.py` 自动维护 -->
<!-- kb:auto:end id="five-dim-technical-architecture" -->
