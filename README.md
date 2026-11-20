# UITA-DSH

<p align="left">
  <strong>English</strong> | <a href="README_zh.md">简体中文</a>
</p>

**UITA-DSH (Universal Intelligent Test Architecture With Deterministic Skill Harness & Cross-Platform UI Automation Runtime)** is a unified intelligent test architecture and runtime that deeply integrates **AI Exploratory Planning (DSH)**, **Controlled Risk-Driven Unit Testing & Live Verification (Sentinel)**, **a Local 5-Dimensional Knowledge Graph Flywheel (`.dsh/knowledge`)**, and **Deterministic Cross-Platform UI Automation Execution (TCS / TestCaseScript + Rosetta Multi-Level Self-Healing Resolution)**.

- **AI Exploration → Deterministic Compilation Closed Loop**: Drive application interactions through natural language goals or plain-text test cases, automatically record structured intermediate representations (**Steps IR**, `.ir.json`), and compile them into deterministic `.tcs` (`TestCaseScript`) scripts with one command — requiring **0 LLM tokens** during regression runs.
- **Controlled 9-Step Change-Driven Unit Testing & Verification (`sentinel`)**: Built for PR/MR/commit or requirement-driven code changes, executing an immutable 9-step state machine covering requirement decomposition, 100% diff skeleton extraction, local knowledge graph recall, risk routing, real behavioral red/green adversarial unit test verification, multimodal live verification, and guarded self-healing knowledge write-back. Supports optional integration with an external **Defect Management Platform** *(Note: optional / can be ignored or self-integrated to improve business correlation accuracy during risk analysis)*.
- **Local 5-Dimensional Knowledge Graph Dual-Quality Flywheel (`.dsh/knowledge`)**: Placed alongside `.dsh/skills/` and `.dsh/agents/`, providing zero-external-dependency recall (`kb_recall`) and `<!-- kb:auto:start/end -->` guarded write-back (`kb_writeback`) across five dimensions: Business Endpoints, Business Flows, Technical Endpoints, Technical Architecture, and Test Design.
- **Unified Multi-Engine Abstraction**: Supports Web / Electron (Playwright CDP), macOS Native (AppKit AXUIElement), Windows Native (UIA default `uiautomation` + optional `pywinauto` backend + Qt UI Probe + `window_hint` multi-window scoping), and HTTP API.
- **Unified `L1 ~ L9` Integer-Level Element Resolution & Two-Tier Custom Fallback Chains (`Resolver` & `ActionRouter`)**:
  - Unified integer levels: `L1` (`MAP_EXACT` exact selector) → `L2` (`SEMANTIC` similarity) → `L3` (`TEXT_MATCH` visible text) → `L4` (`LABEL_MATCH` associated label) → `L5` (`ROLE_MATCH` accessibility role) → `L6` (`LLM_SELECTOR` DOM/tree LLM inference) → `L7` (`VISUAL_FP` visual fingerprint matching & assertion) → `L8` (`LLM_VISUAL` OCR & vision LLM locating/judging) → `L9` (`QT_PROBE` in-process Qt object probe).
  - Supports **App-Level** (`__default__` in `router_proxy.json`, `_meta.default_strategy_chain` in `rosetta_map.json`, or `AppPlatformConfig.extra["default_strategy_chain"]`) and **Element-Level** (per-element ID in `router_proxy.json` or `strategy_chain` in `rosetta_map.json`) custom fallback chains.
- **Unified Model Credential Configuration**: All LLM/VLM calls across the framework (DSH Planner, L6/L8 visual locating & judging, performance analysis) read from two unified environment variables: `DSH_API_URL` and `DSH_API_KEY`.
- **Full Extension & Onboarding Guide**: See [`docs/framework_en.md`](docs/framework_en.md) (Chinese: [`docs/framework.md`](docs/framework.md)) for app onboarding, all 14 registries & decorators, two-tier custom fallback chains, DSH tools, and knowledge graph integration.

---

## 1. Quick Start

### 1.1 Install Dependencies

```bash
pip install -e ".[dev]"
playwright install chromium
```

### 1.2 Configure Model Environment Variables

