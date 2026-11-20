# `sample_web` 技术端点（Technical Endpoints）

记录 `sample_web` 页面 DOM 定位锚点、路由契约与状态回显节点。

## 接口与信号契约

| ID | 名称 | 概述 | 代码/配置锚点 | 证据/备注 |
| --- | --- | --- | --- | --- |
| TECH-EP-001 | 首页根路由与标题节点 | 根路径页面加载完成后渲染主标题节点 | `apps/sample_web/register.py` | 探针与 Rosetta 映射基准 |
| TECH-EP-002 | 表单输入与提交选择器 | 输入框与按钮 CSS/文本定位契约 | `apps/sample_web/rosetta_map.json` | 变更时需同步 `probe-diff` |

<!-- kb:auto:start id="five-dim-technical-endpoints" -->
## 自动沉淀技术端点（勿手改本块）

<!-- 由 `ci/sentinel_kb_writeback.py` 自动维护 -->
<!-- kb:auto:end id="five-dim-technical-endpoints" -->
