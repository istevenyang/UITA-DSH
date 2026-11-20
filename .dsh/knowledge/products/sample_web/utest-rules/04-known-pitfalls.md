# `sample_web` 已知踩坑与规避规则（Known Pitfalls）

记录自动化执行与用例编写过程中的已知陷阱，供测试设计前平级召回。

## 手写核心规则

1. **禁止在 `.tcs` 中硬编码冗长 CSS 选择器**：所有页面元素定位统一收敛至 `apps/sample_web/rosetta_map.json`。
2. **异步渲染需显式等待或断言可见**：触发提交动作后，先断言结果容器可见再校验文本。

<!-- kb:auto:start id="pitfalls" -->
## 自动沉淀踩坑（勿手改本块）

<!-- 由 `ci/sentinel_kb_writeback.py` 自动维护 -->
<!-- kb:auto:end id="pitfalls" -->
