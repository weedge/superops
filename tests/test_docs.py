"""文档的静态检查（对应原型的 tests/docs.spec.ts）。

- 编号文档从 000 连续编号，只链接到编号更小的文档；
- README / AGENTS / CLAUDE 和 docs/ 下每篇文档里的本地链接都能解析；
- 没有合并冲突标记；
- docs/007-architecture.md 的模块表列了 superops/ 下每个 .py（__init__.py 可以不列），测试表列了 tests/ 下每个 test_*.py，
  两张表里也没有不存在的文件；
- 文档里写到的每个 `superops <子命令>` 在 superops/cli.py 里真实存在。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
NUMBERED = re.compile(r"^(\d{3})-[a-z0-9-]+\.md$")
LINK = re.compile(r"\]\(([^)\s]+)\)")
SUBCOMMAND = re.compile(r"(?<![\w/.-])superops\s+([a-z][a-z_-]*)")

numbered = sorted(p.name for p in DOCS.glob("*.md") if NUMBERED.match(p.name))
entries = [ROOT / "README.md", ROOT / "AGENTS.md", ROOT / "CLAUDE.md"]
documents = [*entries, *sorted(DOCS.glob("*.md"))]


def links(path: Path) -> list[str]:
    """文档里的本地链接目标（去掉锚点；外部链接和纯锚点不算）。"""
    targets = [m.group(1).split("#")[0] for m in LINK.finditer(path.read_text(encoding="utf-8"))]
    return [t for t in targets if t and not re.match(r"^[a-z]+:", t)]


def listed(heading: str) -> list[str]:
    """007 里 `## <heading>` 一节的表格第一列列出的仓库相对路径。"""
    lines = (DOCS / "007-architecture.md").read_text(encoding="utf-8").splitlines()
    start = lines.index(f"## {heading}")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    rows = [line for line in lines[start:end] if line.startswith("| ")][2:]   # 前两行是表头和分隔线
    return [name for row in rows for name in re.findall(r"`([^`]+)`", row.split("|")[1])]


def files(directory: str, pattern: str) -> list[str]:
    return sorted(str(p.relative_to(ROOT)) for p in (ROOT / directory).rglob(pattern) if "__pycache__" not in p.parts)


def test_numbered_documents_are_consecutive_from_000():
    assert numbered, "docs/ 下没有编号文档"
    assert [name[:3] for name in numbered] == [f"{i:03d}" for i in range(len(numbered))]


def test_numbered_documents_link_only_to_earlier_numbered_documents():
    violations = []
    for name in numbered:
        for target in links(DOCS / name):
            linked = (DOCS / target).resolve()
            if linked.suffix != ".md":
                continue
            match = NUMBERED.match(linked.name)
            if linked.parent != DOCS.resolve() or not match or match.group(1) >= name[:3]:
                violations.append(f"{name} → {target}")
    assert violations == []


def test_every_local_link_resolves():
    missing = [f"{path.relative_to(ROOT)} → {target}" for path in documents for target in links(path)
               if not (path.parent / target).exists()]
    assert missing == []


def test_readme_links_every_numbered_document():
    linked = {(ROOT / target).resolve().name for target in links(ROOT / "README.md")}
    assert [name for name in numbered if name not in linked] == []


def test_no_merge_conflict_markers():
    assert [str(p.relative_to(ROOT)) for p in documents if re.search(r"^(<{7}|>{7}) ", p.read_text(encoding="utf-8"), re.M)] == []


def test_module_table_lists_every_module_and_only_existing_files():
    modules = listed("模块")
    required = [p for p in files("superops", "*.py") if not p.endswith("__init__.py")]
    assert [p for p in required if p not in modules] == [], "docs/007-architecture.md 的模块表缺这些文件"
    assert [p for p in modules if not (ROOT / p).is_file()] == [], "模块表里有不存在的文件"


def test_test_table_lists_every_test_and_only_existing_files():
    tests = listed("测试")
    assert [p for p in files("tests", "test_*.py") if p not in tests] == [], "docs/007-architecture.md 的测试表缺这些文件"
    assert [p for p in tests if not (ROOT / p).is_file()] == [], "测试表里有不存在的文件"


def mentioned_subcommands() -> list[tuple[str, str]]:
    found = {(str(path.relative_to(ROOT)), m.group(1)) for path in documents
             for m in SUBCOMMAND.finditer(path.read_text(encoding="utf-8"))}
    return sorted(found)


def test_readme_mentions_subcommands():
    assert any(doc == "README.md" for doc, _ in mentioned_subcommands())


@pytest.mark.parametrize("doc,name", mentioned_subcommands())
def test_mentioned_subcommand_exists(doc, name, capsys):
    """argparse 对不认识的子命令以 2 退出；认识的子命令加 --help 以 0 退出，不执行任何动作。"""
    from superops.cli import main
    with pytest.raises(SystemExit) as exit_:
        main([name, "--help"])
    capsys.readouterr()
    assert exit_.value.code == 0, f"{doc} 提到了 `superops {name}`，但 superops/cli.py 里没有这个子命令"
