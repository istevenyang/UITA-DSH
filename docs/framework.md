# UITA-DSH 框架扩展与接入指南（Framework Extension & Onboarding Guide）

<p align="left">
  <a href="framework_en.md">English</a> | <strong>简体中文</strong>
</p>

本文档说明 **UITA-DSH (Universal Intelligent Test Architecture With Deterministic Skill Harness & Cross-Platform UI Automation Runtime)** 的分层解耦原则、新被测应用（App）接入完整流程，以及框架内全部功能注册中心（Registries）与约定式接入点（Integration Points）。

---

## 1. 架构分层与扩展设计原则

1. **`core/` 零业务耦合**：`src/uita/core/` 与 `src/uita/engine/` 不包含任何特定被测应用的硬编码名称、选择器或私有逻辑。所有应用差异化配置与策略均收敛在 `apps/<app_name>/` 下。
2. **启动期自动发现（Auto-Discovery）**：CLI 启动时会自动扫描 `apps/*/register.py` 并导入执行，触发各类装饰器与注册表调用，无需修改框架核心入口。
3. **统一模型配置**：框架内所有模型调用（DSH 规划器、L6/L8 视觉定位与判定、性能分析）统一读取环境变量 `DSH_API_URL` 与 `DSH_API_KEY`。
4. **统一整数层级降级链（`L1 ~ L9`）**：定位与断言自愈降级链统一采用 `L1 ~ L9` 连续整数编号，并支持 **App 适配级** 与 **元素自定义级** 双层自定义降级链。

---

## 2. 全量注册中心与接入点速查表

| 扩展维度 | 注册接口 / 装饰器 / 约定路径 | 所属模块 / 目录 | 作用说明 |
| :--- | :--- | :--- | :--- |
| **应用平台配置** | `AppRegistry.register(AppPlatformConfig(...))` | `uita.core.platform.app_registry` | 注册 `(app_name, platform)` 的启动方式、Rosetta 路径、CDP 端口与 `extra` 扩展参数 |
| **UI 执行引擎** | `@register_engine(app_name, platform)` | `uita.core.engine.registry` | 注册 Playwright / macOS AppKit / Windows UIA 或自定义 `EngineBase` 子类 |
| **API 执行引擎** | `@register_api_engine(app_name, platform)` | `uita.core.engine.api_registry` | 注册纯接口自动化 `ApiEngineBase`（如 `HttpEngine`）子类 |
| **API 认证提供者** | `@register_api_auth(app_name, platform)` | `uita.core.engine.api_auth` | 注册 `ApiAuthProvider` 子类，管理接口鉴权 Header 与生命周期 |
| **自定义降级链** | `router_proxy.json` / `rosetta_map.json` / `extra` | `apps/<app>/` | 配置 **App 适配级**（`__default__`）与 **元素自定义级**（按元素 ID / `strategy_chain`）的 `L1~L9` 降级顺序 |
| **TCS (TestCaseScript) 动作指令** | `@register_action(*names, app="")` | `uita.core.actions.registry` | 注册 TCS 动作关键字（支持 `common/` 公共动作与单 App 专属动作） |
| **启动与恢复策略** | `@register_launch_policy(app_name, platform)` | `uita.core.lifecycle.launch_policy` | 注册 `LaunchPolicyBase`，定制 `wait_ui_ready`、`post_launch`、`recover`、`on_launch_timeout` |
| **安装包管理器** | `InstallRegistry.register(InstallConfig(...))` | `uita.core.platform.install_registry` | 为 `--mode install` 提供 macOS/Windows 安装包静默安装与路径探测元数据 |
| **应用日志采集器** | `@register_collector(app_name, platform)` | `uita.core.hooks.collector_registry` | 注册 `LogCollectorBase`，在批次结束时自动收集被测客户端日志 |
| **执行生命周期钩子** | `@register_hook(*names)` | `uita.core.hooks.registry` | 注册 `HookBase`，拦截 batch/case/step 生命周期（如报告汇总、截图、外部通知） |
| **性能测试场景** | `register_scenario(module, name=...)` | `uita.core.perf.scenarios.registry` | 注册 `--mode perf` 性能测量场景（契约函数 `measure(bridge, **opts)`） |
| **DSH / MCP 工具** | `@register_dsh_tool` | `uita.tools.dsh.registry` | 继承 `DshTool`，一处声明同时暴露给 DSH Planner、MCP Server 与 Web 调试台 |
| **远端存储扩展** | `register_rosetta_store` / `register_router_proxy_store` | `uita.core.rosetta` | 注入自定义远端数据库存储（异常时自动降级本地 JSON） |
| **前置数据构造** | `apps/<app>/pre_test.py::main(ctx)` | `apps/<app>/pre_test.py` | 约定式模块，供 `uita --mode pretest --app <app>` 调用 |
| **Qt 探针部署** | `apps/<app>/probe_provision.py::main(ctx)` | `apps/<app>/probe_provision.py` | 约定式模块，供 `uita --mode probe-provision --app <app>` 调用 |
| **五维知识图谱** | `.dsh/knowledge/products/<app>/` | `.dsh/knowledge/` | 供 `sentinel` 与 `kb_recall` / `kb_writeback` 平级召回和受控沉淀知识 |

