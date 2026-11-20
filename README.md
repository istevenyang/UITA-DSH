# UITA-DSH

**UITA-DSH (Universal Intelligent Test Architecture With Deterministic Skill Harness & Cross-Platform UI Automation Runtime)** 是一个将 **AI 探索式规划（DSH）**、**受控风险单测与真机验证（Sentinel）**、**本地五维知识图谱飞轮（`.dsh/knowledge`）** 与 **确定性跨平台 UI 自动化执行（TCS / TestCaseScript + Rosetta 多层自愈定位）** 深度融合的通用智能测试架构与运行时。

- **AI 探索 → 确定性编译闭环**：通过自然语言目标或文本用例驱动应用交互，自动沉淀结构化步骤中间表示（Steps IR，`.ir.json`）并一键编译为确定性 `.tcs`（`TestCaseScript`）脚本；回归阶段无需调用大模型（0 Token 消耗）。
- **受控 9 步变更单测与验证（`sentinel`）**：面向 PR/MR/commit 或需求驱动的代码变更，按不可变 9 步状态机自动完成需求拆解、100% Diff 骨架提取、本地知识图谱平级召回、风险路由、真实行为级单元测试红绿对抗验证、多模态真机验证与知识自愈回写。支持可选关联**缺陷管理平台**（备注：可忽略/可自行接入，用于增加风险分析的业务关联准确性）。
- **本地五维知识图谱双质量飞轮（`.dsh/knowledge`）**：与 `.dsh/skills/`、`.dsh/agents/` 平级放置，围绕业务端点、业务流转、技术端点、技术架构、测试设计五个维度提供零外部平台强依赖的平级召回（`kb_recall`）与 `<!-- kb:auto:start/end -->` 受控写入（`kb_writeback`）。
- **多引擎统一抽象**：支持 Web / Electron（Playwright CDP）、macOS 原生（AppKit AXUIElement）、Windows 原生（UIA 默认 `uiautomation` + `pywinauto` 灰度后端 + Qt UI 探针 + `window_hint` 多窗口作用域）与 HTTP API。
- **统一 `L1 ~ L9` 整数级元素解析与双层自定义降级链（Resolver & ActionRouter）**：
  - 统一整数编号：`L1`（精确选择器 `MAP_EXACT`）→ `L2`（语义近似 `SEMANTIC`）→ `L3`（文本匹配 `TEXT_MATCH`）→ `L4`（关联标签 `LABEL_MATCH`）→ `L5`（角色匹配 `ROLE_MATCH`）→ `L6`（LLM 选择器推断 `LLM_SELECTOR`）→ `L7`（视觉指纹匹配与断言 `VISUAL_FP`）→ `L8`（OCR 识别与视觉大模型定位/判定 `LLM_VISUAL`）→ `L9`（Qt 探针通道 `QT_PROBE`）。
  - 支持 **App 适配级**（`router_proxy.json` 的 `__default__`、`rosetta_map.json` 的 `_meta.default_strategy_chain` 或 `AppPlatformConfig.extra["default_strategy_chain"]`）与 **元素自定义级**（`router_proxy.json` 按元素 ID 或 `rosetta_map.json` 元素字段 `strategy_chain`）双层自定义降级链。
- **统一模型凭据配置**：框架内所有模型调用（DSH 规划器、L6/L8 视觉定位与判定、性能分析）统一读取环境变量 `DSH_API_URL` 与 `DSH_API_KEY`。
- **完整扩展与接入文档**：详见 [`docs/framework.md`](docs/framework.md)（涵盖新 App 接入流程、14 类注册器与装饰器、双层自定义降级链、DSH 工具与知识图谱接入）。

---

## 1. 快速开始

### 1.1 安装依赖

```bash
pip install -e ".[dev]"
playwright install chromium
```

### 1.2 配置模型环境变量

仅需配置 `DSH_API_URL`（兼容任意通用 `chat/completions` 接口）与 `DSH_API_KEY`：

```bash
export DSH_API_URL="https://api.example.com/v1/chat/completions"
export DSH_API_KEY="sk-your-api-key"
export UITA_DSH_MODEL="default-model"
```

可运行环境自检确认配置状态：

```bash
uita --mode env_check
```

### 1.3 运行示例用例与 DSH 控制台

