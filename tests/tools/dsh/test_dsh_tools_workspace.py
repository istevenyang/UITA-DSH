"""工作区与 Skill 工具单测：文件读写/编辑/检索、越界与受保护文件拦截、Skill 加载、UITA 受控调用。"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from uita.tools.dsh.registry import get_tool, load_builtin_tools


@pytest.fixture(autouse=True)
def _load():
    load_builtin_tools()


def _ctx(workdir: str):
    return SimpleNamespace(workdir=workdir, session=None)


async def test_file_write_read_and_edit(tmp_path):
    ctx = _ctx(str(tmp_path))
    write_tool = get_tool("file_write")
    read_tool = get_tool("file_read")
    edit_tool = get_tool("file_edit")

    res_w = await write_tool.invoke(ctx, {
        "path": "sub/demo.txt",
        "content": "line1\nline2\nline3\n",
    })
    assert res_w.ok is True
    assert (tmp_path / "sub" / "demo.txt").is_file()

    res_r = await read_tool.invoke(ctx, {"path": "sub/demo.txt", "offset": 2, "limit": 1})
    assert res_r.ok is True
    assert res_r.data["total_lines"] == 3
    assert res_r.data["content"] == "line2\n"

    res_e = await edit_tool.invoke(ctx, {
        "path": "sub/demo.txt",
        "old_string": "line2",
        "new_string": "line2_edited",
    })
    assert res_e.ok is True
    assert "line2_edited" in (tmp_path / "sub" / "demo.txt").read_text(encoding="utf-8")


async def test_file_edit_rejects_ambiguous_match(tmp_path):
    ctx = _ctx(str(tmp_path))
    target = tmp_path / "dup.txt"
    target.write_text("foo\nfoo\n", encoding="utf-8")
    edit_tool = get_tool("file_edit")

    res = await edit_tool.invoke(ctx, {
        "path": "dup.txt",
        "old_string": "foo",
        "new_string": "bar",
    })
    assert res.ok is False
    assert "2 次" in res.error

    res_all = await edit_tool.invoke(ctx, {
        "path": "dup.txt",
        "old_string": "foo",
        "new_string": "bar",
        "replace_all": True,
    })
    assert res_all.ok is True
    assert target.read_text(encoding="utf-8") == "bar\nbar\n"


async def test_protected_files_and_path_traversal_blocked(tmp_path, monkeypatch):
    from uita import config

    monkeypatch.setattr(config, "WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "REPORT_DIR", str(tmp_path / "report"))
    monkeypatch.setattr(config, "USER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "ROOT_DIR", str(tmp_path))

    ctx = _ctx(str(tmp_path))
    write_tool = get_tool("file_write")
    read_tool = get_tool("file_read")

    for forbidden in (".env.local", ".env", ".mcp.json", "dsh_settings.json", "sub/.env.secret"):
        res = await write_tool.invoke(ctx, {"path": forbidden, "content": "hacked"})
        assert res.ok is False
        assert "受保护" in res.error

    res_out = await read_tool.invoke(ctx, {"path": "../../etc/passwd"})
    assert res_out.ok is False
    assert "超出" in res_out.error or "不存在" in res_out.error


async def test_glob_and_grep_search(tmp_path):
    ctx = _ctx(str(tmp_path))
    (tmp_path / "a.tcs").write_text("name: case_a\nclick[登录按钮]\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("nothing here\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("SECRET=123\n", encoding="utf-8")

    glob_res = await get_tool("glob_search").invoke(ctx, {"pattern": "*.tcs"})
    assert glob_res.ok is True
    assert glob_res.data["matches"] == ["a.tcs"]

    grep_res = await get_tool("grep_search").invoke(ctx, {"pattern": "登录按钮|SECRET"})
    assert grep_res.ok is True
    files = [m["file"] for m in grep_res.data["matches"]]
    assert "a.tcs" in files
    assert ".env.local" not in files


async def test_skill_load_reads_skill_and_agent(tmp_path):
    skill_dir = tmp_path / ".dsh" / "skills" / "demo-skill"
    ref_dir = skill_dir / "reference"
    ref_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Demo Skill\nSteps here", encoding="utf-8")
    (ref_dir / "01-guide.md").write_text("Reference body", encoding="utf-8")

    agent_dir = tmp_path / ".dsh" / "agents"
    agent_dir.mkdir(parents=True)
    (agent_dir / "demo-agent.md").write_text("# Demo Agent", encoding="utf-8")

    ctx = _ctx(str(tmp_path))
    skill_tool = get_tool("skill_load")

    res_s = await skill_tool.invoke(ctx, {
        "skill": "demo-skill",
        "reference": "01-guide.md",
    })
    assert res_s.ok is True
    assert "Demo Skill" in res_s.data["content"]
    assert res_s.data["reference_content"] == "Reference body"

    res_a = await skill_tool.invoke(ctx, {"agent": "demo-agent"})
    assert res_a.ok is True
    assert res_a.data["kind"] == "agent"
    assert "Demo Agent" in res_a.data["content"]

    res_bad = await skill_tool.invoke(ctx, {"skill": "../escape"})
    assert res_bad.ok is False


async def test_uita_invoke_whitelist_and_subprocess(tmp_path, monkeypatch):
    ctx = _ctx(str(tmp_path))
    tool = get_tool("uita_invoke")

    bad = await tool.invoke(ctx, {"mode": "dsh-run"})
    assert bad.ok is False

    bad_arg = await tool.invoke(ctx, {"mode": "parse", "args": ["--mode", "dsh-run"]})
    assert bad_arg.ok is False
    assert "禁止重复传入 --mode" in bad_arg.error

    seen_cmd = []

    def _fake_run(cmd, **kwargs):
        seen_cmd.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="AST OK", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    ok_res = await tool.invoke(ctx, {"mode": "parse", "args": ["--cases-dir", "a.tcs"]})
    assert ok_res.ok is True
    assert ok_res.data["stdout"] == "AST OK"
    assert seen_cmd[0][1]["shell"] is False

    cap_tool = get_tool("uita_capabilities")
    cap_res = await cap_tool.invoke(ctx, {"category": "execution"})
    assert cap_res.ok is True
    assert "runner" in cap_res.data["modes"]
    assert "batch" in cap_res.data["modes"]