---

## 3. 新被测应用（App）标准接入流程

以内置的 `apps/sample_web/` 为参考，接入一个新应用 `<my_app>` 只需以下 4 步：

### 步骤 1：创建应用目录结构

```text
apps/<my_app>/
├── __init__.py
├── configs.py           # 应用常量与环境变量读取
├── register.py          # 引擎、AppPlatformConfig、LaunchPolicy、专属 Action 注册入口
├── rosetta_map.json     # 元素语义映射表（支持多版本 rosetta_map@<ver>.json）
├── router_proxy.json    # 可选：App 适配级与元素自定义级降级链配置
├── nav_map.json         # 可选：页面导航图（供 jump[] 动作自动寻路）
└── pre_test.py          # 可选：测试前置数据准备（--mode pretest）
```

### 步骤 2：编写 `configs.py` 与 `register.py`

```python
# apps/my_app/configs.py
from __future__ import annotations
import os

APP_NAME = "my_app"
DEFAULT_URL = os.environ.get("MY_APP_URL", "https://example.com")
```

```python
# apps/my_app/register.py
from __future__ import annotations
import os

from apps.my_app.configs import APP_NAME, DEFAULT_URL
from uita.core.engine.registry import register_engine
from uita.core.platform.app_registry import AppPlatformConfig, AppRegistry
from uita.engine.playwright.engine import PlaywrightEngine

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROSETTA_PATH = os.path.join(_HERE, "rosetta_map.json")
_NAV_PATH = os.path.join(_HERE, "nav_map.json")


# 1. 注册执行引擎（"universal" 会在注册时自动解析为当前宿主机平台 macOS / Windows / Linux）
@register_engine(APP_NAME, "universal")
class MyAppEngine(PlaywrightEngine):
    def __init__(self, **kwargs):
        super().__init__(
            app_name=APP_NAME,
            start_url=DEFAULT_URL,
            headless=os.environ.get("MY_APP_HEADLESS", "1") == "1",
            **kwargs,
        )


# 2. 注册各平台 AppPlatformConfig
for _platform in ("macOS", "Windows", "Linux"):
    AppRegistry.register(
        AppPlatformConfig(
            app_name=APP_NAME,
            platform=_platform,
            rosetta_map_path=_ROSETTA_PATH,
            nav_map_path=_NAV_PATH,
            extra={
                "start_url": DEFAULT_URL,
                "use_local_rosetta": True,
                # 可选：App 适配级默认降级链
                "default_strategy_chain": "L1,L2,L3,L4,L5,L6,L7,L8",
            },
        )
    )
```

> **桌面原生应用接入提示**：
> - **macOS 原生应用**：继承 `uita.engine.mac_appkit.engine.MacAppKitEngine`，设置 `launch_strategy=LaunchStrategy.NATIVE_MAC`。
> - **Windows 原生应用**：继承 `uita.engine.win_uia.engine.WinUIAEngine`，设置 `launch_strategy=LaunchStrategy.NATIVE_WIN`；可通过 `extra={"win_backend": "pywinauto"}` 或环境变量 `UIA_WIN_BACKEND=pywinauto` 切换窗口搜索后端。

