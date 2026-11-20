"""确定性 JSON 文本用例导入；转换不执行动作，也不自动绑定元素或断言。"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .textcase import parse_text_case

METADATA_PREFIX = "<!-- DSH-CASE-IMPORT "
METADATA_SUFFIX = " -->"
_PRODUCER = "uita.tools.dsh.case_import"
_STEP_RE = re.compile(r"step([1-9][0-9]?)\Z")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*(?:<([^>\r\n]+)>|([^\s)]+))(?:\s+[\"'][^\r\n]*?[\"'])?\s*\)")
_INVALID_FILE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED_RE = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?\Z", re.I)
_SAFETY_PRIORITY = {"L1": 1, "L2": 2, "L5": 3, "L3": 4, "L4": 5}


class CaseImportError(ValueError):
    """输入、解析保真或输出安全校验失败。"""


@dataclass(frozen=True)
class ConvertedCase:
    """一份待写入文本用例及可供执行门禁/台账读取的元数据。"""

    filename: str
    text: str
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"filename": self.filename, **self.metadata}


def _flatten(value: str) -> str:
    return " ".join(str(value or "").split())


def _strip_images(value: str) -> tuple[str, list[str]]:
    images: list[str] = []

    def remove(match: re.Match[str]) -> str:
        images.append(match.group(1) or match.group(2))
        return " "

    return _flatten(_IMAGE_RE.sub(remove, value)), images


def _comment_json(value: Any) -> str:
    return (json.dumps(value, ensure_ascii=True, separators=(",", ":"))
            .replace("<", r"\u003c").replace(">", r"\u003e")
            .replace("&", r"\u0026").replace("--", r"\u002d\u002d"))


def safe_filename(case_no: int, name: str) -> str:
    """生成跨平台安全文件名；序号取源文件位置，不随 --only 重编。"""
    slug = _INVALID_FILE_RE.sub("", unicodedata.normalize("NFC", name))
    slug = re.sub(r"\s+", "-", slug).strip(". -")[:60].rstrip(". -") or "case"
    if _RESERVED_RE.fullmatch(slug):
        slug = "_" + slug[:59]
    return f"{case_no:02d}-{slug}.md"


def safe_case_filename(index: int, name: str, max_len: int = 60) -> str:
    """兼容别名：生成安全 .md 文件名。"""
    return safe_filename(index, name)


def _validate_text(value: Any, label: str, *, allow_empty: bool = False) -> None:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise CaseImportError(f"{label} 必须为{'可为空的' if allow_empty else '非空'}字符串")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
        raise CaseImportError(f"{label} 含非法控制字符")


def _validate_cases(data: Any) -> None:
    if not isinstance(data, dict) or not data:
        raise CaseImportError("JSON 顶层必须是非空用例对象")
    for name, steps in data.items():
        _validate_text(name, "用例名")
        if not isinstance(steps, dict) or not steps:
            raise CaseImportError(f"{name}: 至少需要一个步骤对象")
        for key, step in steps.items():
            if not isinstance(key, str) or not _STEP_RE.fullmatch(key):
                raise CaseImportError(f"{name}: 步骤键必须为 step1 至 step99: {key!r}")
            if not isinstance(step, dict) or set(step) != {"step_content", "expect_content"}:
                raise CaseImportError(f"{name}/{key}: 步骤字段必须为 step_content、expect_content")
            _validate_text(step["step_content"], f"{name}/{key}/step_content")
            _validate_text(step["expect_content"], f"{name}/{key}/expect_content", allow_empty=True)


def classify_step(name: str, action: str, expected: str) -> tuple[str, list[str], bool]:
    """保守、逐步分级；不明操作不得归入已确认可回滚的 L2。"""
    text = action + " " + expected
    if re.search(r"卸载应用|卸载客户端|销毁环境.*重置|批量删除.*不可逆|数据归档.*迁移|数据迁移.*还原", name, re.I):
        return "L4", ["用例涉及卸载被测应用、销毁环境或批量删除/迁移数据"], True
    if re.search(r"卸载.{0,8}(?:应用|客户端)|(?:应用|客户端).{0,8}卸载|权限变更|彻底删除|选择.*(?:批量删除|迁移|移动)|勾选.*批量删除|新增删除文件", text):
        return "L4", ["可能删除/移动文件、卸载被测应用或变更系统权限"], True
    if re.search(r"发起远程|接受远程", name + " " + text):
        return "L3", ["远程连接或开放远程接入"], True
    if re.match(r"安装(?:完成|后)检查", action) and not re.search(r"点击|下载|更新|卸载|上传|迁移", action):
        return "L1", ["仅检查已安装应用或文件版本"], False
    if re.search(r"安装|下载|更新|升级|上传|订阅|登录|抽奖|成长值|完成.*任务|云服务|云端|进行对话|专家回复", text):
        return "L3", ["安装/更新/网络/账号状态等外部副作用"], True
    if re.search(r"扫描|优化|同步|立即修复|立即处理|修复风险|重置|删除|迁移|还原|网络配置", text):
        return "L3", ["可能修改配置、同步数据或重置环境，需人工确认动作边界"], True
    if re.search(r"任意|任一|所有.*工具|各个.*(?:菜单|工具)|子工具|点击工具|操作.*开关|依次点击|开启.{0,6}开关|批量关闭开关|卸载确认", action):
        return "L3", ["动作目标或回滚范围未明确，需人工复核"], True
    if re.search(r"截图|录屏|截全屏|游戏和视频显示|游戏页面", text + " " + name):
        return "L5", ["视觉/录屏结果未绑定可判定断言，需人工复核"], True
    if re.match(r"(?:检查|查看|观察|确认|断言|读取|等待)", action):
        return "L1", ["仅观察或读取"], False
    if re.search(r"点击|打开|进入|切换|hover|选中文本|失焦|右键|输入.*搜索|输入工具名称", action, re.I):
        return "L2", ["界面导航或明确的可回滚交互；元素仍待人工绑定"], False
    return "L3", ["不明动作，未确认只读或可回滚，需人工复核"], True


def convert_cases(
    data: dict[str, Any], *, only: Iterable[str] | None = None,
    keep_side_effects: bool = False,
) -> list[ConvertedCase]:
    """完整校验后纯转换；每步保留预期，所有预期均标 TODO-ASSERT。"""
    _validate_cases(data)
    if only is not None and isinstance(only, (str, bytes)):
        raise CaseImportError("only 必须是用例名列表，不能是单个字符串")
    selected = None if only is None else list(only)
    if selected is not None:
        if any(not isinstance(name, str) for name in selected):
            raise CaseImportError("only 中用例名必须为字符串")
        unknown = [name for name in selected if name not in data]
        if unknown:
            raise CaseImportError(f"only 指定不存在的用例: {unknown!r}")
    output: list[ConvertedCase] = []
    for case_no, (name, source_steps) in enumerate(data.items(), 1):
        if selected is not None and name not in selected:
            continue
        steps = []
        for key in sorted(source_steps, key=lambda value: int(value[4:])):
            source = source_steps[key]
            action, action_images = _strip_images(source["step_content"])
            expected, expected_images = _strip_images(source["expect_content"])
            level, reasons, manual = classify_step(name, action, expected)
            if not action:
                action = "TODO-VISUAL：原操作仅含图片，待人工补充"
                level, reasons, manual = "L3", ["图片操作不能自动转换为元素动作"], True
            steps.append({
                "source_step_key": key, "source_step_no": int(key[4:]),
                "action": action, "expected": expected, "safety_level": level,
                "reasons": reasons, "todo_assert": True,
                "todo_visual": action_images + expected_images,
                "requires_manual_review": manual,
                "skip": level in {"L3", "L4"} and not keep_side_effects,
            })
        level = max((step["safety_level"] for step in steps), key=_SAFETY_PRIORITY.__getitem__)
        metadata = {
            "schema_version": 1, "producer": _PRODUCER,
            "source_case_no": case_no, "name": name,
            "safety_level": level, "skip": any(step["skip"] for step in steps),
            "keep_side_effects": bool(keep_side_effects), "steps": steps,
        }
        lines = [METADATA_PREFIX + _comment_json(metadata) + METADATA_SUFFIX]
        for step in steps:
            key = step["source_step_key"]
            lines.append(f"<!-- TODO-ASSERT {key}: 原预期待人工绑定元素与可判定断言；空预期不得判通过 -->")
            if step["todo_visual"]:
                lines.append(f"<!-- TODO-VISUAL {key}: 原操作/预期含截图 {_comment_json(step['todo_visual'])}，需人工复核 -->")
            if step["requires_manual_review"]:
                lines.append(f"<!-- TODO-REVIEW {key}: {step['safety_level']}；需人工确认动作与回滚边界 -->")
            if step["skip"]:
                lines.append(f"<!-- SKIP-{step['safety_level']} {key}: 默认不执行有副作用步骤 -->")
        lines.extend([f"用例名称：{_flatten(name)}", "前置条件：被测应用已安装；DSH 以 --app sample_web 启动", "测试步骤："])
        for step in steps:
            lines.append(f"{step['source_step_no']}. {step['action']}")
            if step["expected"]:
                lines.append(f"   预期：{step['expected']}")
        text = "\n".join(lines) + "\n"
        parsed = parse_text_case(text)
        expected_pairs = [(step["action"], step["expected"]) for step in steps]
        actual_pairs = [(step.action, step.expected) for step in parsed.steps]
        if not parsed.parsed or parsed.name != _flatten(name) or actual_pairs != expected_pairs:
            raise CaseImportError(f"{name}: parse_text_case 自检失败，步骤/动作/预期无法保真")
        output.append(ConvertedCase(safe_filename(case_no, name), text, metadata))
    return output


def convert_json_cases_text(raw_json: str, *, only: str = "",
                            app: str = "sample_web",
                            keep_side_effects: bool = False) -> list[tuple[str, str]]:
    """便捷接口：从 JSON 文本转换为 ``[(用例名, markdown), ...]``。"""
    try:
        data = json.loads(raw_json, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except json.JSONDecodeError as exc:
        raise CaseImportError(f"非法 JSON 用例文件: {exc}") from exc
    target = str(only or "").strip()
    if target and isinstance(data, dict) and target not in data:
        raise KeyError(f"未找到指定用例: {target!r}")
    selected = [target] if target else None
    converted = convert_cases(data, only=selected, keep_side_effects=keep_side_effects)
    return [(item.metadata["name"], item.text) for item in converted]


def convert_json_cases(src_path: str, out_dir: str, *, only: str = "",
                       app: str = "sample_web",
                       keep_side_effects: bool = False) -> list[str]:
    """便捷接口：读取 ``src_path`` 并写入 ``out_dir``，返回落地路径字符串列表。"""
    data = load_cases(src_path)
    target = str(only or "").strip()
    if target and target not in data:
        raise KeyError(f"未找到指定用例: {target!r}")
    cases = convert_cases(data, only=[target] if target else None,
                          keep_side_effects=keep_side_effects)
    paths = write_cases(cases, out_dir)
    return [str(p) for p in paths]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CaseImportError(f"JSON 含重复键: {key!r}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise CaseImportError(f"JSON 含非标准数值: {value}")


def load_cases(src: str | os.PathLike[str]) -> dict[str, Any]:
    """读取 UTF-8 JSON；重复键、不合法 JSON 或 schema 全部拒绝。"""
    try:
        data = json.loads(Path(src).read_text(encoding="utf-8-sig"),
                          object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CaseImportError(f"无法读取用例 JSON: {exc}") from exc
    _validate_cases(data)
    return data


def _validate_component(name: str) -> None:
    if (not name or name in {".", ".."} or _INVALID_FILE_RE.search(name)
            or name.endswith((" ", ".")) or _RESERVED_RE.fullmatch(name)):
        raise CaseImportError(f"不安全的 Windows 路径段: {name!r}")


def _reject_links(path: Path) -> None:
    for parent in (path, *path.parents):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            continue
        attributes = getattr(info, "st_file_attributes", 0)
        if stat.S_ISLNK(info.st_mode) or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise CaseImportError(f"拒绝符号链接或 reparse 路径: {parent}")


def write_cases(cases: Iterable[ConvertedCase], out: str | os.PathLike[str]) -> list[Path]:
    """整批预检后创建文件；相同产物幂等，不覆盖任何不同的既有文件。"""
    directory = Path(out)
    for component in directory.parts:
        if component != directory.anchor:
            _validate_component(component)
    _reject_links(directory)
    directory = directory.absolute()
    if directory.exists() and not directory.is_dir():
        raise CaseImportError(f"输出路径不是目录: {directory}")
    existing = {unicodedata.normalize("NFC", item.name).casefold(): item
                for item in directory.iterdir()} if directory.exists() else {}
    entries: list[tuple[Path, bytes, bool]] = []
    names: set[str] = set()
    for case in cases:
        _validate_component(case.filename)
        if not case.filename.endswith(".md"):
            raise CaseImportError("转换产物必须是单个 .md 文件名")
        canonical = unicodedata.normalize("NFC", case.filename).casefold()
        if canonical in names:
            raise CaseImportError(f"输出文件名碰撞: {case.filename}")
        names.add(canonical)
        path = directory / case.filename
        _reject_links(path)
        payload = case.text.encode("utf-8")
        other = existing.get(canonical)
        if other is not None:
            if other.name != case.filename or not other.is_file() or other.read_bytes() != payload:
                raise CaseImportError(f"拒绝覆盖既有文件或大小写碰撞: {other}")
        entries.append((path, payload, other is None))
    if not entries:
        return []
    created: list[Path] = []
    try:
        directory.mkdir(parents=True, exist_ok=True)
        _reject_links(directory)
        for path, payload, create in entries:
            if create:
                with path.open("xb") as handle:
                    created.append(path)
                    handle.write(payload)
    except OSError as exc:
        for path in created:
            path.unlink(missing_ok=True)
        raise CaseImportError(f"创建转换产物失败: {exc}") from exc
    return [path for path, _, _ in entries]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", required=True, help="源 JSON 文件")
    parser.add_argument("--out", required=True, help="文本用例输出目录")
    parser.add_argument("--only", action="append", help="只转换指定用例，可重复")
    parser.add_argument("--keep-side-effects", action="store_true", help="保留危险步骤但不解除 TODO 人工复核门禁")
    args = parser.parse_args(argv)
    try:
        cases = convert_cases(load_cases(args.src), only=args.only,
                              keep_side_effects=args.keep_side_effects)
        paths = write_cases(cases, args.out)
    except (CaseImportError, OSError) as exc:
        print(f"case_import: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"cases": len(paths), "steps": sum(len(case.metadata["steps"]) for case in cases),
                      "skipped_cases": sum(case.metadata["skip"] for case in cases)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