```bash
# 1. 静态检查与 Dry-Run 解析示例 TCS 用例
uita --mode parse --cases-dir cases/sample_web/smoke_search.tcs
uita --mode lint --app sample_web

# 2. 启动 DSH 本地可视化 Web 控制台（默认 127.0.0.1:8765）
dsh serve --app sample_web

# 3. 无头执行自然语言测试目标，并自动导出 IR 与 TCS
dsh run --app sample_web --goal "检查首页标题是否存在" --ir-out cases/sample_web/ir/home.ir.json --tcs-out cases/sample_web/home.tcs

# 4. 零 LLM 成本重放已固化的 IR
dsh replay cases/sample_web/ir/home.ir.json
```

---

## 2. 目录结构

```text
uita_dsh/
├── pyproject.toml
├── .env.example
├── .dsh/                        # Skill、Agent 与本地知识图谱目录（三者平级）
│   ├── skills/
│   │   ├── ui-automation/SKILL.md
│   │   ├── kb-recall/SKILL.md   # 本地五维知识图谱平级召回与回写 Skill
│   │   └── sentinel/            # 基于风险的变更单测设计与真机验证 Skill（9 步状态机 + reference）
│   ├── agents/
│   │   ├── ui-tester.md
│   │   └── sentinel.md
│   └── knowledge/               # 本地五维知识图谱与双质量飞轮（平级召回 + kb:auto 受控写入）
│       └── products/
│           └── sample_web/      # 示例产品五维知识、已知踩坑、缺陷链与死知识台账
├── ci/                          # sentinel 确定性脚手架、单测红绿契约、风险账本、证据归并与知识飞轮脚本
├── apps/                        # 被测应用配置与 Rosetta 语义映射表
│   └── sample_web/
│       ├── configs.py
│       ├── register.py
│       ├── rosetta_map.json     # 元素语义表（含 _meta.default_strategy_chain 与元素级 strategy_chain 示例）
│       ├── router_proxy.json    # App 适配级（__default__）与元素自定义级降级链配置示例
│       └── nav_map.json
├── docs/
│   └── framework.md             # 框架扩展与接入完整指南（注册方式、自定义降级链与接入点）
├── cases/                       # TCS (TestCaseScript) 确定性测试用例与 Steps IR
│   └── sample_web/
│       └── smoke_search.tcs
├── src/uita/
│   ├── __main__.py              # CLI 统一入口 (`uita`)
│   ├── config.py                # 全局配置（统一 DSH_API_URL / DSH_API_KEY）
│   ├── cli/                     # CLI 模式处理器
│   ├── core/                    # 核心运行时（DSL、Resolver、Rosetta、Lifecycle、Hooks、Perf）
│   ├── engine/                  # 跨平台执行引擎（playwright / mac_appkit / win_uia / http）
│   └── tools/
│       ├── dsh/                 # DSH 顶层编排器（Web Console / Headless / MCP / IR Compiler）
│       └── rosetta_diff/        # Rosetta 元素探查 Diff 工具
└── tests/                       # 单元测试集（核心运行时、DSH 编排、跨平台引擎与 Sentinel 工具链）
```

---

## 3. 单元测试与质量验证

UITA-DSH 包含两层单元测试体系：**框架自身单元测试集（`tests/`）** 与 **面向被测变更的风险驱动单元测试设计契约（`sentinel`）**。

### 3.1 运行框架单元测试（`pytest`）

测试集全部采用离线隔离设计（Mock / `tmp_path`），不依赖外网、真实大模型或特定桌面环境，开箱即可全量执行：

```bash
# 运行全量单元测试（覆盖 DSL、Rosetta、Resolver、Playwright/Mac/WinUIA 引擎、DSH 编排与 Sentinel CI 工具）
pytest -q

# 按模块运行专项单测
pytest tests/core/ -q                                   # 核心运行时、Rosetta 远端存储接口与 window_hint 单测
pytest tests/core/engine/test_win_uia_backend.py -q     # Windows UIA uiautomation / pywinauto 灰度后端契约单测
pytest tests/tools/dsh/ -q                              # DSH 工具链、IR 编译器、MCP 与会话管理单测
pytest tests/test_ci_sentinel_*.py -q                   # Sentinel 单测红绿契约、过程证据与本地知识飞轮单测
```

