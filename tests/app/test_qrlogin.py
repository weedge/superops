"""网页里扫码登录（参照 Easel 的账号页）：状态文件和验证码文件、发布台的扫码状态 / 二维码 / 验证码 / 取消、
扫码登录和校验账号读回的昵称粉丝数、退出登录清理，以及三个平台身份接口的解析。浏览器部分不在这里跑（要真平台）。"""
from __future__ import annotations

import json
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
def q(world):
    from superops.biz.executors.publishers import qrlogin
    return qrlogin


def test_状态文件_二维码只在等扫码时带时间戳_验证码读走就删(q):
    q.write_status("douyin", "starting", "准备中")
    assert q.read_status("douyin")["state"] == "starting" and "qr_ts" not in q.read_status("douyin")
    q.qr_file("douyin").write_bytes(b"\x89PNG")
    q.write_status("douyin", "qr_ready", "请扫码")
    assert q.read_status("douyin")["qr_ts"] > 0
    assert q.read_status("xiaohongshu") == {"state": "none", "message": ""}
    q.submit_code("douyin", "12 34-56")
    assert q.take_code("douyin") == "123456" and q.take_code("douyin") == ""


def test_发布台_扫码状态_二维码_验证码_过时的中途状态判出错(world, q):
    desk = http._desk(world)
    q.write_status("douyin", "qr_ready", "请扫码")                    # 没有登录进程在跑：中途状态是上次残留的
    assert desk.login_status("douyin")["state"] == "error" and desk.login_status("douyin")["running"] is False
    desk.jobs["j1"] = {"id": "j1", "action": "qrlogin", "label": "扫码登录", "target": "douyin", "platform": "douyin", "state": "running",
                       "started_at": "2026-11-13T00:00:00Z"}
    q.qr_file("douyin").write_bytes(b"\x89PNG")
    st = http.handle_get(world, "/api/publish/login", {"platform": "douyin"})
    assert st["state"] == "qr_ready" and st["running"] is True and st["qr_ts"]
    assert desk.image_path("douyin", "qr").name == "douyin.png"
    with pytest.raises(ApiError):                                      # 不是等验证码的时候
        http.handle_post(world, "/api/publish/accounts", {"action": "code", "platform": "douyin", "code": "123456"})
    q.write_status("douyin", "sms_required", "验证码已发送")
    with pytest.raises(Exception):                                     # 二维码只在等扫码时给
        desk.image_path("douyin", "qr")
    with pytest.raises(ApiError):
        http.handle_post(world, "/api/publish/accounts", {"action": "code", "platform": "douyin", "code": "12"})
    http.handle_post(world, "/api/publish/accounts", {"action": "code", "platform": "douyin", "code": "123456"})
    assert q.take_code("douyin") == "123456"
    with pytest.raises(Exception):
        desk.image_path("douyin", "avatar")
    with pytest.raises(Exception):
        desk.image_path("weibo", "qr")


def test_扫码登录读回昵称粉丝数_有头像就带上_取消能停掉进程(world, q, monkeypatch):
    from superops.app.publishing import PublishDesk, _read_accounts
    fake = world.tmp / "fake_qr.py"
    fake.write_text(
        "import sys, json, time\n"
        "if 'xiaohongshu' in sys.argv: time.sleep(30)\n"
        "print('✓ 抖音 登录成功：某律师')\n"
        "print(json.dumps({'account': {'logged_in': True, 'name': '某律师', 'id': 'lawyer01', 'followers': 1234}}, ensure_ascii=False))\n",
        encoding="utf-8")
    desk = PublishDesk(lambda: {}, lambda: "2026-11-13T00:00:00Z", cli=[sys.executable, str(fake)])
    desk.start("qrlogin", "douyin")
    _join()
    acct = _read_accounts()["douyin"]
    assert acct["status"] == "ok" and acct["name"] == "某律师" and acct["followers"] == 1234 and acct["logged_in_at"]
    (q.state_dir() / "douyin-avatar.jpg").write_bytes(b"\xff\xd8")
    card = next(a for a in desk.accounts() if a["platform"] == "douyin")
    assert card["name"] == "某律师" and card["account_id"] == "lawyer01" and card["avatar"]
    job = desk.start("qrlogin", "xiaohongshu")                         # 一直等扫码的进程
    for _ in range(50):
        if job.get("_proc"):
            break
        threading.Event().wait(0.1)
    assert desk.cancel("xiaohongshu")["stopped"] is True
    _join()
    assert desk.jobs[job["id"]]["state"] == "failed" and q.read_status("xiaohongshu")["state"] == "cancelled"
    assert "上次扫码" not in str(_read_accounts().get("xiaohongshu", {}).get("note"))   # 人取消的不记成失败
    assert all(not k.startswith("_") for j in desk.job_list() for k in j)            # 进程句柄不进接口


def test_退出登录清掉头像和扫码状态(world, q):
    from superops.biz import paths
    (paths.ops_data() / "browser-profiles" / "douyin" / "Default").mkdir(parents=True)
    (q.state_dir() / "douyin-avatar.png").write_bytes(b"x")
    q.write_status("douyin", "success", "登录成功")
    http.handle_post(world, "/api/publish/accounts", {"action": "logout", "platform": "douyin"})
    assert not list(q.state_dir().glob("douyin*"))
    card = next(a for a in http._desk(world).accounts() if a["platform"] == "douyin")
    assert card["status"] == "none" and not card["name"] and not card["avatar"]