### 步骤 3：配置 `rosetta_map.json` 与 `router_proxy.json`

在 `apps/<my_app>/rosetta_map.json` 中定义页面与元素定位符（支持 CSS/XPath、macOS AX 属性、Windows UIA 属性、Qt 探针路径 `qt`、多窗口提示 `window_hint`、视觉指纹 `pattern`）：

```json
{
  "_meta": {
    "version": "3.0",
    "app": "my_app",
    "default_strategy_chain": "L1,L2,L3,L4,L5,L6,L7,L8"
  },
  "my_app": {
    "首页": {
      "页面主标题": {
        "id": "el_my_app_home_title",
        "selector": "h1",
        "usage": "首页主标题",
        "views": ["首页"],
        "strategy_chain": "L1,L3,L5,L8"
      }
    }
  }
}
```

### 步骤 4：验证接入

```bash
# 静态校验 Rosetta Map 与 TCS 用例
uita --mode lint --app my_app

# 启动 DSH 交互式控制台探索录制
dsh serve --app my_app
```

---

## 4. 核心扩展点详解

### 4.1 统一 `L1 ~ L9` 降级链与双层自定义降级链

Resolver 将所有元素解析与视觉/OCR 策略统一编号为 `L1 ~ L9` 整数层级：

| 层级 | 枚举名 (`ResolveLevel`) | 路由策略名 (`ResolveStrategy`) | 简写 Token | 机制说明 |
| :--- | :--- | :--- | :--- | :--- |
| **L1** | `L1_MAP_EXACT` | `MAP_EXACT` | `L1` / `MAP_EXACT` | `rosetta_map.json` 精确选择器匹配 |
| **L2** | `L2_SEMANTIC` | `SEMANTIC` | `L2` / `SEMANTIC` | 语义近似匹配（别名 / 包含 / bigram）+ 唯一性核验 |
| **L3** | `L3_TEXT_MATCH` | `TEXT_MATCH` | `L3` / `TEXT_MATCH` | 界面可见文本匹配（`find_by_text`） |
| **L4** | `L4_LABEL_MATCH` | `LABEL_MATCH` | `L4` / `LABEL_MATCH` | 关联 `<label>` 文本匹配（`find_by_label`） |
| **L5** | `L5_ROLE_MATCH` | `ROLE_MATCH` | `L5` / `ROLE_MATCH` | ARIA / AX / UIA 角色匹配（`find_by_role`） |
| **L6** | `L6_LLM_SELECTOR` | `LLM_SELECTOR` | `L6` / `LLM_SELECTOR` | 基于 DOM / 控件树摘要的 LLM 选择器推断 |
| **L7** | `L7_VISUAL_FP` | `VISUAL_FP` | `L7` / `VISUAL_FP` | OpenCV 局部视觉指纹模板/特征匹配与断言（`l7_visual_fingerprint.py` / `l7_assert_fingerprint.py`） |
| **L8** | `L8_LLM_VISUAL` | `LLM_VISUAL` | `L8` / `LLM_VISUAL` / `VISUAL` | OCR 文字识别断言（`l8_ocr.py`）与视觉大模型坐标定位/是非判定（`l8_integration.py`） |
| **L9** | `L9_QT_PROBE` | `QT_PROBE` | `L9` / `QT_PROBE` | Qt 进程内对象树探针通道（按需通过自定义链启用） |

#### 双层自定义降级链优先级（由高到低）

`ActionRouter.route_v2()` 按以下优先级决议每个元素的降级链（均支持 `"L1,L3,L7"` 或 `"MAP_EXACT,TEXT_MATCH,VISUAL_FP"` 语法）：

1. **元素自定义级（Element-Level，命中后 `skip_self_heal=True`，严格按声明顺序执行）**：
   - 优先级 1a：`apps/<app>/router_proxy.json` 中 `<element_id>.runtime_define`
   - 优先级 1b：`apps/<app>/router_proxy.json` 中 `<element_id>.default`
   - 优先级 1c：`apps/<app>/rosetta_map.json` 中该元素条目的 `"strategy_chain": "L1,L3,L8"`
