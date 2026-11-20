# `.dsh/knowledge` — 本地五维知识图谱与双质量飞轮

`.dsh/knowledge/` 与 `.dsh/skills/`、`.dsh/agents/` 平级，承载被测产品的**本地五维知识图谱**（业务端点、业务流转、技术端点、技术架构、测试设计）以及执行踩坑与历史缺陷链沉淀，支持**平级本地召回（Recall）**与**受控自动回写（Writeback）**闭环。

## 目录结构（以示例产品 `sample_web` 为例）

```text
.dsh/knowledge/
├── README.md
└── products/
    └── sample_web/
        ├── business/
        │   ├── business-endpoints.md        # 维度 1：业务端点（入口 / 表单 / 反馈态 + 关系表）
        │   └── business-flows.md            # 维度 2：业务流转（状态机 / 主干与异常链路）
        ├── architecture/
        │   ├── technical-endpoints.md       # 维度 3：技术端点（DOM / 路由 / 本地接口契约）
        │   └── technical-architecture.md    # 维度 4：技术架构（模块边界 / 渲染管线 / 依赖关系）
        ├── utest-rules/
        │   ├── rules-meta.json              # 产品知识元数据与版本
        │   ├── 04-known-pitfalls.md         # 已知执行踩坑与规避规则
        │   ├── 06-run-history-digest.md     # 批次运行历史摘要（自动回写区）
        │   └── 07-test-design.md            # 维度 5：测试设计（等价类 / 边界值 / 风险矩阵）
        ├── defects/
        │   └── sample_web-defect-chain.md   # 历史缺陷传播链（触发条件 → 传播路径 → 回归锚点）
        └── kb-flywheel/
            └── dead-knowledge.json          # 死知识台账（长期未命中或待重写条目）
```

## 平级召回与写入命令

1. **平级本地召回（CLI / DSH 工具 `kb_recall`）**：
   ```bash
   python3 ci/sentinel_kb_flywheel.py --product-code sample_web recall --query "登录 表单 校验"
   ```
2. **受控回写（CLI / DSH 工具 `kb_writeback`，仅写入 `<!-- kb:auto:start/end -->` 围栏区，手写区字节零改动）**：
   ```bash
   # 追加已知踩坑
   python3 ci/sentinel_kb_writeback.py --product-code sample_web pitfall \
     --title "提交后立即断言导致偶发未渲染" \
     --symptom "点击提交按钮后立即断言提示条偶发失败" \
     --root-cause "异步状态更新存在约 100ms 渲染延迟" \
     --rule "提交后使用显式等待或断言文本可见" \
     --evidence "report/sentinel/sample_web/20260101-120000/run-report.json"

   # 追加五维增量事实
   python3 ci/sentinel_kb_writeback.py --product-code sample_web five-dim \
     --dimension business_endpoint \
     --section "入口与弹窗" \
     --id "EP-003" \
     --name "重置按钮" \
     --summary "清空输入并恢复初始提示状态" \
     --source "apps/sample_web/rosetta_map.json" \
     --evidence "report/sentinel/sample_web/20260101-120000/run-report.json"
   ```
3. **关系表校验 / 召回缺口审计 / 死知识台账**：
   ```bash
   python3 ci/sentinel_kb_flywheel.py --product-code sample_web relations
   python3 ci/sentinel_kb_flywheel.py --product-code sample_web audit --recall-evidence report/sentinel/sample_web/recall_evidence.json
   python3 ci/sentinel_kb_flywheel.py --product-code sample_web dead --scan-reports report/sentinel/sample_web
   ```