### 3.2 `sentinel` 变更驱动单元测试设计与红绿契约

针对代码变更（PR / MR / commit / branch），`sentinel`（`.dsh/skills/sentinel/`）提供受控的单元测试设计与验证闭环：

1. **平级知识召回与风险分析**：从 `.dsh/knowledge/products/<product>/` 召回五维知识与已知踩坑，结合 100% Diff 骨架与可选接入的**缺陷管理平台**（备注：可忽略/可自行接入，用于增加风险分析的业务关联准确性）识别变更风险 `R<n>`。
2. **对抗红绿（`adversarial`）+ 锁定绿（`lock`）双重契约**：
   - 通过 `ci/sentinel_test_design.py` 执行 `init → validate → freeze → run-batch → seal`；
   - 强制校验真实测试 Runner 退出码与 JUnit XML 产物，区分「真实业务断言红（`AssertionError`）」与「环境错误红」，防止伪造通过或跳步篡改。
3. **经验受控回写**：验证完毕后通过 `ci/sentinel_kb_writeback.py` 将新增踩坑、缺陷传播链与五维事实增量写入 `.dsh/knowledge/products/<product>/` 的 `<!-- kb:auto:start/end -->` 围栏区。

---

## 4. 扩展接口指南（外部子系统接入）

本仓库已剥离所有特定厂商子系统，仅保留标准抽象接口。如需接入自定义报告中心、消息通知、外部存储或视觉模型，可按以下接口扩展：

### 4.1 自定义报告与通知钩子（`HookBase`）

继承 `uita.core.hooks.base.HookBase` 并通过 `@register_hook` 注册，即可在 `--hooks <name>` 中启用：

```python
from uita.core.hooks.base import HookBase, HookContext
from uita.core.hooks.registry import register_hook

@register_hook("custom_report")
class CustomReportHook(HookBase):
    async def on_case_result_ready(self, ctx: HookContext) -> None:
        # ctx.report_dir 下已生成标准 result.json 与截图证据
        pass

    async def on_batch_end(self, ctx: HookContext) -> None:
        # 批次结束时汇总上报或发送 Webhook 通知
        pass
```

### 4.2 自定义视觉 / LLM 客户端（`VisionLLMClient`）

默认视觉客户端 `uita.tools.dsh.vision.DshVisionClient` 自动复用 `DSH_API_URL` 与 `DSH_API_KEY`。如需自定义视觉协议，可实现 `uita.core.drivers.llms.base.VisionLLMClient`：

```python
from uita.core.drivers.llms.base import VisionLLMClient

class CustomVisionClient(VisionLLMClient):
    async def locate(self, prompt: str, screenshot: bytes) -> tuple[float, float]:
        ...

    async def judge(self, question: str, screenshot: bytes) -> bool:
        ...

    async def chat(self, prompt: str) -> str:
        ...
```

### 4.3 自定义 Rosetta 元素与路由远端数据库存储（仅接口契约与配置示例）

框架默认启用本地文件存储（`LocalJsonRosettaStore` 与 `LocalJsonRouterProxyStore`，读取 `apps/<app>/rosetta_map.json` 和 `router_proxy.json`）。
如需将元素表与动态路由策略托管在远端数据库（如 MySQL / PostgreSQL / 配置中心），框架在 `uita.core.rosetta` 中提供了基于远端数据库的抽象基类与自动降级机制（**不内置具体数据库驱动或业务表实现**）：

- **元素表远端接口**：`uita.core.rosetta.base.RemoteDbRosettaStoreBase`（继承自 `RosettaStoreBase`）
- **路由表远端接口**：`uita.core.rosetta.router_proxy_base.RemoteDbRouterProxyStoreBase`（继承自 `RouterProxyStoreBase`）
- **容灾兜底**：`ElementMapLoader` 与 `RouterProxyLoader` 默认通过 `DbFallbackRosettaStore` / `DbFallbackRouterProxyStore` 装配；当远端数据库未配置、连接超时或查询异常时，自动回退读取本地 JSON。

#### 接口实现示例（用户侧扩展代码）