2. **App 适配级（App-Level，命中后 `skip_self_heal=False`，保留运行时自愈记录能力）**：
   - 优先级 2a：`apps/<app>/router_proxy.json` 中 `"__default__": {"default": "L1,L2,L3,L5,L7,L8"}`
   - 优先级 2b：`AppPlatformConfig.extra["default_strategy_chain"]` 或 `rosetta_map.json` 顶层 `"_meta": {"default_strategy_chain": "..."}`
3. **框架标准全链（Framework Default）**：
   - `L1 → L2 → L3 → L4 → L5 → L6 → L7 → L8`（再经当前引擎能力自动裁剪不可用策略）。

---

#### 视觉指纹框选点击（`L7_VISUAL_FP` 区域点击能力）

当定位命中 `L7_VISUAL_FP`（`src/uita/core/resolver/l7_visual_fingerprint.py`）时，`click[]` 支持以下三种框选/子区域点击方式：

1. **指纹图红框标注自动识别与抹除（零配置框选）**：
   - 在截取较大上下文指纹图后，直接用画图工具在图内用**红色中空矩形框**圈出真正需要点击的子区域并编码进 `rosetta_map.json` 的 `pattern` 字段。
   - `VisualFingerprintResolver` 在匹配前会自动检测并 `inpaint` 抹除红框像素（避免干扰特征点与模板相关性），匹配成功后自动将红框内部区域映射为 `MatchResult.sub_bbox`，并取红框中心作为 `click_center`。
2. **`rosetta_map.json` 静态声明 `click_rect`**：
   - 在元素定义中与 `pattern` 同级声明 `"click_rect": [x, y, w, h]`（相对于模板图左上角的像素子矩形），命中后自动点击该子矩形中心。
3. **TCS 九宫格区域点击参数 `region=`（`src/uita/core/resolver/click_region.py`）**：
   - 在 TCS 中编写 `click[目标图标, region=top_right]`（支持 `center`、`top`、`bottom`、`left`、`right`、`top_left`、`top_right`、`bottom_left`、`bottom_right`），按命中 `bbox` 的四分点计算精确点击位置。

---

### 4.2 开发与注册公共 Action 及 App 专属 Action（`@register_action`）

#### 内置精简公共 Action 清单（`src/uita/core/actions/common/`）

框架公共层仅保留最核心的 6 类基础动作，保持内核精简：

| 公共 Action 文件 | 注册关键字 | 核心能力说明 |
| :--- | :--- | :--- |
| `action_click.py` | `click` | 元素点击、声明式首策略（`selector=/role=/text=`）、`prompt=` 视觉直达、`L7` 视觉指纹框选点击（`region=` / `click_rect` / 红框标注） |
| `action_type.py` | `type` | 文本输入、声明式首策略（`selector=/role=/label=`）、`L7/L8` 坐标句柄点击并输入、支持 `region=` 九宫格点击定位 |
| `action_scroll.py` | `scroll` | 按元素或当前窗口滑动（`up/down/left/right`，缺省按窗口高度 2/3 动态计算距离） |
| `action_assert.py` | `assert`, `verify` | 存在性/可见性/文本/属性/数量/JS 断言、`|` 多候选 OR 断言、`L7` 指纹断言、`L8` OCR 与视觉大模型断言 |
| `action_wait.py` | `wait` | 等待元素出现（`visible`）、等待元素消失（`state=gone`）、仅视觉指纹轮询（`visual_only=true`）或固定时长等待 |
| `action_switch.py` | `switch`, `switch_back` | 跨被测应用焦点切换（基于 `AppLifecycleStack`）及 `switch[api]` 接口引擎模式切换 |

#### 如何开发并注册新的公共 Action 或 App 专属 Action

所有 Action 均继承 `uita.core.actions.base.ActionBase` 并使用 `@register_action` 装饰器注册：

