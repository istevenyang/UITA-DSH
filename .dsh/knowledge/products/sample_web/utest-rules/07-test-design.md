# `sample_web` 测试设计（Test Design）

定义 `sample_web` 的等价类划分、边界值矩阵与高风险回归策略。

## 测试设计矩阵

| ID | 名称 | 概述 | 代码/配置锚点 | 证据/备注 |
| --- | --- | --- | --- | --- |
| TD-001 | 首页渲染与表单主干冒烟 | 验证页面标题可见、输入框可填写、提交后结果正确呈现 | `cases/sample_web/smoke_search.tcs` | P0 冒烟必跑 |
| TD-002 | 边界字符与空白输入校验 | 覆盖空串、超长字符串与特殊字符输入 | `cases/sample_web/smoke_search.tcs` | P1 边界设计 |

<!-- kb:auto:start id="five-dim-test-design" -->
## 自动沉淀测试设计（勿手改本块）

<!-- 由 `ci/sentinel_kb_writeback.py` 自动维护 -->
<!-- kb:auto:end id="five-dim-test-design" -->
