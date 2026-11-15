"""内容日历上人做的事：挪日期（还没批的发布包跟着挪）、删除（要确认，在发布 / 已发的不让删）、
发布（要确认；写好稿的图文才行，发出带人工批准凭据的 content.images.requested；计划日已过就今天发）。"""
from __future__ import annotations

import json

import pytest

from superops.app import http
from superops.app.http import ApiError


def _content(world, cid, fmt="小红书图文，8 页", status="scripted", pack=None, platform="xiaohongshu"):
    from superops.biz import db
    from superops.infra import paths
    f = paths.ops_data() / "work" / "t1" / "scripts" / cid / f"{platform}.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"# 稿子\n- 平台：{fmt}\n", encoding="utf-8")
    ops = db.open_ops()
    ops.execute("INSERT INTO contents(id,topic,script_path,status,created_at) VALUES (?,?,?,?,?)",
                (cid, "选题", json.dumps([f"work/t1/scripts/{cid}/{platform}.md"]), status, "2026-11-10T00:00:00+08:00"))
    if pack:
        ops.execute("INSERT INTO publish_packs(id,content_id,platform,title,pack_dir,status,scheduled_at,created_at,updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)", (f"pk_{cid}_xiaohongshu", cid, "xiaohongshu", "t", "/x", pack, f"{cid[2:6]}-{cid[6:8]}-{cid[8:10]}T20:00:00+08:00",
                                                     "2026-11-10T00:00:00Z", "2026-11-10T00:00:00Z"))
    ops.commit()
    ops.close()
    return cid


def _row(sql, *args):
    from superops.biz import db
    ops = db.open_ops()
    try:
        return ops.execute(sql, args).fetchone()
    finally:
        ops.close()


def test_挪日期_还没批的发布包跟着挪(world):
    cid = _content(world, "c_20261114_01", pack="ready")
    http.handle_post(world, "/api/contents", {"action": "move", "id": cid, "date": "2026-11-16"})
    assert _row("SELECT planned_date FROM contents WHERE id=?", cid)[0] == "2026-11-16"
    assert _row("SELECT scheduled_at FROM publish_packs WHERE content_id=?", cid)[0] == "2026-11-16T20:00:00+08:00"
    from superops.biz import bizviews
    cal = {d["date"]: [x["id"] for x in d["items"]] for d in bizviews.business()["content"]["calendar"]}
    assert cid in cal["2026-11-16"] and cid not in cal.get("2026-11-14", [])
    for bad in ({"action": "move", "id": cid, "date": "11-16"}, {"action": "move", "id": "c_none", "date": "2026-11-16"}):
        with pytest.raises(ApiError):
            http.handle_post(world, "/api/contents", bad)


def test_删除要确认_在发布的不让删_没批的发布包一起作废(world):
    cid = _content(world, "c_20261115_01", pack="carded")
    with pytest.raises(ApiError):
        http.handle_post(world, "/api/contents", {"action": "delete", "id": cid})
    http.handle_post(world, "/api/contents", {"action": "delete", "id": cid, "confirm": True})
    assert _row("SELECT status FROM contents WHERE id=?", cid)[0] == "deleted"
    assert _row("SELECT status FROM publish_packs WHERE content_id=?", cid)[0] == "rejected"
    from superops.biz import bizviews
    assert cid not in [x["id"] for d in bizviews.business()["content"]["calendar"] for x in d["items"]]
    busy = _content(world, "c_20261115_02", pack="drafted")
    with pytest.raises(ApiError):
        http.handle_post(world, "/api/contents", {"action": "delete", "id": busy, "confirm": True})


def test_发布_写好稿的图文才行_发出带凭据的出图事件(world, monkeypatch):
    from superops.infra import timeutil
    monkeypatch.setattr(timeutil, "now", lambda: timeutil.parse_iso("2026-11-13T10:00:00+08:00"))
    sent = []
    monkeypatch.setattr(world.pipeline, "emit", lambda event, **kw: sent.append((event, kw)) or {"routed": "trigger"})
    late = _content(world, "c_20261111_09")                                  # 计划日已过：今天发
    with pytest.raises(ApiError):
        http.handle_post(world, "/api/contents", {"action": "publish", "id": late})
    r = http.handle_post(world, "/api/contents", {"action": "publish", "id": late, "confirm": True})
    event, kw = sent[0]
    assert r["date"] == "2026-11-13" and event["type"] == "content.images.requested" and event["subject"] == f"content:{late}"
    assert event["data"]["platforms"] == ["xiaohongshu"] and event["data"]["scheduled_at"] == "2026-11-13T20:00:00+08:00"
    assert kw["approved"]["via"] == "panel" and _row("SELECT planned_date FROM contents WHERE id=?", late)[0] == "2026-11-13"
    video = _content(world, "c_20261116_01", fmt="口播视频 60 秒")
    flowing = _content(world, "c_20261116_02", pack="ready")
    draft = _content(world, "c_20261116_03", status="planned")
    article = _content(world, "c_20261116_04", fmt="知乎图文回答", platform="zhihu")      # 知乎的“图文”是文章，不做成竖图
    for cid in (video, flowing, draft, article):
        with pytest.raises(ApiError):
            http.handle_post(world, "/api/contents", {"action": "publish", "id": cid, "confirm": True})
    from superops.biz import bizviews
    items = {x["id"]: x for d in bizviews.business()["content"]["calendar"] for x in d["items"]}
    assert items["c_20261116_01"]["publish"]["ok"] is False and "视频" in items["c_20261116_01"]["publish"]["why"]
    assert items["c_20261116_02"]["publish"]["ok"] is False and "发布中心" in items["c_20261116_02"]["publish"]["why"]
    assert items["c_20261116_04"]["publish"]["ok"] is False and "文章" in items["c_20261116_04"]["publish"]["why"]