1. **方式 A：注册全局公共 Action（所有 App 共用）**
   - 在 `src/uita/core/actions/common/` 下新建 `action_<name>.py`（CLI 启动时 `uita.__main__._auto_discover()` 会自动扫描并导入该目录下所有 `action_*.py` 模块），并在 `interpreter.py` / `streaming_runner.py` 导出：
   ```python
   # src/uita/core/actions/common/action_hover.py
   from uita.core.actions.base import ActionBase, ActionContext, ActionResult
   from uita.core.actions.registry import register_action


   @register_action("hover")
   class HoverAction(ActionBase):
       @property
       def name(self) -> str:
           return "hover"

       async def execute(self, ctx: ActionContext) -> ActionResult:
           if not ctx.args:
               return ActionResult.fail("hover[] 需要参数: hover[element_name]")
           target_name = ctx.args[0].strip()
           resolved = await ctx.resolver.resolve(target_name)
           if not resolved.found:
               return ActionResult.fail(f"未找到元素: {target_name}")
           await ctx.engine.hover(resolved.element)
           return ActionResult.ok(f"hovered '{target_name}'")
   ```

2. **方式 B：注册 App 专属 Action（仅指定 App 生效，推荐放置于 `apps/<app>/`）**
   - 在 `apps/<app>/register.py`（或 `apps/<app>/actions.py` 并在 `register.py` 中导入）中使用 `@register_action("...", app="<app>")`：
   ```python
   # apps/my_app/actions.py
   from uita.core.actions.base import ActionBase, ActionContext, ActionResult
   from uita.core.actions.registry import register_action, register_action_scope_alias


   @register_action("custom_verify", app="my_app")
   class CustomVerifyAction(ActionBase):
       @property
       def name(self) -> str:
           return "custom_verify"

       async def execute(self, ctx: ActionContext) -> ActionResult:
           target_name = ctx.args[0].strip() if ctx.args else ""
           resolved = await ctx.resolver.resolve(target_name)
           if not resolved.found:
               return ActionResult.fail(f"未找到元素: {target_name}")
           return ActionResult.ok(f"verified '{target_name}' via {resolved.level.value}")


   # 可选：让派生/子应用继承 my_app 的专属 Action 作用域
   register_action_scope_alias("my_app_subtool", "my_app")
   ```

---

### 4.3 开发与注册执行引擎（`@register_engine` / `@register_api_engine`）

框架通过 `EngineBase`（UI 引擎）与 `ApiEngineBase`（API 引擎）隔离底层驱动差异：

1. **复用并定制内置跨平台 UI 引擎**：
   - Web / Electron：继承 `uita.engine.playwright.engine.PlaywrightEngine`
   - macOS 原生（AXUIElement）：继承 `uita.engine.mac_appkit.engine.MacAppKitEngine`
   - Windows 原生（UIA / `pywinauto` / Qt 探针）：继承 `uita.engine.win_uia.engine.WinUIAEngine`
2. **开发全新自定义 UI 引擎（实现 `EngineBase`）**：
   - 继承 `uita.core.engine.base.EngineBase`，实现核心抽象方法并用 `@register_engine(app_name, platform)` 注册：
   ```python
   from typing import Optional
   from uita.core.engine.base import ElementRef, EngineBase
   from uita.core.engine.registry import register_engine


   @register_engine("my_custom_app", "universal")
   class MyCustomEngine(EngineBase):
       async def launch(self) -> None:
           ...

       async def close(self) -> None:
           ...

       async def find_by_id(self, selector: str) -> Optional[ElementRef]:
           ...

       async def find_by_text(self, text: str) -> Optional[ElementRef]:
           ...

       async def click(self, element: ElementRef) -> None:
           ...

       async def type_text(self, element: ElementRef, text: str, clear: bool = True) -> None:
           ...

       async def scroll(self, element: Optional[ElementRef], direction: str, amount: int) -> None:
           ...

       async def is_visible(self, element: ElementRef) -> bool:
           ...

       async def take_screenshot(self) -> bytes:
           ...
   ```
