"""`ci/sentinel_kb_writeback.py`（sentinel W3 机器区块协议）单测。

覆盖三条硬契约（对齐 `reference/08-self-heal-writeback.md`「知识正文改写」小节）：
  1. **越界即 stop**：区块外任何字节差异都不得发生（含协议损坏、嵌套标记、密钥特征）；
  2. **幂等**：同 id 同内容重跑 ⇒ 文件字节不变、台账 `unchanged`（冲突块也不得一次一个时间戳）；
  3. **可回滚**：每次改写落 `kb-writeback.json`（前后 sha256）与原文件副本。

全程用 `tmp_path` 自造工作区，不依赖网络 / 真实知识库 / 报告目录。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
CI_DIR = ROOT_DIR / "ci"
if str(CI_DIR) not in sys.path:
    sys.path.insert(0, str(CI_DIR))


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, CI_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    # 必须先进 sys.modules：脚本用了 `from __future__ import annotations` + dataclass，
    # 后者解析字段类型时会回查 `sys.modules[cls.__module__]`（缺失即 AttributeError）。
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


wb = _load("sentinel_kb_writeback", "sentinel_kb_writeback.py")

#: 知识目录名 = uita 侧产品短码（如 `sample_web`）。
PRODUCT = "sample_web"
RUN_KEY = "proj/sample_web!258#20261016-173429"
PITFALLS = f".dsh/knowledge/products/{PRODUCT}/utest-rules/04-known-pitfalls.md"
DEFECT_CHAIN = f".dsh/knowledge/products/{PRODUCT}/defects/{PRODUCT}-defect-chain.md"
DIGEST = f".dsh/knowledge/products/{PRODUCT}/utest-rules/06-run-history-digest.md"


def _mk_workspace(tmp_path: Path) -> Path:
    root = tmp_path / "uita"
    (root / ".dsh/knowledge/products" / PRODUCT / "utest-rules").mkdir(parents=True)
    return root


def _write_blocks(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _input_file(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "blocks.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _run(main_args: list[str]):
    return wb.main(main_args)


def _args(root: Path, report_dir: Path, input_path: Path, *extra: str) -> list[str]:
    return [
        "--workspace-root", str(root),
        "--report-dir", str(report_dir),
        "--run-key", RUN_KEY,
        "--product-code", PRODUCT,
        "--input", str(input_path),
        *extra,
    ]


def _args_with_product(root: Path, report_dir: Path, input_path: Path, product: str) -> list[str]:
    return [
        "--workspace-root", str(root),
        "--report-dir", str(report_dir),
        "--run-key", RUN_KEY,
        "--product-code", product,
        "--input", str(input_path),
    ]


# ─────────────────── 1. 白名单新文件：整文件机器创建 ─────────────────── #

def test_creates_whitelisted_new_file(tmp_path: Path, capsys) -> None:
    root = _mk_workspace(tmp_path)
    report_dir = tmp_path / "report"
    payload = {
        "rules_version": 21,
        "summary": "本轮回写 1 条缺陷形态",
        "blocks": [{"file": DEFECT_CHAIN, "id": "defect-chain/t8-compat-guid", "content": "### T8\n- 现象：guid 空值回落"}],
    }
    code = _run(_args(root, report_dir, _input_file(tmp_path, payload)))
    out = capsys.readouterr().out
    assert code == 0
    assert "status=ok" in out

    text = (root / DEFECT_CHAIN).read_text(encoding="utf-8")
    assert text.startswith(f"# {PRODUCT} · 缺陷链（机器维护）")
    assert f"<!-- kb:auto id=defect-chain/t8-compat-guid run={RUN_KEY} " in text
    assert "<!-- /kb:auto -->" in text

    report = json.loads((report_dir / "kb-writeback.json").read_text(encoding="utf-8"))
    assert report["schema"] == "sentinel-kb-writeback/v1"
    assert report["rules_version"] == 21
    assert report["status"] == "ok"
    entry = report["files"][0]
    assert entry["action"] == "created"
    assert entry["before_sha256"] is None
    assert entry["after_sha256"] == wb.sha256_text(text)
    # 新文件不产生备份（原本不存在，无可回滚内容）
    assert entry["backup_path"] is None


def test_new_file_header_is_fixed_template(tmp_path: Path) -> None:
    """调用方塞不进「区块外内容」——新文件头由脚本生成。"""
    root = _mk_workspace(tmp_path)
    payload = {
        "blocks": [{"file": DIGEST, "id": "digest/run", "content": "本轮 3 条风险"}],
    }
    code = _run(_args(root, tmp_path / "report", _input_file(tmp_path, payload)))
    assert code == 0
    text = (root / DIGEST).read_text(encoding="utf-8")
    assert "只允许改写 `kb:auto` 区块内部" in text
    # 区块被挖空后，区块外只剩固定模板里的那一次 `kb:auto` 说明文字
    assert wb._outside_blocks(text).count("kb:auto") == 1


# ─────────────────── 2. 既有文件：仅区块内可改 ─────────────────── #

def test_appends_block_keeping_outside_bytes(tmp_path: Path) -> None:
    root = _mk_workspace(tmp_path)
    original = "# 历史 bug 形态\n\n| bug_id | 结论 |\n|---|---|\n| 111 | x |\n"
    _write_blocks(root, PITFALLS, original)
    payload = {"blocks": [{"file": PITFALLS, "id": "pitfalls/rules-version", "content": "rules_version=21"}]}
    code = _run(_args(root, tmp_path / "report", _input_file(tmp_path, payload)))
    assert code == 0
    text = (root / PITFALLS).read_text(encoding="utf-8")
    assert text.startswith(original)  # 人工正文（区块外）逐字节保留
    assert wb._outside_blocks(text) == original + "\n\n"
    assert "pitfalls/rules-version" in text


def test_replaces_block_in_place(tmp_path: Path) -> None:
    root = _mk_workspace(tmp_path)
    header = "# 标题\n\n人工段落\n"
    old_block = wb.render_block("pitfalls/rules-version", "rules_version=20", run_key="old", at="t0")
    _write_blocks(root, PITFALLS, f"{header}\n{old_block}\n\n人工尾注\n")
    before_outside = wb._outside_blocks((root / PITFALLS).read_text(encoding="utf-8"))

    payload = {"blocks": [{"file": PITFALLS, "id": "pitfalls/rules-version", "content": "rules_version=21"}]}
    code = _run(_args(root, tmp_path / "report", _input_file(tmp_path, payload)))
    assert code == 0
    text = (root / PITFALLS).read_text(encoding="utf-8")
    assert "rules_version=20" not in text
    assert "rules_version=21" in text
    assert wb._outside_blocks(text) == before_outside  # 原位替换不动区块外

    report = json.loads((tmp_path / "report" / "kb-writeback.json").read_text(encoding="utf-8"))
    block = report["files"][0]["blocks"][0]
    assert block["status"] == "replaced"
    assert block["before_sha256"] == wb.sha256_text("rules_version=20")
    assert block["after_sha256"] == wb.sha256_text("rules_version=21")


# ─────────────────── 3. 幂等 ─────────────────── #

def test_rerun_same_content_is_byte_identical(tmp_path: Path, capsys) -> None:
    root = _mk_workspace(tmp_path)
    _write_blocks(root, PITFALLS, "# 标题\n")
    payload = {"blocks": [{"file": PITFALLS, "id": "pitfalls/rules-version", "content": "rules_version=21"}]}
    input_path = _input_file(tmp_path, payload)
    report_dir = tmp_path / "report"

    assert _run(_args(root, report_dir, input_path)) == 0
    first = (root / PITFALLS).read_text(encoding="utf-8")
    first_hash = wb.sha256_text(first)
    # 第二次换一个 at（run 属性不变）——同 id 同内容必须一个字节都不动
    assert _run(_args(root, report_dir, input_path)) == 0
    capsys.readouterr()
    assert wb.sha256_text((root / PITFALLS).read_text(encoding="utf-8")) == first_hash

    report = json.loads((report_dir / "kb-writeback.json").read_text(encoding="utf-8"))
    assert report["status"] == "no_change"
    assert report["totals"]["files_changed"] == 0
    assert report["files"][0]["action"] == "unchanged"
    # 第二次不覆盖备份：备份仍指向首次写入前的状态
    assert report["files"][0]["backup_path"] is None


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    root = _mk_workspace(tmp_path)
    _write_blocks(root, PITFALLS, "# 标题\n")
    payload = {"blocks": [{"file": PITFALLS, "id": "pitfalls/rules-version", "content": "v=21"}]}
    report_dir = tmp_path / "report"
    assert _run(_args(root, report_dir, _input_file(tmp_path, payload), "--dry-run")) == 0
    assert not (root / PITFALLS).read_text(encoding="utf-8").count("kb:auto")
    assert not (report_dir / "kb-writeback.json").exists()


# ─────────────────── 4. 冲突：两条都保留 ─────────────────── #

def test_conflict_keeps_both_and_is_idempotent(tmp_path: Path) -> None:
    root = _mk_workspace(tmp_path)
    _write_blocks(root, PITFALLS, "# 标题\n")
    _run(_args(root, tmp_path / "report", _input_file(tmp_path, {
        "blocks": [{"file": PITFALLS, "id": "pitfalls/guid", "content": "旧结论：guid 取 native"}],
    })))
    # 新结论与旧结论冲突：显式 conflict=True ⇒ 旧块保留，新块另起 id
    conflict_input = _input_file(tmp_path, {
        "blocks": [{"file": PITFALLS, "id": "pitfalls/guid", "content": "新结论：native 缺失时回落缓存 ID", "conflict": True}],
    })
    code = _run(_args(root, tmp_path / "report", conflict_input))
    assert code == 0
    text = (root / PITFALLS).read_text(encoding="utf-8")
    assert "旧结论：guid 取 native" in text
    assert "新结论：native 缺失时回落缓存 ID" in text
    assert text.count("<!-- /kb:auto -->") == 2

    report = json.loads((tmp_path / "report" / "kb-writeback.json").read_text(encoding="utf-8"))
    assert report["totals"]["conflicts"] == 1
    assert report["conflicts"][0]["id"] == "pitfalls/guid"
    assert report["conflicts"][0]["reason"] == "explicit"

    # 冲突结论重跑：不得再追加第三个块（一次一个时间戳 = 无限膨胀）
    snapshot = wb.sha256_text(text)
    assert _run(_args(root, tmp_path / "report", conflict_input)) == 0
    assert wb.sha256_text((root / PITFALLS).read_text(encoding="utf-8")) == snapshot


# ─────────────────── 5. 越界 / 协议违规即 stop ─────────────────── #

def test_rejects_file_outside_whitelist(tmp_path: Path, capsys) -> None:
    root = _mk_workspace(tmp_path)
    payload = {"blocks": [{"file": f".dsh/knowledge/products/{PRODUCT}/workflow/01-x.md", "id": "x", "content": "y"}]}
    code = _run(_args(root, tmp_path / "report", _input_file(tmp_path, payload)))
    assert code == 2
    assert "不在机器可写白名单" in capsys.readouterr().err


def test_existing_file_not_creatable_whitelisted_target(tmp_path: Path, capsys) -> None:
    """不存在的**既有类**文件（04-known-pitfalls）不得被整文件创建。"""
    root = _mk_workspace(tmp_path)
    payload = {"blocks": [{"file": PITFALLS, "id": "x", "content": "y"}]}
    code = _run(_args(root, tmp_path / "report", _input_file(tmp_path, payload)))
    assert code == 2
    assert "不属于可整文件创建的白名单" in capsys.readouterr().err
    assert not (root / PITFALLS).exists()


def test_nested_marker_is_stopped(tmp_path: Path) -> None:
    root = _mk_workspace(tmp_path)
    _write_blocks(root, PITFALLS, "# 标题\n")
    payload = {"blocks": [{"file": PITFALLS, "id": "x", "content": "内容 <!-- /kb:auto --> 越狱"}]}
    report_dir = tmp_path / "report"
    code = _run(_args(root, report_dir, _input_file(tmp_path, payload)))
    assert code == 1
    report = json.loads((report_dir / "kb-writeback.json").read_text(encoding="utf-8"))
    assert report["status"] == "stopped"
    assert report["out_of_scope_changes"] == [f"{PITFALLS}#x:nested-marker"]
    assert "kb:auto" not in (root / PITFALLS).read_text(encoding="utf-8")


def test_broken_protocol_in_existing_file_is_stopped(tmp_path: Path) -> None:
    root = _mk_workspace(tmp_path)
    _write_blocks(root, PITFALLS, "# 标题\n\n<!-- kb:auto id=a run=r at=t -->\n正文但没闭合\n")
    payload = {"blocks": [{"file": PITFALLS, "id": "a", "content": "新内容"}]}
    report_dir = tmp_path / "report"
    code = _run(_args(root, report_dir, _input_file(tmp_path, payload)))
    assert code == 1
    report = json.loads((report_dir / "kb-writeback.json").read_text(encoding="utf-8"))
    assert "未闭合" in report["reason"]


def test_secret_like_content_is_stopped(tmp_path: Path) -> None:
    root = _mk_workspace(tmp_path)
    _write_blocks(root, PITFALLS, "# 标题\n")
    payload = {"blocks": [{"file": PITFALLS, "id": "x", "content": "password: abcdefghijklmnopq"}]}
    code = _run(_args(root, tmp_path / "report", _input_file(tmp_path, payload)))
    assert code == 1


def test_partial_write_is_prevented(tmp_path: Path) -> None:
    """同一批里第二个文件越界 ⇒ 第一个文件也不得被写（两阶段提交）。"""
    root = _mk_workspace(tmp_path)
    _write_blocks(root, PITFALLS, "# 标题\n")
    payload = {
        "blocks": [
            {"file": PITFALLS, "id": "ok", "content": "正常内容"},
            {"file": DIGEST, "id": "bad", "content": "越狱 <!-- kb:auto id=z run=r at=t -->"},
        ],
    }
    code = _run(_args(root, tmp_path / "report", _input_file(tmp_path, payload)))
    assert code == 1
    assert "kb:auto" not in (root / PITFALLS).read_text(encoding="utf-8")


# ─────────────────── 6. 回滚依据 ─────────────────── #

def test_backup_enables_rollback(tmp_path: Path) -> None:
    root = _mk_workspace(tmp_path)
    original = "# 标题\n\n人工内容\n"
    _write_blocks(root, PITFALLS, original)
    report_dir = tmp_path / "report"
    payload = {"blocks": [{"file": PITFALLS, "id": "x", "content": "机器内容"}]}
    assert _run(_args(root, report_dir, _input_file(tmp_path, payload))) == 0

    report = json.loads((report_dir / "kb-writeback.json").read_text(encoding="utf-8"))
    entry = report["files"][0]
    assert entry["before_sha256"] == wb.sha256_text(original)
    backup = report_dir / entry["backup_path"]
    assert backup.read_text(encoding="utf-8") == original
    # 用备份可直接回滚
    (root / PITFALLS).write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
    assert (root / PITFALLS).read_text(encoding="utf-8") == original


def test_block_id_must_be_unique_within_input(tmp_path: Path, capsys) -> None:
    root = _mk_workspace(tmp_path)
    _write_blocks(root, PITFALLS, "# 标题\n")
    payload = {
        "blocks": [
            {"file": PITFALLS, "id": "dup", "content": "a"},
            {"file": PITFALLS, "id": "dup", "content": "b"},
        ],
    }
    code = _run(_args(root, tmp_path / "report", _input_file(tmp_path, payload)))
    assert code == 2
    assert "id 重复" in capsys.readouterr().err


# ─────────────────── 7. rules_version 只读语义 ─────────────────── #

def test_rules_version_read_from_meta_is_authoritative(tmp_path: Path) -> None:
    """`rules-meta.json` 是权威且**只读**：输入里的版本号只作对照，且不改该文件。

    实测`rules_version` 是 `"v29"` 这类**字符串**（见 `sample_web/utest-rules/rules-meta.json`），
    故必须原样透传 —— 强制转 int 会把权威值读成「未知」。
    """
    root = _mk_workspace(tmp_path)
    _write_blocks(root, PITFALLS, "# 标题\n")
    meta = wb.rules_meta_path(root, PRODUCT)
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta_text = json.dumps(
        {"product_code": PRODUCT, "rules_version": "v29", "other": "keep-me"},
        ensure_ascii=False,
    )
    meta.write_text(meta_text, encoding="utf-8")

    payload = {
        "rules_version": 99,
        "blocks": [{"file": PITFALLS, "id": "x", "content": "机器内容"}],
    }
    report_dir = tmp_path / "report"
    assert _run(_args(root, report_dir, _input_file(tmp_path, payload))) == 0
    report = json.loads((report_dir / "kb-writeback.json").read_text(encoding="utf-8"))
    assert report["rules_version"] == "v29"
    assert report["rules_version_source"] == "rules-meta.json"
    assert report["rules_version_declared"] == 99
    # 只读：meta 文件字节不变（W3 不碰 rules-meta.json）
    assert meta.read_text(encoding="utf-8") == meta_text


def test_rejects_unknown_product_directory(tmp_path: Path, capsys) -> None:
    """传外部编码（`sample-web-ext`）而非知识目录名（`sample_web`）必须被拦下。
    """
    root = _mk_workspace(tmp_path)
    payload = {"blocks": [{"file": DEFECT_CHAIN, "id": "x", "content": "内容"}]}
    code = _run(_args_with_product(
        root, tmp_path / "report", _input_file(tmp_path, payload), "sample-web-ext",
    ))
    assert code == 2
    err = capsys.readouterr().err
    assert "知识目录不存在" in err
    assert PRODUCT in err  # 列出可用目录，便于纠正入参
    assert not (root / ".dsh/knowledge/products/sample-web-ext").exists()


def test_rejects_product_code_mismatch_with_rules_meta(tmp_path: Path, capsys) -> None:
    root = _mk_workspace(tmp_path)
    (root / ".dsh/knowledge/products" / PRODUCT / "utest-rules").mkdir(
        parents=True, exist_ok=True,
    )
    wb.rules_meta_path(root, PRODUCT).write_text(
        json.dumps({"product_code": "some-other-product"}), encoding="utf-8",
    )
    payload = {"blocks": [{"file": PITFALLS, "id": "x", "content": "内容"}]}
    code = _run(_args(root, tmp_path / "report", _input_file(tmp_path, payload)))
    assert code == 2
    assert "product_code 不一致" in capsys.readouterr().err


def test_rules_version_unknown_when_meta_missing_or_broken(tmp_path: Path) -> None:
    root = _mk_workspace(tmp_path)
    _write_blocks(root, PITFALLS, "# 标题\n")
    payload = {"blocks": [{"file": PITFALLS, "id": "x", "content": "机器内容"}]}
    report_dir = tmp_path / "report"
    assert _run(_args(root, report_dir, _input_file(tmp_path, payload))) == 0
    report = json.loads((report_dir / "kb-writeback.json").read_text(encoding="utf-8"))
    assert report["rules_version"] is None
    assert report["rules_version_source"] == "unknown"

    meta = wb.rules_meta_path(root, PRODUCT)
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text("{ 坏 JSON", encoding="utf-8")
    assert wb.read_rules_version(root, PRODUCT) is None

# ─────────────── 6. 知识库五维（业务端点/业务流程/技术端点/技术架构/测试设计） ─────────────── #

FIVE_DIMENSION_FILES = (
    f".dsh/knowledge/products/{PRODUCT}/business/business-endpoints.md",
    f".dsh/knowledge/products/{PRODUCT}/business/business-flows.md",
    f".dsh/knowledge/products/{PRODUCT}/architecture/technical-endpoints.md",
    f".dsh/knowledge/products/{PRODUCT}/architecture/technical-architecture.md",
    f".dsh/knowledge/products/{PRODUCT}/utest-rules/07-test-design.md",
)


def test_creates_five_dimension_knowledge_files(tmp_path: Path) -> None:
    """五维知识文件属白名单：允许整文件机器创建，文件头为固定模板。"""
    root = _mk_workspace(tmp_path)
    payload = {
        "blocks": [
            {"file": rel, "id": f"kb-dim/{rel.rsplit('/', 1)[-1]}", "content": f"### {rel}\n- 事实"}
            for rel in FIVE_DIMENSION_FILES
        ]
    }
    report_dir = tmp_path / "report"
    assert _run(_args(root, report_dir, _input_file(tmp_path, payload))) == 0
    report = json.loads((report_dir / "kb-writeback.json").read_text(encoding="utf-8"))
    assert report["totals"]["files_changed"] == len(FIVE_DIMENSION_FILES)
    assert {item["action"] for item in report["files"]} == {"created"}
    for rel in FIVE_DIMENSION_FILES:
        text = (root / rel).read_text(encoding="utf-8")
        assert text.startswith(f"# {PRODUCT} · ")
        assert "只允许改写 `kb:auto` 区块内部" in text or "只允许改写 `kb:auto` " in text


def test_five_dimension_headers_are_distinct_templates(tmp_path: Path) -> None:
    root = _mk_workspace(tmp_path)
    payload = {
        "blocks": [
            {"file": rel, "id": "kb-dim/x", "content": "事实"} for rel in FIVE_DIMENSION_FILES
        ]
    }
    assert _run(_args(root, tmp_path / "report", _input_file(tmp_path, payload))) == 0
    titles = [(root / rel).read_text(encoding="utf-8").splitlines()[0] for rel in FIVE_DIMENSION_FILES]
    assert len(set(titles)) == len(FIVE_DIMENSION_FILES)
    joined = " ".join(titles)
    for keyword in ("业务端点", "业务流程", "技术端点", "技术架构", "测试设计"):
        assert keyword in joined


def test_five_dimension_existing_file_only_block_editable(tmp_path: Path) -> None:
    """已存在的五维文件只允许区块内改写：业务写的区块外内容逐字节保留。"""
    root = _mk_workspace(tmp_path)
    rel = FIVE_DIMENSION_FILES[0]
    original = "# 业务端点\n\n| 端点 | 入口 |\n|---|---|\n| 导出相册 | 右键菜单 |\n"
    _write_blocks(root, rel, original)
    payload = {"blocks": [{"file": rel, "id": "kb-dim/business-endpoint", "content": "### 导出相册\n- 后端模块 main/export"}]}
    assert _run(_args(root, tmp_path / "report", _input_file(tmp_path, payload))) == 0
    text = (root / rel).read_text(encoding="utf-8")
    assert text.startswith(original)
    assert wb._outside_blocks(text) == original + "\n\n"
    assert "kb-dim/business-endpoint" in text


def test_non_whitelisted_knowledge_file_still_rejected(tmp_path: Path) -> None:
    """白名单外（含 business-rules / architecture/README / codebase-profile）一律拒绝。"""
    root = _mk_workspace(tmp_path)
    for rel in (
        f".dsh/knowledge/products/{PRODUCT}/business/business-rules.md",
        f".dsh/knowledge/products/{PRODUCT}/architecture/README.md",
        f".dsh/knowledge/products/{PRODUCT}/codebase-profile/01-modules.md",
    ):
        payload = {"blocks": [{"file": rel, "id": "kb-dim/x", "content": "事实"}]}
        code = _run(_args(root, tmp_path / "report", _input_file(tmp_path, payload)))
        assert code == 2, rel
        assert not (root / rel).exists()
