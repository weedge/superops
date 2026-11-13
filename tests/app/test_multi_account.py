"""同一平台多个号（方案 A）：账号键（主号 = 平台名，后加的号 douyin@2…）、默认账号、发布包发到哪个号、
添加 / 设为默认 / 移除、按账号分开的登录状态文件。（数据采集按账号分开见 tests/biz/test_metrics_accounts.py）浏览器动作用假的命令行代替。"""
from __future__ import annotations

import sys
import threading

import pytest

from superops.app import http
from superops.app.http import ApiError


def _join():
    for t in threading.enumerate():
        if t.name.startswith("publish-"):
            t.join(15)


@pytest.fixture
def pubs(world):
    from superops.biz.executors import publishers
    return publishers


def _profile(pubs, key):
    d = pubs.profiles_root() / key / "Default"
    d.mkdir(parents=True, exist_ok=True)


def _fake_cli(world, name="某号"):
    fake = world.tmp / "fake_multi.py"
    fake.write_text(
        "import sys, json\n"
        f"print('✓ 登录成功：{name}')\n"
        f"print(json.dumps({{'account': {{'logged_in': True, 'name': sys.argv[-1] + '{name}', 'id': sys.argv[-1] + '-id', 'followers': 7}}}}, ensure_ascii=False))\n",
        encoding="utf-8")
    return [sys.executable, str(fake)]


def test_账号键_主号是平台名_后加的号带序号(pubs):
    assert pubs.platform_of("douyin@2") == "douyin" and pubs.platform_of("douyin") == "douyin"
    assert pubs.is_account_key("douyin") and pubs.is_account_key("douyin@2") and pubs.is_account_key("zhihu@work")
    assert not pubs.is_account_key("weibo") and not pubs.is_account_key("douyin@") and not pubs.is_account_key("douyin@../x")
    assert pubs.account_keys("douyin") == ["douyin"] and pubs.default_account("douyin") == "douyin"
    pubs.write_account("douyin@3", status="ok", name="三号")
    _profile(pubs, "douyin@2")                                    # 登记丢了也认登录目录
    assert pubs.account_keys("douyin") == ["douyin", "douyin@2", "douyin@3"]
    assert pubs.next_account_key("douyin") == "douyin@4" and pubs.next_account_key("xiaohongshu") == "xiaohongshu@2"
    assert pubs.account_label("douyin@3") == "抖音「三号」" and pubs.account_label("douyin@2") == "抖音 2号"


def test_默认账号_设了用设的_移除后回到主号(pubs):
    pubs.write_account("douyin@2", status="ok", name="二号")
    pubs.set_default("douyin@2")
    assert pubs.default_account("douyin") == "douyin@2" and pubs.default_account("xiaohongshu") == "xiaohongshu"
    pubs.forget_account("douyin@2")
    assert pubs.default_account("douyin") == "douyin" and "douyin@2" not in pubs.read_registry()


def test_发布包的账号_没写用默认_账号键账号名备注都认_认不出来不猜(pubs):
    pubs.write_account("douyin", status="ok", name="主号昵称")
    pubs.write_account("douyin@2", status="ok", name="二号昵称", nickname="正式号")
    p = {"id": "pk_c_1_douyin", "platform": "douyin", "account": None}
    assert pubs.pack_account(p) == "douyin"
    pubs.set_default("douyin@2")
    assert pubs.pack_account(p) == "douyin@2"
    assert pubs.resolve_account("douyin", "douyin") == "douyin"
    assert pubs.resolve_account("douyin", "@主号昵称") == "douyin" and pubs.resolve_account("douyin", "正式号") == "douyin@2"
    assert pubs.pack_account({**p, "account": None}, {"per_platform": {"抖音": {"account": "主号昵称"}}}) == "douyin"
    assert pubs.pack_account({**p, "account": "douyin@2"}, {"per_platform": {"抖音": {"account": "主号昵称"}}}) == "douyin@2"   # 面板改的优先
    with pytest.raises(pubs.AccountError):
        pubs.resolve_account("douyin", "不存在的号")
    with pytest.raises(pubs.AccountError):
        pubs.resolve_account("douyin", "xiaohongshu")              # 别的平台的号不算
    pl = pubs.payload_for({**p, "account": "不存在的号"}, {})
    assert pl["account"] is None and "不在已登录的账号里" in pl["account_error"]
    with pytest.raises(pubs.AccountError):                         # 不会拿别的号去发
        pubs.run({**p, "account": "不存在的号"}, {}, dry_run=True)