Only `DSH_API_URL` (compatible with any OpenAI-style `chat/completions` endpoint) and `DSH_API_KEY` are required:

```bash
export DSH_API_URL="https://api.example.com/v1/chat/completions"
export DSH_API_KEY="sk-your-api-key"
export UITA_DSH_MODEL="default-model"
```

Run the environment check to verify your setup:

```bash
uita --mode env_check
```

### 1.3 Run Sample Cases & DSH Console

```bash
# 1. Static lint & dry-run AST parse on the sample TCS (TestCaseScript) case
uita --mode parse --cases-dir cases/sample_web/smoke_search.tcs
uita --mode lint --app sample_web

# 2. Launch the local DSH Web Console (defaults to 127.0.0.1:8765)
dsh serve --app sample_web

# 3. Run a natural-language test goal headlessly and auto-export Steps IR & TCS
dsh run --app sample_web --goal "Verify that the home page heading is visible" --ir-out cases/sample_web/ir/home.ir.json --tcs-out cases/sample_web/home.tcs

# 4. Replay the frozen IR with zero LLM cost
dsh replay cases/sample_web/ir/home.ir.json
```

---

## 2. Repository Structure

```text
uita-dsh/
├── pyproject.toml
├── .env.example
├── README.md                    # Primary documentation (English, GitHub default)
├── README_zh.md                 # Primary documentation (Simplified Chinese)
├── .dsh/                        # Skills, Agents, and Local Knowledge Graph (sibling directories)
│   ├── skills/
│   │   ├── ui-automation/SKILL.md
│   │   ├── kb-recall/SKILL.md   # Local 5-dimensional knowledge graph recall & write-back skill
│   │   └── sentinel/            # Risk-driven unit test design & live verification skill (9-step state machine)
│   ├── agents/
│   │   ├── ui-tester.md
│   │   └── sentinel.md
│   └── knowledge/               # Local 5-dimensional knowledge graph & dual-quality flywheel
│       └── products/
│           └── sample_web/      # Sample product 5-dim docs, known pitfalls, defect chains & dead-knowledge ledger
├── ci/                          # Sentinel deterministic scaffolding, red/green contracts, risk ledger & KB flywheel scripts
├── apps/                        # Target app configurations & Rosetta semantic element maps
│   └── sample_web/
│       ├── configs.py
│       ├── register.py
│       ├── rosetta_map.json     # Semantic element map (with _meta.default_strategy_chain & per-element strategy_chain)
│       ├── router_proxy.json    # App-level (__default__) & element-level fallback chain configuration example
│       └── nav_map.json
├── docs/
│   ├── framework_en.md          # Complete Framework Extension & Onboarding Guide (English)
│   └── framework.md             # Complete Framework Extension & Onboarding Guide (Chinese)
├── cases/                       # Deterministic TCS (TestCaseScript) cases & Steps IR
│   └── sample_web/
│       └── smoke_search.tcs
├── src/uita/
│   ├── __main__.py              # Unified CLI entry point (`uita`)
│   ├── config.py                # Global configuration (unified DSH_API_URL / DSH_API_KEY)
│   ├── cli/                     # CLI mode handlers
│   ├── core/                    # Core runtime (DSL, Resolver, Rosetta, Lifecycle, Hooks, Perf)
│   ├── engine/                  # Cross-platform engines (playwright / mac_appkit / win_uia / http)
│   └── tools/
│       ├── dsh/                 # DSH orchestrator (Web Console / Headless / MCP / IR Compiler)
│       └── rosetta_diff/        # Rosetta element probe & diff tool
└── tests/                       # Unit test suite (Core runtime, DSH orchestrator, Engines & Sentinel CI tools)
```

---

## 3. Unit Testing & Quality Verification

UITA-DSH includes a two-tier testing architecture: **the framework's own unit test suite (`tests/`)** and **the risk-driven unit test design contract for target code changes (`sentinel`)**.

### 3.1 Run Framework Unit Tests (`pytest`)

All unit tests are offline-isolated (using mocks / `tmp_path`) with no external network, live LLM, or desktop GUI dependencies required:

