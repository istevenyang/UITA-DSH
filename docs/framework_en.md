# UITA-DSH Framework Extension & Onboarding Guide

<p align="left">
  <strong>English</strong> | <a href="framework.md">简体中文</a>
</p>

This guide explains the layered decoupling principles of **UITA-DSH (Universal Intelligent Test Architecture With Deterministic Skill Harness & Cross-Platform UI Automation Runtime)**, the end-to-end onboarding workflow for new target applications (Apps), and all registries, decorators, and convention-based integration points across the framework.

---

## 1. Architectural Layering & Extension Principles

1. **Zero Business Coupling in `core/`**: `src/uita/core/` and `src/uita/engine/` contain no hardcoded application names, selectors, or app-specific logic. All app-specific configurations and strategies live under `apps/<app_name>/`.
2. **Startup Auto-Discovery**: At CLI startup, `uita` automatically scans and imports `apps/*/register.py`, triggering all decorators and registry calls without modifying the core entry point.
3. **Unified Model Configuration**: All LLM/VLM calls (DSH Planner, L6/L8 visual locating & judging, performance analysis) read from `DSH_API_URL` and `DSH_API_KEY`.
4. **Unified Integer Fallback Chain (`L1 ~ L9`)**: Element resolution and assertion self-healing use continuous integer levels `L1 ~ L9`, supporting both **App-Level** and **Element-Level** custom fallback chains.

---

## 2. Complete Registry & Integration Point Reference

| Extension Dimension | Registry API / Decorator / Path Convention | Module / Directory | Description |
| :--- | :--- | :--- | :--- |
| **App Platform Config** | `AppRegistry.register(AppPlatformConfig(...))` | `uita.core.platform.app_registry` | Registers launch strategy, Rosetta path, CDP port, and `extra` options per `(app_name, platform)` |
| **UI Execution Engine** | `@register_engine(app_name, platform)` | `uita.core.engine.registry` | Registers Playwright / macOS AppKit / Windows UIA or custom `EngineBase` subclasses |
| **API Execution Engine** | `@register_api_engine(app_name, platform)` | `uita.core.engine.api_registry` | Registers pure API automation `ApiEngineBase` (e.g., `HttpEngine`) subclasses |
| **API Auth Provider** | `@register_api_auth(app_name, platform)` | `uita.core.engine.api_auth` | Registers `ApiAuthProvider` subclasses to manage authentication headers and lifecycle |
| **Custom Fallback Chain** | `router_proxy.json` / `rosetta_map.json` / `extra` | `apps/<app>/` | Configures **App-Level** (`__default__`) and **Element-Level** (per element ID / `strategy_chain`) `L1~L9` fallback order |
| **TCS (TestCaseScript) Actions** | `@register_action(*names, app="")` | `uita.core.actions.registry` | Registers TCS action keywords (supports shared `common/` actions and per-App custom actions) |
| **Launch & Recovery Policy** | `@register_launch_policy(app_name, platform)` | `uita.core.lifecycle.launch_policy` | Registers `LaunchPolicyBase` to customize `wait_ui_ready`, `post_launch`, `recover`, and `on_launch_timeout` |
| **Installer Manager** | `InstallRegistry.register(InstallConfig(...))` | `uita.core.platform.install_registry` | Provides silent install and binary path discovery metadata for `--mode install` on macOS/Windows |
| **App Log Collector** | `@register_collector(app_name, platform)` | `uita.core.hooks.collector_registry` | Registers `LogCollectorBase` to archive client logs automatically at the end of a batch |
| **Execution Lifecycle Hooks** | `@register_hook(*names)` | `uita.core.hooks.registry` | Registers `HookBase` to intercept batch/case/step lifecycle events (reporting, screenshots, webhooks) |
| **Performance Scenarios** | `register_scenario(module, name=...)` | `uita.core.perf.scenarios.registry` | Registers `--mode perf` measurement scenarios (`measure(bridge, **opts)` contract) |
| **DSH / MCP Tools** | `@register_dsh_tool` | `uita.tools.dsh.registry` | Subclasses `DshTool`; a single declaration exposes the tool to DSH Planner, MCP Server, and Web Console |
| **Remote Store Extension** | `register_rosetta_store` / `register_router_proxy_store` | `uita.core.rosetta` | Injects custom remote database storage with automatic fallback to local JSON |
| **Pre-Test Provisioning** | `apps/<app>/pre_test.py::main(ctx)` | `apps/<app>/pre_test.py` | Convention-based module invoked by `uita --mode pretest --app <app>` |
| **Qt Probe Provisioning** | `apps/<app>/probe_provision.py::main(ctx)` | `apps/<app>/probe_provision.py` | Convention-based module invoked by `uita --mode probe-provision --app <app>` |
| **5-Dim Knowledge Graph** | `.dsh/knowledge/products/<app>/` | `.dsh/knowledge/` | Enables local 5-dimensional knowledge recall (`kb_recall`) and guarded write-back (`kb_writeback`) |

