"""提示词文件和 Skill 文案的静态检查。

- prompts/ 下 12 个文件齐全、非空，格式符合 prompts.read 的拼法（一行一句、空行分段）；
- 每个文件确实被拼进了它该去的角色；
- skills/ops-*/SKILL.md 不再用旧架构的说法，附带文件的内联标记能展开；
- policy.yaml 引用的 ops-* Skill 都在，Skill 里写的事件类型都有规则接。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from superops.app import prompts
from superops.biz.context import load_skill_text

HOME = Path(__file__).resolve().parent.parent

ROUTER_FILES = ["shared.md", "orchestrator.md", "dispatch.md", "admission.md"]
WORKER_FILES = ["shared.md", "worker.md", "subscriptions.md", "replies.md", "work.md", "handoff-worker.md"]
TEAM_FILES = ["team/communication.md", "team/scope.md", "team/delivery.md"]
PROMPT_FILES = sorted({*ROUTER_FILES, *WORKER_FILES, *TEAM_FILES})

OLD_TERMS = ["input.md", "follow_up_events", "decision_card", "needs_decision", "message_markdown", "opsd", "事件中台"]
SKILL_FILES = sorted((HOME / "skills").glob("ops-*/SKILL.md"))

SENTENCE_END = "。！？；：….!?"
CLOSERS = "”’」』）)】》`*\"'"
EVENT_TYPE = r"[a-z_]+(?:\.[a-z_]+)+"


def _markdown_line(line: str) -> bool:
    """列表、标题、表格、代码围栏这些行不要求以句末标点结尾。"""
    s = line.strip()
    return s.startswith(("#", "|", "- ", "* ", "```")) or bool(re.match(r"\d+[.、)]\s", s))


def _complete_sentence(line: str) -> bool:
    s = line.rstrip().rstrip(CLOSERS)
    return bool(s) and s[-1] in SENTENCE_END


def _policy_rules() -> list[dict]:
    return yaml.safe_load((HOME / "policy.yaml").read_text(encoding="utf-8"))["policy"]["rules"]


def _first_sentence(name: str) -> str:
    return next(l.strip() for l in (HOME / "prompts" / name).read_text(encoding="utf-8").splitlines() if l.strip())


def test_there_are_twelve_prompt_files():
    assert len(PROMPT_FILES) == 12


@pytest.mark.parametrize("name", PROMPT_FILES)
def test_prompt_file_exists_and_not_empty(name):
    path = HOME / "prompts" / name
    assert path.is_file(), f"缺少 prompts/{name}"
    assert path.read_text(encoding="utf-8").strip(), f"prompts/{name} 是空的"


@pytest.mark.parametrize("name", PROMPT_FILES)
def test_prompt_lines_are_complete_sentences(name):
    """连续的行会被直接拼成一段，所以每行必须自成一句。"""
    lines = (HOME / "prompts" / name).read_text(encoding="utf-8").splitlines()
    bad = [f"{i}: {l}" for i, l in enumerate(lines, 1) if l.strip() and not _markdown_line(l) and not _complete_sentence(l)]
    assert not bad, f"prompts/{name} 这些行没有以句末标点结尾：\n" + "\n".join(bad)


@pytest.mark.parametrize("name", PROMPT_FILES)
def test_prompts_read_joins_lines_into_paragraphs(name):
    raw = (HOME / "prompts" / name).read_text(encoding="utf-8")
    text = prompts.read(HOME, name)
    assert text.strip()
    blocks = [b for b in re.split(r"\n\s*\n", raw.strip()) if b.strip()]
    paragraphs = text.split("\n\n")
    assert len(paragraphs) == len(blocks)
    for block, paragraph in zip(blocks, paragraphs):
        lines = [l.rstrip() for l in block.splitlines() if l.strip()]
        if not any(l.lstrip().startswith(("-", "#", "|", "`")) for l in lines):
            assert paragraph == "".join(lines)  # 普通段落：各行直接相接，中间没有换行


def test_prompts_read_missing_file_is_empty():
    assert prompts.read(HOME, "no-such-file.md") == ""


def test_router_prompt_contains_its_files():
    settings = {"eval": {"mode": "observe"}, "policy": {"rules": []}}
    text = prompts.router_turn(HOME, settings=settings, cards=["### 候选"], open_tickets=[], errors=None)
    for name in [*ROUTER_FILES, *TEAM_FILES]:
        assert _first_sentence(name) in text, f"prompts/{name} 没有进主 Session 的提示词"
    for heading in ("## 你的角色", "## 沟通", "## 工单范围", "## 交付要求", "## 路由", "## 主动候选的研判", "## 输出"):
        assert heading in text


def test_worker_prompt_contains_its_files():
    ticket = {"id": "ticket-1", "no": 1, "title": "测试工单", "mandate": {"scope": "按原话做", "strength": "explicit"},
              "execution": {"directory": "/tmp/work/t1"}, "assignments": []}
    text = prompts.worker_turn(HOME, ticket=ticket, rule=None, skill_text="", decisions="", context_files="", inputs=[],
                               triggers=lambda _id: None, first=True, compact=False, notes=[], errors=None,
                               skill_schema=None, origin=None)
    for name in [*WORKER_FILES, *TEAM_FILES]:
        assert _first_sentence(name) in text, f"prompts/{name} 没有进执行器的提示词"
    for heading in ("## 你的角色", "## 沟通", "## 工单范围", "## 交付要求", "## 订阅与回复", "## 怎么工作", "## 需要人决定时", "## 输出"):
        assert heading in text


def test_skill_files_found():
    assert len(SKILL_FILES) >= 15


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.parent.name)
def test_skill_has_no_old_architecture_terms(path):
    text = path.read_text(encoding="utf-8")
    found = [term for term in OLD_TERMS if term in text]
    assert not found, f"{path.parent.name}/SKILL.md 还有旧说法：{found}"


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.parent.name)
def test_skill_inline_markers_resolve(path):
    text = load_skill_text(path.parent)
    assert "读不到" not in text
    assert ":inline" not in text  # 标记都被换成了文件内容


def test_policy_ops_skills_exist():
    missing = []
    for rule in _policy_rules():
        run = rule.get("run") or {}
        skill = run.get("skill")
        if run.get("executor", "agent") != "agent" or not skill or not str(skill).startswith("ops-"):
            continue
        if not (HOME / "skills" / skill / "SKILL.md").is_file():
            missing.append(f"{rule['id']} → {skill}")
    assert not missing, f"policy.yaml 引用的 Skill 目录不存在：{missing}"


def test_events_named_in_skills_are_handled_by_a_rule():
    """Skill 让执行器发的事件（events 里的，或卡片选项 emit 里的）必须有规则的 when.types 接；
    否则它既不会开出下一张工单，也不会留底。"""
    handled = {t for rule in _policy_rules() for t in ((rule.get("when") or {}).get("types") or [])}
    orphans = []
    for path in SKILL_FILES:
        text = path.read_text(encoding="utf-8")
        named = set(re.findall(rf"\{{type: ({EVENT_TYPE})", text)) | set(re.findall(rf"emit ({EVENT_TYPE})", text))
        orphans += [f"{path.parent.name}: {t}" for t in sorted(named - handled)]
    assert not orphans, f"这些事件没有任何规则接：{orphans}"