```bash
# Run the full unit test suite (DSL, Rosetta, Resolver, Playwright/Mac/WinUIA engines, DSH & Sentinel CI tools)
pytest -q

# Run module-specific test suites
pytest tests/core/ -q                                   # Core runtime, remote Rosetta store interfaces & window_hint tests
pytest tests/core/engine/test_win_uia_backend.py -q     # Windows UIA uiautomation / pywinauto backend contract tests
pytest tests/tools/dsh/ -q                              # DSH toolchain, IR compiler, MCP & session management tests
pytest tests/test_ci_sentinel_*.py -q                   # Sentinel red/green contract, evidence & local KB flywheel tests
```

### 3.2 `sentinel` Change-Driven Unit Test Design & Red/Green Contract

For code changes (PR / MR / commit / branch), `sentinel` (`.dsh/skills/sentinel/`) enforces a deterministic unit test design and verification closed loop:

1. **Knowledge Recall & Risk Analysis**: Recalls 5-dimensional product knowledge and known pitfalls from `.dsh/knowledge/products/<product>/`, combining 100% diff skeleton extraction with optional **Defect Management Platform** correlation *(optional / can be ignored or self-integrated)* to identify change risks `R<n>`.
2. **Adversarial Red/Green (`adversarial`) + Lock Green (`lock`) Dual Contract**:
   - Executes `init → validate → freeze → run-batch → seal` via `ci/sentinel_test_design.py`;
   - Strictly verifies real test runner exit codes and JUnit XML artifacts, distinguishing genuine behavioral assertion failures (`AssertionError`) from environment/setup errors.
3. **Guarded Knowledge Write-Back**: After verification, `ci/sentinel_kb_writeback.py` incrementally writes new pitfalls, defect propagation chains, and 5-dimensional facts into `<!-- kb:auto:start/end -->` guarded blocks under `.dsh/knowledge/products/<product>/`.

---

## 4. Extension Interfaces (External Subsystem Integration)

All vendor-specific subsystems have been decoupled from this repository, leaving clean standard interfaces for custom reporting, notifications, remote storage, or vision models.

### 4.1 Custom Reporting & Notification Hooks (`HookBase`)

Subclass `uita.core.hooks.base.HookBase` and register via `@register_hook` to enable with `--hooks <name>`:

```python
from uita.core.hooks.base import HookBase, HookContext
from uita.core.hooks.registry import register_hook

@register_hook("custom_report")
class CustomReportHook(HookBase):
    async def on_case_result_ready(self, ctx: HookContext) -> None:
        # Standard result.json and screenshot artifacts are ready under ctx.report_dir
        pass

    async def on_batch_end(self, ctx: HookContext) -> None:
        # Upload batch summary or send webhook notifications when a batch finishes
        pass
```

### 4.2 Custom Vision / LLM Client (`VisionLLMClient`)

By default, `uita.tools.dsh.vision.DshVisionClient` reuses `DSH_API_URL` and `DSH_API_KEY`. To integrate a custom vision protocol, implement `uita.core.drivers.llms.base.VisionLLMClient`:

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

### 4.3 Custom Remote Database Storage for Rosetta Maps & Router Proxy (Interface Contract & Examples)

By default, the framework uses local JSON storage (`LocalJsonRosettaStore` and `LocalJsonRouterProxyStore`, reading `apps/<app>/rosetta_map.json` and `router_proxy.json`).
To host element maps and dynamic routing strategies in a remote database (e.g., MySQL, PostgreSQL, or a configuration center), `uita.core.rosetta` provides abstract base classes with automatic local JSON fallback (**no vendor-specific database driver or schema is bundled**):

- **Remote Element Map Interface**: `uita.core.rosetta.base.RemoteDbRosettaStoreBase` (inherits from `RosettaStoreBase`)
- **Remote Router Proxy Interface**: `uita.core.rosetta.router_proxy_base.RemoteDbRouterProxyStoreBase` (inherits from `RouterProxyStoreBase`)
- **Automatic Local Fallback**: `ElementMapLoader` and `RouterProxyLoader` wrap stores with `DbFallbackRosettaStore` / `DbFallbackRouterProxyStore`, automatically falling back to local JSON when the remote database is unconfigured, times out, or raises an error.

