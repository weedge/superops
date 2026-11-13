"""面板跟进看板上人做的事：改跟进阶段（已联系 / 已回复 / 不跟进 / 拉回待跟进）、登记自己发了评论。只改业务库，不对外发东西。"""
from __future__ import annotations

import pytest

from superops.app import http
from superops.app.http import ApiError


def _lead(lid="ld_1", stage="待跟进", comment_status="manual"):
    from superops.biz import db
    ops = db.open_ops()
    ops.execute("INSERT INTO leads(id,platform,nickname,score,stage,comment_text,comment_status,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (lid, "douyin", "某律师", 80, stage, "评论原文", comment_status, "2026-11-13T00:00:00Z"))
    ops.commit()
    ops.close()
    return lid


def _row(lid):
    from superops.biz import db
    ops = db.open_ops()
    try:
        return dict(ops.execute("SELECT stage, comment_status, commented_at, comment_note FROM leads WHERE id=?", (lid,)).fetchone())
    finally:
        ops.close()


def test_改跟进阶段_只认四个阶段(world):
    lid = _lead()
    assert http.handle_post(world, "/api/leads", {"action": "stage", "id": lid, "stage": "已联系"})["stage"] == "已联系"
    http.handle_post(world, "/api/leads", {"action": "stage", "id": lid, "stage": "不跟进", "note": "同行"})
    assert _row(lid)["stage"] == "不跟进" and _row(lid)["comment_note"] == "同行"
    http.handle_post(world, "/api/leads", {"action": "stage", "id": lid, "stage": "待跟进"})          # 从归档拉回
    assert _row(lid)["stage"] == "待跟进"
    for bad in ({"action": "stage", "id": lid, "stage": "已评论"}, {"action": "stage", "id": lid, "stage": "发现"},
                {"action": "stage", "id": "ld_none", "stage": "已联系"}, {"action": "delete", "id": lid}):
        with pytest.raises(ApiError):
            http.handle_post(world, "/api/leads", bad)


def test_登记自己发了评论_挪到已评论_不重复登记(world, monkeypatch):
    lid = _lead()
    sent = []
    monkeypatch.setattr(world.pipeline, "emit", lambda event, **kw: sent.append(event) or {"routed": "trigger"})
    r = http.handle_post(world, "/api/leads", {"action": "commented", "id": lid})
    row = _row(lid)
    assert r["stage"] == "已评论" and row["comment_status"] == "sent" and row["commented_at"] and sent == []   # 不发事件、不对外
    with pytest.raises(ApiError):
        http.handle_post(world, "/api/leads", {"action": "commented", "id": lid})


def test_获客视图带监听漏斗(world):
    _lead("ld_a", "已评论", "sent")
    _lead("ld_b", "粉丝不足", None)
    _lead("ld_c", "已回复", "sent")
    from superops.biz import bizviews
    f = bizviews.business()["growth"]["funnel"]
    assert (f["leads"], f["carded"], f["commented"], f["contacted"], f["replied"]) == (3, 2, 2, 1, 1)
