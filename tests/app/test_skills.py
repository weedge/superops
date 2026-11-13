"""面板“Skill”页：列表（来源、被哪些规则用）、看详情、改 SKILL.md（校验、留历史、防覆盖）、恢复历史版本；外部目录只读。"""
from __future__ import annotations

import pytest

from superops.app import http, skills
from superops.core.errors import UserError

DOC = "---\nname: ops-demo\ndescription: 示例 Skill\n---\n\n# ops-demo\n\n照这几步做：先读数据，再写报告。\n"


@pytest.fixture
def ws(world, tmp_path, monkeypatch):
    repo = world.home / "skills" / "ops-demo"
    repo.mkdir(parents=True)
    (repo / "SKILL.md").write_text(DOC, encoding="utf-8")
    (repo / "schema.json").write_text('{"required": ["x"]}', encoding="utf-8")
    ext = tmp_path / "ext"
    (ext / "popto-ext").mkdir(parents=True)
    (ext / "popto-ext" / "SKILL.md").write_text("---\nname: popto-ext\ndescription: 外部的\n---\n正文正文正文正文正文正文正文正文正文正文\n", encoding="utf-8")
    monkeypatch.setitem(world.cfg, "external_skill_dirs", [str(ext)])
    cur = world.settings.current()
    rules = cur["policy"]["rules"] + [{**cur["policy"]["rules"][0], "id": "demo", "name": "示例规则", "run": {"skill": "ops-demo", "agent": "claude"}},
                                      {**cur["policy"]["rules"][0], "id": "gone", "name": "缺 Skill 的规则", "run": {"skill": "ops-missing"}}]
    monkeypatch.setattr(world.settings, "current", lambda: {**cur, "policy": {**cur["policy"], "rules": rules}})
    return world


def test_列表_来源_规则_缺的Skill(ws):
    c = http.handle_get(ws, "/api/skills", {})
    by = {s["name"]: s for s in c["skills"]}
    assert by["ops-demo"]["editable"] and by["ops-demo"]["description"] == "示例 Skill" and by["ops-demo"]["has_schema"]
    assert [r["name"] for r in by["ops-demo"]["rules"]] == ["示例规则"]
    assert not by["popto-ext"]["editable"] and by["popto-ext"]["origin"] == "外部"
    assert c["missing"] == [{"name": "ops-missing", "rules": [r for r in skills._rules(ws)["ops-missing"]]}]


def test_改SKILL_md_留历史_能恢复(ws):
    d = http.handle_get(ws, "/api/skills", {"name": "ops-demo"})
    assert d["editable"] and '"required"' in d["schema"] and d["versions"] == []
    new = DOC.replace("再写报告", "再核对一遍，最后写报告")
    r = http.handle_post(ws, "/api/skills", {"action": "save", "name": "ops-demo", "markdown": new, "base_updated_at": d["updated_at"]})
    assert r["changed"] and "核对一遍" in (ws.home / "skills/ops-demo/SKILL.md").read_text(encoding="utf-8")
    assert len(r["versions"]) == 1
    old = http.handle_get(ws, "/api/skills", {"name": "ops-demo", "version": r["versions"][0]["id"]})
    assert old["markdown"] == DOC
    r2 = http.handle_post(ws, "/api/skills", {"action": "restore", "name": "ops-demo", "version": old["id"]})
    assert r2["markdown"] == DOC and len(r2["versions"]) == 2     # 恢复前的也留了一份
    assert http.handle_post(ws, "/api/skills", {"action": "save", "name": "ops-demo", "markdown": DOC})["changed"] is False


def test_校验_防覆盖_外部只读_路径不能乱填(ws):
    with pytest.raises(UserError, match="name: ops-demo"):
        skills.save(ws, "ops-demo", DOC.replace("name: ops-demo", "name: other"))
    with pytest.raises(UserError, match="description"):
        skills.save(ws, "ops-demo", "---\nname: ops-demo\n---\n正文正文正文正文正文正文正文正文正文正文正文\n")
    with pytest.raises(UserError, match="别处改的"):
        skills.save(ws, "ops-demo", DOC + "多一行\n", base_updated_at="2000-01-01T00:00:00+08:00")
    with pytest.raises(UserError, match="外部目录"):
        skills.save(ws, "popto-ext", "---\nname: popto-ext\ndescription: x\n---\n正文正文正文正文正文正文正文正文正文正文正文\n")
    for bad in ("../policy", "ops-demo/../../x", ""):
        with pytest.raises(UserError):
            skills.detail(ws, bad)
    with pytest.raises(UserError):
        skills.version(ws, "ops-demo", "../../policy")