#### Implementation Example (User Extension Code)

```python
# my_project/rosetta_db.py
from typing import Any, Optional
from uita.core.rosetta.base import RemoteDbRosettaStoreBase
from uita.core.rosetta.router_proxy_base import RemoteDbRouterProxyStoreBase


class MyDbRosettaStore(RemoteDbRosettaStoreBase):
    """Custom remote database store for Rosetta maps (reads UITA_REMOTE_DB_URL via self.db_url)."""

    def fetch_from_db(self, app_name: str) -> Optional[dict]:
        # Example: query the rosetta_map dict for app_name from your database
        # return db_client.query_rosetta_map(dsn=self.db_url, app=app_name)
        raise NotImplementedError

    def upsert_heal_to_db(
        self,
        app_name: str,
        os_name: str,
        records: list[Any],
        reset_ids: Optional[set[str]] = None,
    ) -> int:
        # Optional: persist runtime self-healed selectors back to the remote database
        return 0


class MyDbRouterProxyStore(RemoteDbRouterProxyStoreBase):
    """Custom remote database store for Router Proxy strategies."""

    def fetch_from_db(self, app_name: str) -> Optional[dict]:
        # Example: query router_proxy dict ({element_id: {"default": ..., "runtime_define": ...}})
        # return db_client.query_router_proxy(dsn=self.db_url, app=app_name)
        raise NotImplementedError

    def upsert_routes_to_db(
        self,
        app_name: str,
        os_name: str,
        routes: dict[str, str],
        reset_ids: Optional[set[str]] = None,
    ) -> int:
        # Optional: persist runtime self-healed optimal routing strategies to the remote database
        return 0
```

#### Activation Options

**Option A: Via Environment Variables (Zero Code Changes)**

```bash
export UITA_USE_LOCAL_ROSETTA=0
export UITA_REMOTE_DB_URL="mysql+pymysql://user:password@db.example.com:3306/uita_rosetta"
export UITA_ROSETTA_STORE_CLASS="my_project.rosetta_db:MyDbRosettaStore"
export UITA_ROUTER_PROXY_STORE_CLASS="my_project.rosetta_db:MyDbRouterProxyStore"
```

**Option B: Programmatic Registration in `apps/<app>/register.py`**

```python
from uita.core.rosetta.base import register_rosetta_store
from uita.core.rosetta.router_proxy_base import register_router_proxy_store
from my_project.rosetta_db import MyDbRosettaStore, MyDbRouterProxyStore

register_rosetta_store(MyDbRosettaStore)
register_router_proxy_store(MyDbRouterProxyStore)
```

### 4.4 Windows UIA Backend Switching (`uiautomation` / `pywinauto`) & `window_hint`

`WinUIAEngine` abstracts the `WindowBackend` contract (`src/uita/engine/win_uia/window_contract.py`), supporting two in-window control search backends:
- `uiautomation` (default): Native DFS control traversal.
- `pywinauto` (optional): Searches controls via `UIAElementInfo(hwnd).descendants()`, with built-in `win32ui` stub protection when MFC runtime (`mfc140u.dll`) is absent; automatically falls back to `uiautomation` if uninstalled or on error.

How to enable:
1. Install optional dependency: `pip install -e ".[win-pywinauto]"`
2. Set `export UIA_WIN_BACKEND=pywinauto` or configure `AppPlatformConfig.extra["win_backend"] = "pywinauto"` in `apps/<app>/configs.py`.
3. Declare cross-window / multi-top-level-window scoping in `rosetta_map.json` via `"window_hint": "main"`, `"window_hint": "title=Settings"`, or `{"title": "...", "class": "...", "process": "..."}`.

---

## 5. Copyright & Open-Source Notice

- **Author**: **ist3venyang**
- **Ownership Disclaimer**: This project is independently designed, authored, and maintained by the individual developer **ist3venyang**. **It is a personal open-source project and is not affiliated with or owned by any commercial organization or enterprise.** It contains no proprietary code, internal service endpoints, business data, or restricted assets from any commercial entity.
- **License**: Released under the [MIT License](https://opensource.org/licenses/MIT).

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