```python
# my_project/rosetta_db.py
from typing import Any, Optional
from uita.core.rosetta.base import RemoteDbRosettaStoreBase
from uita.core.rosetta.router_proxy_base import RemoteDbRouterProxyStoreBase


class MyDbRosettaStore(RemoteDbRosettaStoreBase):
    """自定义远端数据库元素存储实现（通过 self.db_url 读取 UITA_REMOTE_DB_URL）。"""

    def fetch_from_db(self, app_name: str) -> Optional[dict]:
        # 示例：使用自建数据库连接查询 app_name 对应的 rosetta_map 字典
        # return db_client.query_rosetta_map(dsn=self.db_url, app=app_name)
        raise NotImplementedError

    def upsert_heal_to_db(
        self,
        app_name: str,
        os_name: str,
        records: list[Any],
        reset_ids: Optional[set[str]] = None,
    ) -> int:
        # 可选：将运行期自愈命中的元素选择器回写至远端数据库
        return 0


class MyDbRouterProxyStore(RemoteDbRouterProxyStoreBase):
    """自定义远端数据库路由策略存储实现。"""

    def fetch_from_db(self, app_name: str) -> Optional[dict]:
        # 示例：查询 app_name 对应的 router_proxy 字典（{element_id: {"default": ..., "runtime_define": ...}}）
        # return db_client.query_router_proxy(dsn=self.db_url, app=app_name)
        raise NotImplementedError

    def upsert_routes_to_db(
        self,
        app_name: str,
        os_name: str,
        routes: dict[str, str],
        reset_ids: Optional[set[str]] = None,
    ) -> int:
        # 可选：将运行期自愈产生的最优路由策略回写至远端数据库
        return 0
```

#### 启用配置方式

**方式 A：通过环境变量声明（零代码改动）**

```bash
export UITA_USE_LOCAL_ROSETTA=0
export UITA_REMOTE_DB_URL="mysql+pymysql://user:password@db.example.com:3306/uita_rosetta"
export UITA_ROSETTA_STORE_CLASS="my_project.rosetta_db:MyDbRosettaStore"
export UITA_ROUTER_PROXY_STORE_CLASS="my_project.rosetta_db:MyDbRouterProxyStore"
```

**方式 B：在应用 `register.py` 或启动脚本中编程注册**

```python
from uita.core.rosetta.base import register_rosetta_store
from uita.core.rosetta.router_proxy_base import register_router_proxy_store
from my_project.rosetta_db import MyDbRosettaStore, MyDbRouterProxyStore

register_rosetta_store(MyDbRosettaStore)
register_router_proxy_store(MyDbRouterProxyStore)
```

### 4.4 Windows UIA 窗口后端灰度切换（`uiautomation` / `pywinauto`）与 `window_hint`

`WinUIAEngine` 抽象了 `WindowBackend` 契约（`src/uita/engine/win_uia/window_contract.py`），支持两种窗口内控件搜索后端：
- `uiautomation`（默认）：原生 DFS 查找。
- `pywinauto`（灰度可选）：基于 `UIAElementInfo(hwnd).descendants()` 查找，并内置缺 MFC 运行库（`mfc140u.dll`）时的 `win32ui` 空桩防护；未安装或异常时自动回退至 `uiautomation`。

启用方式：
1. 安装可选依赖：`pip install -e ".[win-pywinauto]"`
2. 通过环境变量 `export UIA_WIN_BACKEND=pywinauto` 或在应用 `configs.py` 的 `AppPlatformConfig.extra["win_backend"] = "pywinauto"` 开启。
3. 在 `rosetta_map.json` 元素定义中可通过 `"window_hint": "main"`、`"window_hint": "title=设置|Settings"` 或 `{"title": "...", "class": "...", "process": "..."}` 声明跨窗口/多顶层窗口作用域。

---

## 5. 版权与开源声明（Copyright & Open-Source Notice）

- **作者（Author）**：**ist3venyang**
- **归属声明（Ownership Disclaimer）**：本项目由个人开发者 **ist3venyang** 独立设计、编写与维护，**不属于任何商业组织或企业，仅归属于作者个人**。项目内不包含任何商业组织的专有代码、内部服务地址、业务数据或受限资产。
- **开源许可（License）**：本项目基于 [MIT License](https://opensource.org/licenses/MIT) 开源发布。

```text
MIT License

Copyright (c) 2026 ist3venyang (Personal Open-Source Project, Not Affiliated with Any Commercial Organization)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
