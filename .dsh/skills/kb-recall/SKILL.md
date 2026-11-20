---
name: kb-recall
description: 本地五维知识图谱平级召回与受控回写技能：在需求评审、风险分析、测试设计与故障归因前，从 .dsh/knowledge/products/<product>/ 召回业务端点、业务流转、技术端点、技术架构、测试设计、已知踩坑与缺陷链，并在复盘阶段将有效经验通过 kb:auto 围栏回写至本地知识库。
---

# 本地五维知识图谱召回与回写（`kb-recall`）

`.dsh/knowledge/` 与 `.dsh/skills/`、`.dsh/agents/` 同级放置，提供**零外部平台强依赖**的本地知识图谱飞轮。

## 1. 适用场景

1. **变更风险分析与测试设计前**：根据变更模块、页面入口或需求描述，平级召回五维知识与历史踩坑。
2. **关联缺陷管理平台单据时**（备注：可忽略/可自行接入，用于增加风险分析的业务关联准确性）：将单据标题/模块名作为查询词，与本地五维知识图谱做交叉印证。
3. **测试批次收尾与复盘时**：将新发现的踩坑、缺陷传播链、五维增量事实与批次摘要受控回写至 `<!-- kb:auto:start/end -->` 围栏区。

## 2. 平级召回方式

### 方式 A：DSH 原生工具调用（推荐）

调用 `kb_recall` 工具：
- `product`: 产品知识目录名（如 `sample_web`）
- `query`: 检索词（如 `"首页 表单 提交"`）
- `dimension`: 可选限定五维（`business_endpoint` / `business_flow` / `tech_endpoint` / `tech_arch` / `test_design`）
- `top_k`: 返回条数（默认 `8`）
- `hop_depth`: 关系表图扩展跳数（默认 `1`）

### 方式 B：命令行脚本调用

```bash
python3 ci/sentinel_kb_flywheel.py \
  --product-code sample_web \
  recall \
  --query "首页 表单 提交" \
  --top-k 8 \
  --hop-depth 1
```

## 3. 受控写入与双质量飞轮

1. **受控写入（`kb_writeback` 工具或 `ci/sentinel_kb_writeback.py`）**：
   - 仅允许修改目标 Markdown 文件中 `<!-- kb:auto:start id="..." -->` 与 `<!-- kb:auto:end id="..." -->` 之间的内容，手写区字节零改动（越界即触发 `GuardViolation`）。
   - 支持四类动作：`run-digest`、`pitfall`、`defect-chain`、`five-dim`。
2. **召回缺口审计与死知识台账（`ci/sentinel_kb_flywheel.py`）**：
   - `audit`: 校验 `recall_evidence.json` 中每条命中记录是否落了 `adopted` / `rejected` / `irrelevant` 决策，并识别低命中缺口。
   - `dead`: 汇总长期未命中或累计被拒 ≥ 3 次的死知识条目，落盘至 `kb-flywheel/dead-knowledge.json`。
   - `relations`: 审计五维文档中的关系表是否符合 10 种有向边白名单与合法 UID 命名空间（`uita:` / `code:` / `issue:` / `pr:` / `git:` / `tcs:` / `utest:`）。