def test_发布台_每个号一行_添加账号直接扫码_设为默认要登录着(world, pubs):
    from superops.app.publishing import PublishDesk
    desk = PublishDesk(lambda: {"publishers": {"douyin": {"mode": "draft"}}}, lambda: "2026-11-13T00:00:00Z", cli=_fake_cli(world))
    pubs.write_account("douyin", status="ok", name="主号")
    job = desk.add_account("douyin")
    assert job["key"] == "douyin@2" and job["action"] == "qrlogin" and job["account"] == "douyin@2"
    _join()
    rows = [a for a in desk.accounts() if a["platform"] == "douyin"]
    assert [a["key"] for a in rows] == ["douyin", "douyin@2"]
    assert rows[0]["default"] and rows[0]["main"] and not rows[1]["default"] and rows[1]["slug"] == "2" and rows[1]["accounts"] == 2
    assert rows[1]["status"] == "ok" and rows[1]["name"] == "douyin@2某号" and rows[1]["mode"] == "draft"
    with pytest.raises(ApiError):                                   # 没登录的号不能设为默认
        http.handle_post(world, "/api/publish/accounts", {"action": "default", "key": "xiaohongshu"})
    http._desk(world)
    r = http.handle_post(world, "/api/publish/accounts", {"action": "default", "key": "douyin@2"})
    assert r["default"] == "douyin@2" and pubs.default_account("douyin") == "douyin@2"
    with pytest.raises(ApiError):
        http.handle_post(world, "/api/publish/accounts", {"action": "add", "platform": "weibo"})


def test_添加账号没登上的号先复用_不越加越多(world, pubs):
    from superops.app.publishing import PublishDesk
    fake = world.tmp / "fake_fail.py"
    fake.write_text("print('✗ 没登录成功')\nimport sys; sys.exit(1)\n", encoding="utf-8")
    desk = PublishDesk(lambda: {}, lambda: "2026-11-13T00:00:00Z", cli=[sys.executable, str(fake)])
    assert desk.add_account("xiaohongshu")["key"] == "xiaohongshu@2"
    _join()
    assert desk.add_account("xiaohongshu")["key"] == "xiaohongshu@2"
    _join()
    assert pubs.account_keys("xiaohongshu") == ["xiaohongshu", "xiaohongshu@2"]


def test_同一个号登了两次会提醒(world, pubs):
    from superops.app.publishing import PublishDesk
    fake = world.tmp / "fake_same.py"
    fake.write_text("import json\nprint('✓ 登录成功')\nprint(json.dumps({'account': {'logged_in': True, 'name': '同一个', 'id': 'same-id'}}))\n",
                    encoding="utf-8")
    pubs.write_account("douyin", status="ok", name="同一个", account_id="same-id")
    desk = PublishDesk(lambda: {}, lambda: "2026-11-13T00:00:00Z", cli=[sys.executable, str(fake)])
    desk.add_account("douyin")
    _join()
    assert "是同一个号" in pubs.read_registry()["douyin@2"]["note"]