---

## 3. Standard Onboarding Workflow for a New App

Using the built-in `apps/sample_web/` as a reference, onboarding a new application `<my_app>` takes 4 steps:

### Step 1: Create the App Directory Structure

```text
apps/<my_app>/
├── __init__.py
├── configs.py           # App constants and environment variable lookups
├── register.py          # Engine, AppPlatformConfig, LaunchPolicy, and custom Action registration entry
├── rosetta_map.json     # Semantic element map (supports versioned rosetta_map@<ver>.json)
├── router_proxy.json    # Optional: App-level and element-level fallback chain configuration
├── nav_map.json         # Optional: Navigation graph for automatic jump[] pathfinding
└── pre_test.py          # Optional: Pre-test data setup (--mode pretest)
```

### Step 2: Implement `configs.py` and `register.py`

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
from uita.core.platform.app_registry import AppPlatformConfig, AppRegistry, LaunchStrategy
from uita.engine.playwright.engine import PlaywrightEngine

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROSETTA_PATH = os.path.join(_HERE, "rosetta_map.json")
_NAV_PATH = os.path.join(_HERE, "nav_map.json")


# 1. Register Execution Engine ("universal" resolves to current host OS: macOS / Windows / Linux)
@register_engine(APP_NAME, "universal")
class MyAppEngine(PlaywrightEngine):
    def __init__(self, **kwargs):
        super().__init__(
            app_name=APP_NAME,
            start_url=DEFAULT_URL,
            headless=os.environ.get("MY_APP_HEADLESS", "1") == "1",
            **kwargs,
        )


# 2. Register AppPlatformConfig per platform
for _platform in ("macOS", "Windows", "Linux"):
    AppRegistry.register(
        AppPlatformConfig(
            app_name=APP_NAME,
            platform=_platform,
            launch_strategy=LaunchStrategy.CDP,
            rosetta_map_path=_ROSETTA_PATH,
            nav_map_path=_NAV_PATH,
            extra={
                "start_url": DEFAULT_URL,
                "use_local_rosetta": True,
                # Optional: App-level default fallback chain
                "default_strategy_chain": "L1,L2,L3,L4,L5,L6,L7,L8",
            },
        )
    )
```

> **Desktop Native App Onboarding Tips**:
> - **macOS Native Apps**: Inherit from `uita.engine.mac_appkit.engine.MacAppKitEngine` and set `launch_strategy=LaunchStrategy.NATIVE_MAC`.
> - **Windows Native Apps**: Inherit from `uita.engine.win_uia.engine.WinUIAEngine` and set `launch_strategy=LaunchStrategy.NATIVE_WIN`; switch control search backend via `extra={"win_backend": "pywinauto"}` or `UIA_WIN_BACKEND=pywinauto`.

### Step 3: Configure `rosetta_map.json` & `router_proxy.json`

Define pages and element locators in `apps/<my_app>/rosetta_map.json` (supports CSS/XPath, macOS AX attributes, Windows UIA properties, Qt probe paths `qt`, multi-window hints `window_hint`, and visual fingerprints `pattern`):

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
        "usage": "Main heading on the home page",
        "views": ["首页"],
        "strategy_chain": "L1,L3,L5,L8"
      }
    }
  }
}
```