3. **注册 HTTP / API 引擎（`@register_api_engine` + `@register_api_auth`）**：
   - 继承 `uita.engine.http.engine.HttpEngine`（或 `ApiEngineBase`），通过 `@register_api_engine("my_app", "universal")` 注册后，即可支持 `--mode api-runner` 或在 UI 用例中通过 `switch[api]` 切换执行接口请求。

---

### 4.3 应用生命周期策略（`@register_launch_policy`）与安装配置（`InstallRegistry`）

当被测客户端需要在启动后等待特定主页元素、自动关闭首屏引导弹窗或定制崩溃恢复策略时，在 `apps/<app>/register.py` 中注册 `LaunchPolicyBase`：

```python
from uita.core.lifecycle.launch_policy import LaunchPolicyBase, register_launch_policy
from uita.core.platform.install_registry import InstallConfig, InstallRegistry


@register_launch_policy("my_app", "universal")
class MyAppLaunchPolicy(LaunchPolicyBase):
    @property
    def name(self) -> str:
        return "my_app_policy"

    async def wait_ui_ready(self, engine) -> None:
        # 启动后等待核心元素出现
        pass

    async def post_launch(self, engine) -> None:
        # 启动就绪后清理一次性引导弹窗
        pass


# 可选：支持 `uita --mode install --app my_app --package-url <url>`
InstallRegistry.register(
    InstallConfig(
        app_name="my_app",
        platform="macOS",
        mac_app_dst_path="/Applications/MyApp.app",
        mac_process_name="MyApp",
    )
)
```

---

### 4.4 前置数据准备（`pre_test.py`）与探针注入（`probe_provision.py`）

框架提供两个非侵入式约定模块接入点：

- **`apps/<app>/pre_test.py`**（由 `uita --mode pretest --app <app>` 触发）：
  ```python
  from uita.core.lifecycle.pretest import PretestContext, PretestResult

  async def main(ctx: PretestContext) -> PretestResult:
      # 准备测试账号、清理缓存或初始化本地配置
      return PretestResult(status="success", message="前置数据就绪")
  ```
- **`apps/<app>/probe_provision.py`**（由 `uita --mode probe-provision --app <app>` 触发）：
  ```python
  from uita.core.lifecycle.probe_provision import ProbeProvisionContext, ProbeProvisionResult

  async def main(ctx: ProbeProvisionContext) -> ProbeProvisionResult:
      # 部署期注入 Qt 探针 DLL/dylib 并重启被测进程
      return ProbeProvisionResult(status="success", message="探针部署完成")
  ```

---

### 4.5 批次/用例生命周期钩子（`@register_hook`）与日志采集（`@register_collector`）

1. **执行钩子（`HookBase`）**：通过 `@register_hook("my_hook")` 注册，在 CLI 传入 `--hooks my_hook` 即可在 `on_batch_start`、`on_case_start`、`on_step_end`、`on_case_result_ready`、`on_batch_end` 阶段挂载自定义逻辑（如上传测试报告或发送告警通知）。
2. **应用日志采集（`LogCollectorBase`）**：通过 `@register_collector("my_app", "universal")` 注册，在批次结束时自动归档被测应用本地崩溃转储与运行日志到 `report/` 目录。

---

### 4.6 性能测试场景扩展（`register_scenario`）

在 `apps/<app>/perf/scenarios/` 下编写场景模块并通过 `register_scenario` 注册，即可通过 `uita --mode perf --app <app> --perf-scenarios <name>` 运行：

```python
# apps/my_app/perf/scenarios/cold_start.py
from uita.core.perf.scenarios.registry import register_scenario

SCENARIO_NAME = "cold_start"
SCENARIO_LABEL = "冷启动耗时"
REPORT_KEYS = {"launch_ms": "冷启动耗时(ms)"}
AGGREGATE_PICK = {"launch_ms": "min"}


def measure(bridge, **opts) -> dict:
    # 使用 bridge 执行测量并返回结构化指标
    return {"launch_ms": 420.0}


def to_aggregate_fields(result: dict) -> dict:
    return {"launch_ms": result["launch_ms"]}
```

---

### 4.7 DSH 工具与 MCP 能力扩展（`@register_dsh_tool`）

