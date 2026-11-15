"""superops doctor 的聊天软件几行：两个命令行都查、只有开着的缺了才算问题；登录、群（飞书群名要能换成群 ID）、
home 指向开着的那个、审批人名单（飞书要 open_id、本人不在名单里提醒）；钉钉登录认 dws auth status 的字段。"""
from __future__ import annotations

import json
import subprocess

import pytest

from superops.app import chatcheck

LOGGED_IN = {"installed": True, "ok": True, "user": "本人", "open_id": "ou_me", "detail": "已登录"}
LOGGED_OUT = {"installed": True, "ok": False, "detail": "本人身份没登录或已过期"}


def settings(app="lark", chats=("测试superops",), home_chat="测试superops", allowed=(), home_source=None):
    s = {"sources": {"dingtalk": {"enabled": app == "dingtalk", "config": {}}, "lark": {"enabled": app == "lark", "config": {}}},
         "policy": {"sources": {"dingtalk": {"allowed": []}, "lark": {"allowed": []}}},
         "home": {"source": home_source or app or "cli", "thread": "home", "address": {"chat": home_chat} if home_chat else {}}}
    if app:
        s["sources"][app]["config"]["chats"] = list(chats)
        s["policy"]["sources"][app]["allowed"] = [dict(p) for p in allowed]
    return s


def rows(s, status=LOGGED_IN, find=lambda name: "oc_test", which=("lark-cli", "dws")):
    import shutil
    real = shutil.which
    shutil.which = lambda cmd, *a, **k: f"/bin/{cmd}" if cmd in which else None
    try:
        return {r["title"]: r for r in chatcheck.check(s, status=lambda app: status, find_chat=find)}
    finally:
        shutil.which = real


def test_都配好了_每项都通过_群名换成了群ID():
    r = rows(settings(allowed=[{"id": "ou_me", "name": "本人"}]))
    assert all(x["ok"] for x in r.values()), r
    assert r["聊天渠道"]["detail"] == "用飞书（钉钉关着）"
    assert r["飞书群"]["detail"] == "「测试superops」→ oc_test" and r["home 渠道"]["detail"] == "飞书 「测试superops」→ oc_test"
    assert r["钉钉命令行 dws"]["detail"] == "/bin/dws（没开钉钉，用不到）"
    assert r["飞书登录（本人身份）"]["detail"] == "已登录 本人 ou_me"


def test_没开的那个没装不算问题_开着的没装要报并给安装命令():
    r = rows(settings(allowed=[{"id": "ou_me"}]), which=())
    assert r["钉钉命令行 dws"]["ok"] is True and "未安装" in r["钉钉命令行 dws"]["detail"]
    assert r["飞书命令行 lark-cli"]["ok"] is False and "npm install -g @larksuite/cli" in r["飞书命令行 lark-cli"]["fix"]
    assert r["飞书登录（本人身份）"]["ok"] is False


def test_没登录_群名换不了_提示先登录():
    r = rows(settings(allowed=[{"id": "ou_me"}]), status=LOGGED_OUT)
    assert r["飞书登录（本人身份）"]["ok"] is False and "lark-cli auth login" in r["飞书登录（本人身份）"]["fix"]
    assert r["飞书群"]["ok"] is False and "要登录飞书后" in r["飞书群"]["detail"] and "lark-cli auth login" in r["飞书群"]["fix"]
    assert r["home 渠道"]["ok"] is False and "lark-cli auth login" in r["home 渠道"]["fix"]
    assert rows(settings(chats=("oc_1",), home_chat="oc_1", allowed=[{"id": "ou_me"}]), status=LOGGED_OUT)["飞书群"]["ok"] is True


def test_群名找不到或重名_报出原因():
    def find(name):
        raise RuntimeError(f"按群名「{name}」找到 2 个群，请直接填群 ID（oc_ 开头）")
    r = rows(settings(allowed=[{"id": "ou_me"}]), find=find)
    assert r["飞书群"]["ok"] is False and "找到 2 个群" in r["飞书群"]["detail"] and "chat-search" in r["飞书群"]["fix"]


def test_没配群_home指向别处():
    r = rows(settings(chats=(), home_chat=None, home_source="cli", allowed=[{"id": "ou_me"}]))
    assert r["飞书群"]["ok"] is False and "sources.lark.chats" in r["飞书群"]["detail"]
    assert r["home 渠道"]["ok"] is False and "指向 cli" in r["home 渠道"]["detail"]


def test_审批人_空的给出能直接抄的写法_不是open_id的不生效_本人不在名单里提醒():
    empty = rows(settings())["审批人"]
    assert empty["ok"] is False and empty["fix"] == 'policy.sources.lark.allowed: [{id: "ou_me", name: "本人"}]'
    bad = rows(settings(allowed=[{"id": "张三"}]))["审批人"]
    assert bad["ok"] is False and "张三 不是 open_id" in bad["detail"]
    other = rows(settings(allowed=[{"id": "ou_other", "name": "同事"}]))["审批人"]
    assert other["ok"] is True and "你本人不在名单里" in other["detail"]


def test_都没开就落本机收件箱_两个命令行都只是说一声():
    r = rows(settings(app=None, home_chat=None, home_source="cli"), which=())
    assert r["聊天渠道"]["ok"] is True and "本机收件箱" in r["聊天渠道"]["detail"]
    assert set(r) == {"聊天渠道", "钉钉命令行 dws", "飞书命令行 lark-cli"} and all(x["ok"] for x in r.values())


def test_钉钉开着_认dws的登录字段_钉钉群不用换ID():
    r = rows(settings(app="dingtalk", chats=("cid1==",), home_chat="cid1==", allowed=[{"id": "u1"}]))
    assert r["钉钉群"]["detail"] == "cid1==" and all(x["ok"] for x in r.values())


@pytest.mark.parametrize("out,rc,ok", [
    ({"data": {"authenticated": True, "token_valid": True, "user": {"name": "本人"}}}, 0, True),
    ({"authenticated": True, "token_valid": False}, 0, False),
    ({"authenticated": True}, 1, False),
    ({}, 0, False),
])
def test_dws登录状态(monkeypatch, out, rc, ok):
    monkeypatch.setattr(chatcheck.shutil, "which", lambda cmd: "/bin/dws")
    monkeypatch.setattr(chatcheck.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a[0], rc, stdout=json.dumps(out), stderr=""))
    st = chatcheck.dws_status()
    assert st["ok"] is ok and st["installed"] is True
    if ok:
        assert st["user"] == "本人"


def test_dws没装():
    import shutil
    real = shutil.which
    shutil.which = lambda cmd, *a, **k: None
    try:
        assert chatcheck.dws_status() == {"installed": False, "ok": False, "detail": "PATH 里没有 dws"}
    finally:
        shutil.which = real
