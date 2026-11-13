"""面板“设置 → 聊天渠道”：钉钉 / 飞书二选一的切换、改群、增删审批人；存成运行时覆盖，叠在配置文件上。"""
from __future__ import annotations

import pytest

from superops.app import chat, http
from superops.app.settings import apply_chat, normalize
from superops.core.errors import UserError


@pytest.fixture
def ui(world, monkeypatch):
    monkeypatch.setattr(chat, "_lark_auth", lambda: {"ok": True, "user": "张三", "detail": "已登录"})
    restarts = []
    monkeypatch.setattr(world, "request_restart", lambda reason: restarts.append(reason) or True)
    world.restarts = restarts
    return world


def test_叠加规则_选了哪个就只开哪个_home跟过去():
    s = normalize({"sources": {"dingtalk": {"enabled": True, "chats": ["cidA"]}, "lark": {"enabled": False}},
                   "home": {"source": "dingtalk", "thread": "home", "address": {"chat": "cidA"}}})
    apply_chat(s, {"app": "lark", "chats": {"lark": ["oc_T"]}, "approvers": {"lark": [{"id": "ou_me", "name": "张三"}]}})
    assert s["sources"]["lark"]["enabled"] and not s["sources"]["dingtalk"]["enabled"]
    assert s["home"] == {"source": "lark", "thread": "home", "address": {"chat": "oc_T"}}
    assert s["policy"]["sources"]["lark"]["allowed"] == [{"id": "ou_me", "name": "张三"}]


def test_面板上配群_加审批人_切到飞书_宿主重启(ui):
    v = http.handle_get(ui, "/api/chat", {})
    assert v["active"] is None and set(v["apps"]) == {"dingtalk", "lark"}
    with pytest.raises(UserError):                                   # 没配群不能切
        http.handle_post(ui, "/api/chat", {"action": "switch", "app": "lark"})
    http.handle_post(ui, "/api/chat", {"action": "set_group", "app": "lark", "group": "oc_1234abcd"})
    with pytest.raises(UserError):                                   # 飞书审批人只认 open_id
        http.handle_post(ui, "/api/chat", {"action": "add_approver", "app": "lark", "id": "张三"})
    http.handle_post(ui, "/api/chat", {"action": "add_approver", "app": "lark", "id": "ou_me", "name": "张三"})
    r = http.handle_post(ui, "/api/chat", {"action": "switch", "app": "lark"})
    assert r["active"] == "lark" and r["restart"] and ui.restarts
    s = ui.settings.current()
    assert s["sources"]["lark"]["enabled"] and not s["sources"].get("dingtalk", {}).get("enabled")
    assert s["home"]["source"] == "cli"            # home 原来是终端（不是聊天软件）就不动；是钉钉的话会跟过去（见上一个测试）
    assert ui.settings.policy()  # 叠加后的设置能组成策略
    r = http.handle_post(ui, "/api/chat", {"action": "remove_approver", "app": "lark", "id": "ou_me"})
    assert r["apps"]["lark"]["approvers"] == [] and len(ui.restarts) == 1   # 改审批人不用重启
    assert any(h["origin"] == "panel" for h in ui.settings.history())


def test_飞书没登录不能切_改正在用的钉钉群要重启(ui, monkeypatch):
    http.handle_post(ui, "/api/chat", {"action": "set_group", "app": "lark", "group": "oc_x"})
    monkeypatch.setattr(chat, "_lark_auth", lambda: {"ok": False, "detail": "过期"})
    with pytest.raises(UserError, match="lark-cli auth login"):
        http.handle_post(ui, "/api/chat", {"action": "switch", "app": "lark"})
    http.handle_post(ui, "/api/chat", {"action": "set_group", "app": "dingtalk", "group": "cidOPS0000000=="})
    assert not ui.restarts                                           # 钉钉没在用：不用重启
    http.handle_post(ui, "/api/chat", {"action": "switch", "app": "dingtalk"})
    r = http.handle_post(ui, "/api/chat", {"action": "set_group", "app": "dingtalk", "group": "cidOPS1111111=="})
    assert r["restart"] and len(ui.restarts) == 2


def test_按群名设置_找不到唯一的就要ID(ui, monkeypatch):
    from superops.biz.notifier import lark
    monkeypatch.setattr(lark, "run", lambda args, **k: {"data": {"chats": [{"chat_id": "oc_A", "name": "测试superops"},
                                                                          {"chat_id": "oc_B", "name": "测试superops2"}]}})
    r = http.handle_post(ui, "/api/chat", {"action": "set_group", "app": "lark", "group": "测试superops"})
    assert r["apps"]["lark"]["chats"] == [{"id": "oc_A", "name": "测试superops"}]
    monkeypatch.setattr(lark, "run", lambda args, **k: {"data": {"chats": []}})
    with pytest.raises(UserError, match="请直接填群 ID"):
        http.handle_post(ui, "/api/chat", {"action": "set_group", "app": "lark", "group": "没有这个群"})


def test_不是launchd起的不自己重启(world, monkeypatch):
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    assert not world.restartable() and world.request_restart("x") is False