def test_同一平台的两个号互不占位_状态文件分开_移除只清自己的(world, pubs):
    from superops.biz.executors.publishers import qrlogin as q
    desk = http._desk(world)
    _profile(pubs, "douyin")
    _profile(pubs, "douyin@2")
    pubs.write_account("douyin@2", status="ok", name="二号")
    pubs.set_default("douyin@2")
    desk.jobs["j1"] = {"id": "j1", "action": "qrlogin", "label": "扫码登录", "target": "douyin", "account": "douyin", "platform": "douyin",
                       "state": "running", "started_at": "2026-11-13T00:00:00Z"}
    assert desk._running("douyin") and not desk._running("douyin@2")
    q.write_status("douyin", "qr_ready", "请扫码")
    q.write_status("douyin@2", "success", "登录成功")
    (q.state_dir() / "douyin-avatar.png").write_bytes(b"x")
    (q.state_dir() / "douyin@2-avatar.png").write_bytes(b"y")
    assert http.handle_get(world, "/api/publish/login", {"key": "douyin@2"})["state"] == "success"
    assert desk.image_path("douyin@2", "avatar").name == "douyin@2-avatar.png"
    r = http.handle_post(world, "/api/publish/accounts", {"action": "logout", "key": "douyin@2"})
    assert r["removed"] is True and r["moved"]
    assert (q.state_dir() / "douyin-avatar.png").exists() and q.read_status("douyin")["state"] == "qr_ready"   # 主号的不动
    assert not list(q.state_dir().glob("douyin@2*"))
    assert pubs.account_keys("douyin") == ["douyin"] and pubs.default_account("douyin") == "douyin"
    with pytest.raises(ApiError):                                   # 主号在跑扫码，不能退出
        http.handle_post(world, "/api/publish/accounts", {"action": "logout", "key": "douyin"})


def test_发布包换账号_只能换成这个平台的号_发出去的不能改(world, pubs):
    from tests.app.test_publishing import _pack
    pubs.write_account("douyin@2", status="ok", name="二号")
    pid = _pack(world)
    detail = http._desk(world).pack(pid)
    assert detail["account_key"] == "douyin" and [c["key"] for c in detail["account_choices"]] == ["douyin", "douyin@2"]
    assert detail["account_editable"] is True
    r = http.handle_post(world, "/api/publish/packs", {"action": "account", "id": pid, "account": "douyin@2"})
    assert r["account"] == "douyin@2" and http._desk(world).pack(pid)["account_key"] == "douyin@2"
    with pytest.raises(ApiError):
        http.handle_post(world, "/api/publish/packs", {"action": "account", "id": pid, "account": "xiaohongshu"})
    http.handle_post(world, "/api/publish/packs", {"action": "account", "id": pid, "account": ""})      # 空着 = 用默认账号
    assert http._desk(world).pack(pid)["account_key"] == "douyin"
    done = _pack(world, status="published", pid="pk_c_20261113_02_douyin")
    with pytest.raises(ApiError):
        http.handle_post(world, "/api/publish/packs", {"action": "account", "id": done, "account": "douyin@2"})


def test_发布包的浏览器动作占住它要发的那个号(world, pubs):
    from superops.app.publishing import PublishDesk
    from tests.app.test_publishing import _pack
    fake = world.tmp / "fake_slow.py"
    fake.write_text("import time; time.sleep(0.8); print('✓ 预演')\n", encoding="utf-8")
    pubs.write_account("douyin@2", status="ok", name="二号")
    desk = PublishDesk(lambda: {}, lambda: "2026-11-13T00:00:00Z", cli=[sys.executable, str(fake)])
    pid = _pack(world)
    desk.set_pack_account(pid, "douyin@2")
    job = desk.start("try", pid)
    assert job["account"] == "douyin@2"
    desk.start("check", "douyin")                                   # 主号不受影响
    with pytest.raises(Exception):
        desk.start("check", "douyin@2")
    _join()


def test_社媒账号上下文_每个号一行_标出默认(world, pubs):
    from superops.biz import context as ctxmod
    pubs.write_account("douyin", status="ok", name="主号")
    pubs.write_account("douyin@2", status="ok", name="二号")
    pubs.set_default("douyin@2")
    out = {}
    fake = type("B", (), {"_write": lambda self, name, data, desc: out.update({name: data})})()
    ctxmod.ContextBuilder.p_social_accounts(fake, {}, [])
    rows = [r for r in out["social_accounts.json"]["accounts"] if r["platform"] == "douyin"]
    assert [(r["account_key"], r["is_default"], r["name"]) for r in rows] == [("douyin", False, "主号"), ("douyin@2", True, "二号")]
    assert "douyin@2" in out["social_accounts.json"]["note"]