### Step 4: Verify Onboarding

```bash
# Statically lint Rosetta Map and TCS cases
uita --mode lint --app my_app

# Launch the interactive DSH Web Console for exploratory recording
dsh serve --app my_app
```

---

## 4. Core Extension Points in Detail

### 4.1 Unified `L1 ~ L9` Fallback Chain & Two-Tier Custom Fallback Chains

Resolver numbers all element resolution and visual/OCR strategies as continuous integer levels `L1 ~ L9`:

| Level | Enum (`ResolveLevel`) | Router Strategy (`ResolveStrategy`) | Shorthand Token | Mechanism |
| :--- | :--- | :--- | :--- | :--- |
| **L1** | `L1_MAP_EXACT` | `MAP_EXACT` | `L1` / `MAP_EXACT` | Exact selector match from `rosetta_map.json` |
| **L2** | `L2_SEMANTIC` | `SEMANTIC` | `L2` / `SEMANTIC` | Semantic similarity match (alias / substring / bigram) + uniqueness verification |
| **L3** | `L3_TEXT_MATCH` | `TEXT_MATCH` | `L3` / `TEXT_MATCH` | Visible UI text match (`find_by_text`) |
| **L4** | `L4_LABEL_MATCH` | `LABEL_MATCH` | `L4` / `LABEL_MATCH` | Associated `<label>` text match (`find_by_label`) |
| **L5** | `L5_ROLE_MATCH` | `ROLE_MATCH` | `L5` / `ROLE_MATCH` | ARIA / AX / UIA role match (`find_by_role`) |
| **L6** | `L6_LLM_SELECTOR` | `LLM_SELECTOR` | `L6` / `LLM_SELECTOR` | LLM selector inference from DOM / control tree summary |
| **L7** | `L7_VISUAL_FP` | `VISUAL_FP` | `L7` / `VISUAL_FP` | OpenCV local visual fingerprint template/feature matching & assertion (`l7_visual_fingerprint.py` / `l7_assert_fingerprint.py`) |
| **L8** | `L8_LLM_VISUAL` | `LLM_VISUAL` | `L8` / `LLM_VISUAL` / `VISUAL` | OCR text assertion (`l8_ocr.py`) & Vision LLM coordinate locating/boolean judging (`l8_integration.py`) |
| **L9** | `L9_QT_PROBE` | `QT_PROBE` | `L9` / `QT_PROBE` | In-process Qt object tree probe channel (enabled on demand via custom chains) |

#### Two-Tier Custom Fallback Chain Precedence (Highest to Lowest)

`ActionRouter.route_v2()` resolves the fallback chain for each element in the following order (supporting both `"L1,L3,L7"` and `"MAP_EXACT,TEXT_MATCH,VISUAL_FP"` syntax):

1. **Element-Level Custom Chain (`skip_self_heal=True`, strictly follows declared order)**:
   - Priority 1a: `<element_id>.runtime_define` in `apps/<app>/router_proxy.json`
   - Priority 1b: `<element_id>.default` in `apps/<app>/router_proxy.json`
   - Priority 1c: `"strategy_chain": "L1,L3,L8"` on the element entry in `apps/<app>/rosetta_map.json`
2. **App-Level Custom Chain (`skip_self_heal=False`, preserves runtime self-healing recording)**:
   - Priority 2a: `"__default__": {"default": "L1,L2,L3,L5,L7,L8"}` in `apps/<app>/router_proxy.json`
   - Priority 2b: `AppPlatformConfig.extra["default_strategy_chain"]` or top-level `"_meta": {"default_strategy_chain": "..."}` in `rosetta_map.json`
