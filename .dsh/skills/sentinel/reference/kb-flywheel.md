# 本地知识图谱双质量飞轮参考（`kb-flywheel`）

本参考定义 `sentinel` 在 `.dsh/knowledge/products/<product>/` 上的**平级召回 → 采纳/拒绝决策 → 缺口审计 → `kb:auto` 受控回写 → 死知识淘汰**完整闭环。

## 1. 五维知识结构与目录契约

| 维度键 | 相对路径 | 必需二级章节 | 自动围栏 ID |
| --- | --- | --- | --- |
| `business_endpoint` | `business/business-endpoints.md` | `## 入口与弹窗` | `five-dim-business-endpoints` |
| `business_flow` | `business/business-flows.md` | `## 主干与异常流转` | `five-dim-business-flows` |
| `tech_endpoint` | `architecture/technical-endpoints.md` | `## 接口与信号契约` | `five-dim-technical-endpoints` |
| `tech_arch` | `architecture/technical-architecture.md` | `## 模块与调用链` | `five-dim-technical-architecture` |
| `test_design` | `utest-rules/07-test-design.md` | `## 测试设计矩阵` | `five-dim-test-design` |

配套规则与台账文件：
- `utest-rules/rules-meta.json`：产品知识版本元数据
- `utest-rules/04-known-pitfalls.md`：已知踩坑（自动围栏 `id="pitfalls"`）
- `utest-rules/06-run-history-digest.md`：运行历史摘要（自动围栏 `id="run-digest"`）
- `defects/<product>-defect-chain.md`：历史缺陷传播链（自动围栏 `id="defect-chain"`）
- `kb-flywheel/dead-knowledge.json`：死知识治理台账

## 2. 关系表与 UID 命名空间规范

五维文档中可通过 `| 源实体 | 目标实体 | 关系类型 | 证据 |` 维护知识图谱有向边：
- **合法 UID 命名空间**：`uita:`、`code:`、`issue:`（缺陷管理平台单据，可忽略/可自行接入）、`pr:`、`git:`、`tcs:`、`utest:`。
- **10 种有向边白名单**：
  `owns_module`、`changes_symbol`、`Exposes`、`triggers`、`implemented_by`、`calls`、`depends_on`、`breaks`、`fixed_by`、`tested_by`。

## 3. 执行阶段命令速查

1. **阶段 0/1 平级召回**：
   ```bash
   python3 ci/sentinel_kb_flywheel.py --product-code <product> recall --query "<模块或变更关键词>" --out report/sentinel/<product>/<batch>/local_recall.json
   ```
2. **阶段 5 召回审计与关系校验**：
   ```bash
   python3 ci/sentinel_kb_flywheel.py --product-code <product> audit --recall-evidence report/sentinel/<product>/<batch>/recall_evidence.json
   python3 ci/sentinel_kb_flywheel.py --product-code <product> relations
   ```
3. **阶段 5 受控回写（`kb:auto` 围栏守护）**：
   ```bash
   python3 ci/sentinel_kb_writeback.py --product-code <product> run-digest --run-report report/sentinel/<product>/<batch>/run-report.json
   python3 ci/sentinel_kb_writeback.py --product-code <product> pitfall --title "..." --symptom "..." --root-cause "..." --rule "..." --evidence "..."
   ```