class FakePage:
    def __init__(self, resp):
        self.resp, self.url = resp, "https://creator.douyin.com/creator-micro/home"

    def evaluate(self, js, arg=None):
        return self.resp


class FakeCtx:
    class request:          # noqa: N801
        payload: dict = {}

        @classmethod
        def get(cls, url, **kw):
            return type("R", (), {"status": 200, "json": lambda self: cls.payload})()


def test_三个平台的身份接口解析(q):
    d = q._douyin_identity(FakePage({"status_code": 0, "user": {"uid": "1", "nickname": "某律师", "unique_id": "lawyer01", "follower_count": 9,
                                                                 "avatar_thumb": {"url_list": ["javascript:alert(1)", "https://p3.douyinpic.com/a.jpeg"]}}}))
    assert d == {"name": "某律师", "avatar": "https://p3.douyinpic.com/a.jpeg", "id": "lawyer01", "followers": 9}
    assert q._douyin_identity(FakePage({"status_code": 8})) is None                  # 没登录
    FakeCtx.request.payload = {"success": True, "data": {"userId": "u1", "userName": "某号", "userAvatar": "//sns-avatar.xhscdn.com/x", "fansCount": 5}}
    x = q._xhs_identity(FakeCtx)
    assert x["name"] == "某号" and x["avatar"] == "https://sns-avatar.xhscdn.com/x" and x["followers"] == 5
    FakeCtx.request.payload = {"success": False}
    assert q._xhs_identity(FakeCtx) is None
    c = q._channels_identity({"errCode": 0, "data": {"finderUser": {"nickname": "某视频号", "headImgUrl": "https://wx.qlogo.cn/x", "fansCount": 3}}})
    assert c["name"] == "某视频号" and c["avatar"] == "https://wx.qlogo.cn/x"
    assert q._channels_identity({"errCode": 300}) is None
    assert q._clean_avatar("https://user:pw@evil/x") == "" and q._clean_avatar("ftp://x") == ""


def test_其他社媒账号_只管账号不进发布(world, q):
    desk = http._desk(world)
    accts = {a["platform"]: a for a in desk.accounts()}
    assert set(accts) == {"douyin", "xiaohongshu", "wechat_channels", "kuaishou", "bilibili", "zhihu", "wechat_oa"}
    assert all(accts[p]["publish"] for p in ("douyin", "xiaohongshu", "wechat_channels"))
    for p in ("kuaishou", "bilibili", "zhihu", "wechat_oa"):
        assert accts[p]["publish"] is False and accts[p]["mode"] == "none" and accts[p]["upload"].startswith("https://") and not accts[p]["drafts"]
    from superops.biz.executors import publishers
    assert "zhihu" not in publishers.PLATFORMS                       # 发布、登录检查、数据采集只认能发布的三个
    with pytest.raises(Exception):
        desk.start("open", "zhihu")                                  # 没有草稿箱
    assert desk.login_status("bilibili")["state"] == "none"
    with pytest.raises(Exception):
        desk.start("qrlogin", "weibo")


class FakeReq:
    def __init__(self, routes):
        self.routes = routes

    def get(self, url, **kw):
        body = self.routes.get(url)
        return type("R", (), {"status": 200 if body is not None else 404, "json": lambda self: body or {}})()

    post = get


class FakePages:
    def __init__(self, urls):
        self.pages = [type("P", (), {"url": u, "is_closed": lambda self: False})() for u in urls]


def test_其他平台的登录判断和身份解析(q):
    bili = type("C", (), {"request": FakeReq({q.BILI_NAV: {"data": {"isLogin": True, "uname": "某UP", "face": "https://i0.hdslb.com/a.jpg", "mid": 42}},
                                               q.BILI_STAT: {"data": {"follower": 7}}}), "pages": []})()
    assert q.logged_in("bilibili", bili) is True
    assert q._bili_identity(bili) == {"name": "某UP", "avatar": "https://i0.hdslb.com/a.jpg", "id": "42", "followers": 7}
    zh = type("C", (), {"request": FakeReq({q.ZHIHU_ME: {"id": "abc", "name": "某答主", "avatar_url": "https://pic1.zhimg.com/x.jpg",
                                                         "url_token": "some-one", "follower_count": 3}}), "pages": []})()
    assert q.logged_in("zhihu", zh) is True and q._zhihu_identity(zh)["id"] == "some-one"
    out = type("C", (), {"request": FakeReq({q.ZHIHU_ME: {"error": {"code": 100}}}), "pages": []})()
    assert q.logged_in("zhihu", out) is False and q._zhihu_identity(out) is None
    ks = type("C", (), {"request": FakeReq({q.KUAISHOU_USER_API: {"result": 1}}), "pages": []})()
    assert q.logged_in("kuaishou", ks) is True
    assert q.logged_in("kuaishou", type("C", (), {"request": FakeReq({q.KUAISHOU_USER_API: {"result": 109}}), "pages": []})()) is False
    assert q.logged_in("wechat_oa", FakePages(["https://mp.weixin.qq.com/cgi-bin/home?t=home/index&token=123456&lang=zh_CN"])) is True
    assert q.logged_in("wechat_oa", FakePages(["https://mp.weixin.qq.com/", "https://evil.example/?token=1"])) is False
