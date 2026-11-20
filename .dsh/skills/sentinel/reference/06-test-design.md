# 06 · 单测设计（行为级 + 可执行红绿契约）

步骤 6 严格通过 `ci/sentinel_test_design.py` 执行状态机，禁止手写结果字段（`actual_result`、`verification_status`、`revision`、`content_sha256` 等均由脚本独占生成）：

```text
init（步骤 5 末已执行）→ Agent 编写 .cache/test-intent.json 与 tests/ 测试文件
  → validate → freeze → run / run-batch（真实 Runner + JUnit XML）→ seal
```

## 1. 填写 `.cache/test-intent.json` 与测试代码

每个 `T<n>` 必填：
- `test_id`（`T1`、`T2`...）、`risk_key`（`R<n>`）、`coverage_target`、`test_kind`（`adversarial` | `lock`）；
- `source_bug_id`（无则写 `"none"`）、`change_location`、`feature_behavior`、`ui_surface`；
- `given` / `when` / `then`、`mock_surface`、非空 `assertions`；
- `test_file`（报告目录内 `tests/...` 相对路径）与 `test_name`（与 JUnit XML `<testcase name>` 精确一致）；
- `correct_oracle={"code_ref": "<40位SHA>", "description": "..."}`；对抗用例另填 `defective_oracle={"code_ref": "<40位SHA>", "description": "..."}`。

## 2. 校验、冻结、执行与封存

```bash
# 1) 校验意图与文件
python ci/sentinel_test_design.py validate --report-dir <报告目录>

# 2) 冻结 revision 与文件 SHA-256
python ci/sentinel_test_design.py freeze --report-dir <报告目录>

# 3) 批量或单条真实运行（Runner 产出 JUnit XML）
python ci/sentinel_test_design.py run-batch --report-dir <报告目录> \
  --manifest <报告目录>/.cache/test-runs-manifest.json

# 4) 封存并生成 .cache/test-design-summary.json
python ci/sentinel_test_design.py seal --report-dir <报告目录>
```

- **对抗用例（`adversarial`）**：必须在 `defective` 上产生真实业务断言红（`assertion_red`），并在 `correct` 上全绿（`green`），才被脚本判定为 `verified`。
- **锁定用例（`lock`）**：在 `correct` 上真实运行全绿即为 `verified`。
- 落盘 `06-test-design.md` 并用 `ci/sentinel_evidence.py append` 记录每条 `test:T<n>` 证据。