3. **Framework Default Chain**:
   - `L1 → L2 → L3 → L4 → L5 → L6 → L7 → L8` (automatically pruned by the active engine's supported capabilities).

---

#### Visual Fingerprint Sub-Region Clicking (`L7_VISUAL_FP` Region Click)

When an element resolves via `L7_VISUAL_FP` (`src/uita/core/resolver/l7_visual_fingerprint.py`), `click[]` supports three sub-region targeting methods:

1. **Automatic Red-Box Annotation Detection & Inpainting (Zero-Config Sub-Region Click)**:
   - Capture a larger context template screenshot, draw a **hollow red rectangle** around the exact click target inside the image, and encode it into the `pattern` field of `rosetta_map.json`.
   - `VisualFingerprintResolver` automatically detects and inpaints the red border pixels before template/feature matching, maps the red box interior to `MatchResult.sub_bbox`, and clicks its center (`click_center`).
2. **Static `click_rect` Declaration in `rosetta_map.json`**:
   - Declare `"click_rect": [x, y, w, h]` (pixel sub-rectangle relative to the top-left corner of the template image) alongside `pattern`.
3. **TCS 9-Grid `region=` Parameter (`src/uita/core/resolver/click_region.py`)**:
   - Write `click[TargetIcon, region=top_right]` in TCS (supports `center`, `top`, `bottom`, `left`, `right`, `top_left`, `top_right`, `bottom_left`, `bottom_right`) to click the exact quadrant point of the matched `bbox`.

---

### 4.2 Developing & Registering Shared Actions and App-Specific Actions (`@register_action`)

#### Built-in Core Shared Actions (`src/uita/core/actions/common/`)

The core runtime keeps 6 essential shared actions:

| Shared Action File | Keywords | Core Capabilities |
| :--- | :--- | :--- |
| `action_click.py` | `click` | Element click, declarative primary strategy (`selector=/role=/text=`), `prompt=` vision locating, `L7` fingerprint sub-region click (`region=` / `click_rect` / red-box annotation) |
| `action_type.py` | `type` | Text input, declarative primary strategy (`selector=/role=/label=`), `L7/L8` coordinate click-and-type, `region=` 9-grid targeting |
| `action_scroll.py` | `scroll` | Element or window scrolling (`up/down/left/right`, dynamically defaults to 2/3 of window height) |
| `action_assert.py` | `assert`, `verify` | Existence/visibility/text/attribute/count/JS assertions, `|` multi-candidate OR assertions, `L7` fingerprint assertions, `L8` OCR & VLM assertions |
| `action_wait.py` | `wait` | Wait for element appearance (`visible`), disappearance (`state=gone`), visual-only polling (`visual_only=true`), or fixed duration wait |
| `action_switch.py` | `switch`, `switch_back` | Cross-application focus switching (`AppLifecycleStack`) and `switch[api]` API engine switching |

#### How to Register a New Shared or App-Specific Action

All actions inherit from `uita.core.actions.base.ActionBase` and use `@register_action`:

1. **Option A: Register a Global Shared Action (available to all Apps)**
   - Create `action_<name>.py` under `src/uita/core/actions/common/` (auto-discovered at CLI startup):
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
               return ActionResult.fail("hover[] requires an element argument: hover[element_name]")
           target_name = ctx.args[0].strip()
           resolved = await ctx.resolver.resolve(target_name)
           if not resolved.found:
               return ActionResult.fail(f"Element not found: {target_name}")
           await ctx.engine.hover(resolved.element)
           return ActionResult.ok(f"hovered '{target_name}'")
   ```

2. **Option B: Register an App-Specific Action (recommended under `apps/<app>/`)**
   - Use `@register_action("...", app="<app>")` inside `apps/<app>/register.py` (or `apps/<app>/actions.py` imported by `register.py`):
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
               return ActionResult.fail(f"Element not found: {target_name}")
           return ActionResult.ok(f"verified '{target_name}' via {resolved.level.value}")


   # Optional: allow a sub-app to inherit my_app's action scope
   register_action_scope_alias("my_app_subtool", "my_app")
   ```

---

### 4.3 Developing & Registering Execution Engines (`@register_engine` / `@register_api_engine`)

`EngineBase` (UI engine) and `ApiEngineBase` (API engine) isolate driver specifics:

1. **Subclass Built-in Cross-Platform UI Engines**:
   - Web / Electron: `uita.engine.playwright.engine.PlaywrightEngine`
   - macOS Native (AXUIElement): `uita.engine.mac_appkit.engine.MacAppKitEngine`
   - Windows Native (UIA / `pywinauto` / Qt Probe): `uita.engine.win_uia.engine.WinUIAEngine`
2. **Implement a Custom UI Engine (`EngineBase`)**:
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
3. **Register an HTTP / API Engine (`@register_api_engine` + `@register_api_auth`)**:
   - Subclass `uita.engine.http.engine.HttpEngine` (or `ApiEngineBase`) and register with `@register_api_engine("my_app", "universal")` to enable `--mode api-runner` or `switch[api]`.

---

### 4.4 App Lifecycle Policies (`@register_launch_policy`) & Installer Config (`InstallRegistry`)

```python
from uita.core.lifecycle.launch_policy import LaunchPolicyBase, register_launch_policy
from uita.core.platform.install_registry import InstallConfig, InstallRegistry


@register_launch_policy("my_app", "universal")
class MyAppLaunchPolicy(LaunchPolicyBase):
    @property
    def name(self) -> str:
        return "my_app_policy"

    async def wait_ui_ready(self, engine) -> None:
        # Wait for core UI elements after launch
        pass

    async def post_launch(self, engine) -> None:
        # Dismiss one-time onboarding modals after launch
        pass


# Optional: support `uita --mode install --app my_app --package-url <url>`
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

### 4.5 Pre-Test Setup (`pre_test.py`) & Probe Provisioning (`probe_provision.py`)

- **`apps/<app>/pre_test.py`** (invoked by `uita --mode pretest --app <app>`):
  ```python
  from uita.core.lifecycle.pretest import PretestContext, PretestResult

  async def main(ctx: PretestContext) -> PretestResult:
      return PretestResult(status="success", message="Pre-test data ready")
  ```
- **`apps/<app>/probe_provision.py`** (invoked by `uita --mode probe-provision --app <app>`):
  ```python
  from uita.core.lifecycle.probe_provision import ProbeProvisionContext, ProbeProvisionResult

  async def main(ctx: ProbeProvisionContext) -> ProbeProvisionResult:
      return ProbeProvisionResult(status="success", message="Probe provisioned")
  ```

---

### 4.6 Lifecycle Hooks (`@register_hook`) & Log Collectors (`@register_collector`)

1. **Execution Hooks (`HookBase`)**: Register via `@register_hook("my_hook")` and pass `--hooks my_hook` to run custom logic on `on_batch_start`, `on_case_start`, `on_step_end`, `on_case_result_ready`, and `on_batch_end`.
2. **App Log Collectors (`LogCollectorBase`)**: Register via `@register_collector("my_app", "universal")` to automatically archive crash dumps and client logs into `report/` at the end of a batch.

---

### 4.7 Performance Scenarios (`register_scenario`)

```python
# apps/my_app/perf/scenarios/cold_start.py
from uita.core.perf.scenarios.registry import register_scenario

SCENARIO_NAME = "cold_start"
SCENARIO_LABEL = "Cold Start Latency"
REPORT_KEYS = {"launch_ms": "Cold Start (ms)"}
AGGREGATE_PICK = {"launch_ms": "min"}


def measure(bridge, **opts) -> dict:
    return {"launch_ms": 420.0}


def to_aggregate_fields(result: dict) -> dict:
    return {"launch_ms": result["launch_ms"]}
```

---

### 4.8 Extending DSH Tools & MCP Capabilities (`@register_dsh_tool`)

Subclass `DshTool` under `src/uita/tools/dsh/tools/` and decorate with `@register_dsh_tool` to expose a tool simultaneously to:
1. DSH Planner LLM function calling;
2. MCP Server (`dsh mcp`) `list_tools` / `call_tool`;
3. DSH Web Console (`dsh serve`) tool debugger.

```python
from uita.tools.dsh.registry import DshTool, TIER_READ, ToolResult, register_dsh_tool


@register_dsh_tool
class MyCustomInspectTool(DshTool):
    name = "custom_inspect"
    tier = TIER_READ
    description = "Inspect custom diagnostic state of the target app"
    params = {
        "section": {"type": "string", "description": "Diagnostic section name"},
    }
    required = ("section",)

    async def run(self, ctx, *, section: str) -> ToolResult:
        return ToolResult(ok=True, data={"section": section, "status": "healthy"})
```

---

### 4.9 `.dsh/` AI Skills, Agents & Local 5-Dimensional Knowledge Graph

`.dsh/` contains three sibling components:

1. **`.dsh/skills/<skill_name>/SKILL.md`**: Reusable domain skills (`ui-automation`, `sentinel`, `kb-recall`).
2. **`.dsh/agents/<agent_name>.md`**: Specialized agent personas and prompt contracts (`ui-tester.md`, `sentinel.md`).
3. **`.dsh/knowledge/products/<app_name>/`**: Local 5-dimensional knowledge graph flywheel:
   ```text
   .dsh/knowledge/products/<app_name>/
   ├── business/                # Dimensions 1 & 2: Business Endpoints & Business Flows
   ├── architecture/            # Dimensions 3 & 4: Technical Endpoints & Technical Architecture
   ├── utest-rules/             # Dimension 5: Test Design Rules, 04-known-pitfalls.md, 06-run-history-digest.md, rules-meta.json
   ├── defects/                 # Historical defect propagation chains (<app_name>-defect-chain.md)
   └── kb-flywheel/             # Dead-knowledge governance ledger (dead-knowledge.json)
   ```
   - **Optional Defect Management Platform Integration**: During `sentinel` change risk analysis, you may optionally correlate issues from an external **Defect Management Platform** *(optional / can be ignored or self-integrated)*. Without an external platform, `sentinel` operates completely self-contained using `.dsh/knowledge/products/<app_name>/defects/` and the 5-dimensional knowledge graph.

---

### 4.10 Categorized Logging under Root `logs/` (`uita.core.logging_manager`)

Logs are routed by category into `<repo_root>/logs/` (overridable via `UITA_LOG_DIR`):

| Log File | Category | Logger Accessor | Logged Content |
| :--- | :--- | :--- | :--- |
| `logs/execution.log` | **Execution Log** | `get_execution_logger()` | Fallback chain routing decisions (`[ROUTE]`), per-level resolution attempts & hits (`[LOCATE_TRY]` / `[LOCATE_HIT]` with `level`, `selector`, `coords`, `bbox`), and action outcomes (`[ACTION_PASS]` / `[ACTION_FAIL]` with `click_coords` and `region=`) |
| `logs/actions.log` | **Actions Log** | `get_action_logger(name)` | Detailed arguments, retries, coordinate calculations, and verdicts for `click`, `type`, `scroll`, `assert`/`verify`, `wait`, `switch`/`switch_back`, and custom actions |
| `logs/kernel.log` | **Kernel Log** | `get_kernel_logger(comp)` | Interpreter scheduling, DSL parsing, cross-platform engines (Playwright / MacAppKit / WinUIA / HTTP), lifecycle stack, hooks, and DSH session logs |
| `logs/uita.log` | **Aggregated Log** | Automatic | Full chronological stream across all categories for end-to-end troubleshooting |
