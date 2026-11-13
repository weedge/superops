"""面板发布台：社媒账号的登录目录、发布包的批准 / 登记 / 预览，和只给发布包目录里媒体文件的下载口。浏览器动作用假的命令行代替。"""
from __future__ import annotations

import json
import sys

import pytest

from superops.app import http
from superops.app.http import ApiError


def _pack(world, status="ready", pid="pk_c_20261113_01_douyin", platform="douyin", meta=None):
    from superops.biz import db
    d = world.tmp / "pack" / pid
    d.mkdir(parents=True, exist_ok=True)
    (d / "cover.jpg").write_bytes(b"\xff\xd8jpg")
    (d / "v.mp4").write_bytes(b"0000")
    (d / "notes.txt").write_text("不是媒体")
    (d / "meta.json").write_text(json.dumps(meta or {"title": "总标题", "media": {"cover": "cover.jpg", "video": "v.mp4"},
                                                     "per_platform": {"抖音": {"title": "抖音标题", "body": "正文", "topics": ["#AI", "数字人"]}}},
                                                    ensure_ascii=False), encoding="utf-8")
    ops = db.open_ops()
    ops.execute("INSERT INTO publish_packs(id,content_id,platform,title,pack_dir,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (pid, "c_20261113_01", platform, "抖音标题", str(d), status, "2026-11-13T00:00:00Z", "2026-11-13T00:00:00Z"))
    ops.commit()
    ops.close()
    return pid


def test_发布包详情给面板预览_按平台取文案和素材(world):
    pid = _pack(world)
    k = http.handle_get(world, "/api/publish/pack", {"id": pid})
    assert k["title"] == "抖音标题" and k["body"] == "正文" and k["topics"] == ["#AI", "数字人"]       # “抖音”键按平台代码对上
    assert k["cover"] == "cover.jpg" and {f["name"] for f in k["files"]} == {"cover.jpg", "v.mp4"}     # 只列媒体文件
    over = http.handle_get(world, "/api/publish", {})
    assert [x["id"] for x in over["packs"]] == [pid] and {a["platform"] for a in over["accounts"]} >= {"douyin", "xiaohongshu", "wechat_channels"}
    assert all(a["upload"].startswith("https://") for a in over["accounts"])
    assert all(a["limits"] for a in over["accounts"] if a["publish"])          # 只管账号的平台没有发布器，也就没有字数限制


def test_媒体下载口只给发布包目录里的媒体文件(world):
    pid = _pack(world)
    desk = http._desk(world)
    assert desk.media_path(pid, "cover.jpg").read_bytes().startswith(b"\xff\xd8")
    for bad in ("notes.txt", "../meta.json", "meta.json", ".hidden.jpg", "sub/x.jpg", ""):
        with pytest.raises(Exception):
            desk.media_path(pid, bad)
    with pytest.raises(Exception):
        desk.media_path("pk_nope", "cover.jpg")


def test_批准发布要确认_只批还没发的_带人工批准凭据发出事件(world, monkeypatch):
    pid = _pack(world)
    sent = []
    monkeypatch.setattr(world.pipeline, "emit", lambda event, **kw: sent.append((event, kw)) or {"routed": "trigger"})
    with pytest.raises(ApiError):
        http.handle_post(world, "/api/publish/packs", {"action": "approve", "ids": [pid]})            # 没确认
    r = http.handle_post(world, "/api/publish/packs", {"action": "approve", "ids": [pid], "confirm": True})
    assert r["approved"] == [pid]
    event, kw = sent[0]
    assert event["type"] == "content.publish.approved" and event["data"]["pack_id"] == pid and kw["approved"]["via"] == "panel"
    done = _pack(world, status="published", pid="pk_done", platform="xiaohongshu")
    with pytest.raises(ApiError):
        http.handle_post(world, "/api/publish/packs", {"action": "approve", "ids": [done], "confirm": True})


def test_人工发完登记_链接要是网址_已发布的不重复登记(world, monkeypatch):
    pid = _pack(world, status="approved")
    sent = []
    monkeypatch.setattr(world.pipeline, "emit", lambda event, **kw: sent.append(event) or {"routed": "trigger"})
    with pytest.raises(ApiError):
        http.handle_post(world, "/api/publish/packs", {"action": "record", "id": pid, "url": "javascript:alert(1)"})
    http.handle_post(world, "/api/publish/packs", {"action": "record", "id": pid, "url": "https://www.douyin.com/video/1"})
    assert sent[0]["type"] == "content.published.manual" and sent[0]["data"] == {"pack_id": pid, "by": "panel", "url": "https://www.douyin.com/video/1"}
    done = _pack(world, status="published", pid="pk_done2", platform="xiaohongshu")
    with pytest.raises(ApiError):
        http.handle_post(world, "/api/publish/packs", {"action": "record", "id": done})


def test_浏览器动作走子进程_同一平台同时只跑一个_检查结果记下来(world, monkeypatch):
    from superops.app.publishing import PublishDesk, _read_accounts
    fake = world.tmp / "fake_cli.py"
    fake.write_text("import sys, time\nif 'check' in sys.argv: print('✓ 抖音   已登录  https://creator.douyin.com/creator-micro/home')\n"
                    "else: time.sleep(0.5); print('✗ 预演：没找到按钮')\n", encoding="utf-8")
    desk = PublishDesk(lambda: {"publishers": {"douyin": {"mode": "draft"}}}, lambda: "2026-11-13T00:00:00Z", cli=[sys.executable, str(fake)])
    pid = _pack(world)
    job = desk.start("try", pid)
    with pytest.raises(Exception):                         # 同一平台（同一个登录目录）正在跑
        desk.start("check", "douyin")
    for t in __import__("threading").enumerate():
        if t.name.startswith("publish-"):
            t.join(10)
    assert desk.jobs[job["id"]]["state"] == "failed"      # ✗ 开头算没成功
    desk.start("check", "douyin")
    for t in __import__("threading").enumerate():
        if t.name.startswith("publish-"):
            t.join(10)
    assert _read_accounts()["douyin"]["status"] == "ok"
    acct = next(a for a in desk.accounts() if a["platform"] == "douyin")
    assert acct["status"] == "ok" and acct["mode"] == "draft" and not acct["busy"]
    with pytest.raises(Exception):
        desk.start("rm", "douyin")
    with pytest.raises(Exception):
        desk.start("check", "weibo")


def test_退出登录把登录目录挪走不直接删(world):
    from superops.biz import paths
    prof = paths.ops_data() / "browser-profiles" / "xiaohongshu"
    (prof / "Default").mkdir(parents=True)
    (prof / "Default" / "Cookies").write_text("x")
    r = http.handle_post(world, "/api/publish/accounts", {"action": "logout", "platform": "xiaohongshu"})
    assert not prof.exists() and (paths.ops_data() / "browser-profiles" / ".removed").is_dir() and r["moved"]
    acct = next(a for a in http._desk(world).accounts() if a["platform"] == "xiaohongshu")
    assert acct["status"] == "none" and not acct["profile"]
    http.handle_post(world, "/api/publish/accounts", {"action": "rename", "platform": "douyin", "name": "@示例账号"})
    assert next(a for a in http._desk(world).accounts() if a["platform"] == "douyin")["nickname"] == "@示例账号"