在 `src/uita/tools/dsh/tools/` 下继承 `DshTool` 并添加 `@register_dsh_tool`，**声明一次即可同时在三处生效**：
1. DSH Planner 的 LLM Function Calling 工具表；
2. MCP Server（`dsh mcp`）的 `list_tools` / `call_tool`；
3. DSH Web 控制台（`dsh serve`）的「工具调试台」。

```python
from uita.tools.dsh.registry import DshTool, TIER_READ, ToolResult, register_dsh_tool


@register_dsh_tool
class MyCustomInspectTool(DshTool):
    name = "custom_inspect"
    tier = TIER_READ
    description = "读取被测应用自定义诊断状态"
    params = {
        "section": {"type": "string", "description": "诊断模块名"},
    }
    required = ("section",)

    async def run(self, ctx, *, section: str) -> ToolResult:
        return ToolResult(ok=True, data={"section": section, "status": "healthy"})
```

---

### 4.8 `.dsh/` AI Skill、Agent 与本地五维知识图谱接入

`.dsh/` 目录下包含三大平级组件：

1. **`.dsh/skills/<skill_name>/SKILL.md`**：定义可复用的领域技能（如 `ui-automation`、`sentinel`、`kb-recall`）。
2. **`.dsh/agents/<agent_name>.md`**：定义专职智能体角色与提示词契约（如 `ui-tester.md`、`sentinel.md`）。
3. **`.dsh/knowledge/products/<app_name>/`**：本地五维知识图谱双质量飞轮。接入新产品的知识库时，按以下目录结构创建即可被 `kb_recall` / `kb_writeback` 与 `ci/sentinel_kb_flywheel.py` 自动识别：
   ```text
   .dsh/knowledge/products/<app_name>/
   ├── business/                # 维度 1 & 2：业务端点与业务流转
   ├── architecture/            # 维度 3 & 4：技术端点与技术架构
   ├── utest-rules/             # 维度 5：测试设计规则、04-known-pitfalls.md、06-run-history-digest.md、rules-meta.json
   ├── defects/                 # 历史缺陷传播链（<app_name>-defect-chain.md）
   └── kb-flywheel/             # 死知识治理台账（dead-knowledge.json）
   ```
   - **可选缺陷管理平台接入**：在 `sentinel` 执行代码变更风险分析时，支持可选关联外部**缺陷管理平台**（备注：可忽略/可自行接入，用于增加风险分析的业务关联准确性）。未接入外部平台时，`sentinel` 完全基于本地 `.dsh/knowledge/products/<app_name>/defects/` 与五维知识图谱闭环运行。

---

### 4.9 根目录 `logs/` 分类日志体系（`uita.core.logging_manager`）

框架默认将日志分流输出至项目根目录 `logs/` 下（可通过 `UITA_LOG_DIR` 覆盖）：

| 日志文件 | 类别 | 获取接口 | 记录内容 |
| :--- | :--- | :--- | :--- |
| `logs/execution.log` | **执行日志类** | `get_execution_logger()` | 元素降级链路由决策（`[ROUTE]` 候选链与来源）、逐层定位尝试与命中详情（`[LOCATE_TRY]` / `[LOCATE_HIT]`，含 `level`、`selector`、`coords`、`bbox`）、以及定位点击/输入/滑动/断言/等待闭环（`[ACTION_PASS]` / `[ACTION_FAIL]`，含 `click_coords` 与 `region=`） |
| `logs/actions.log` | **动作日志类** | `get_action_logger(name)` | `click`、`type`、`scroll`、`assert`/`verify`、`wait`、`switch`/`switch_back` 及自定义 Action 的入参、重试、坐标计算与判定明细 |
| `logs/kernel.log` | **内核日志类** | `get_kernel_logger(comp)` | 解释器调度、DSL 解析、跨平台引擎（Playwright / MacAppKit / WinUIA / HTTP）、生命周期栈、Hook 与 DSH 会话日志 |
| `logs/uita.log` | **全量汇总日志** | 自动汇总 | 全量汇总所有类别日志，便于按全局时间轴串联排查 |

