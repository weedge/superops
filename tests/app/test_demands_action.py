"""面板需求池上人的决定：批准开发（带人工批准凭据的 demand.approved，要确认一次）、观察 / 搁置 / 恢复、提新需求。"""
from __future__ import annotations

import pytest

from superops.app import http
from superops.app.http import ApiError


def _demand(world, did="dm_1", status="open"):
    from superops.biz import db
    ops = db.open_ops()
    ops.execute("INSERT INTO demands(id,title,score,status,updated_at) VALUES (?,?,?,?,?)", (did, "横屏口播", 2.0, status, "2026-11-13T00:00:00Z"))
    ops.commit()
    ops.close()
    return did


def _status(did):
    from superops.biz import db
    ops = db.open_ops()
    try:
        return ops.execute("SELECT status, decided_ref FROM demands WHERE id=?", (did,)).fetchone()
    finally:
        ops.close()


def test_批准开发要确认_发出带凭据的事件_状态改成已批准(world, monkeypatch):
    did = _demand(world)
    sent = []
    monkeypatch.setattr(world.pipeline, "emit", lambda event, **kw: sent.append((event, kw)) or {"routed": "trigger"})
    with pytest.raises(ApiError):
        http.handle_post(world, "/api/demands", {"action": "approve", "id": did})                   # 没确认
    assert sent == [] and _status(did)["status"] == "open"
    r = http.handle_post(world, "/api/demands", {"action": "approve", "id": did, "confirm": True})
    event, kw = sent[0]
    assert r["status"] == "approved" and event["type"] == "demand.approved" and event["subject"] == f"demand:{did}"
    assert event["data"]["demand_id"] == did and event["data"]["title"] == "横屏口播" and kw["approved"]["via"] == "panel"
    assert _status(did)["status"] == "approved"
    with pytest.raises(ApiError):                                                                    # 批过的不再批
        http.handle_post(world, "/api/demands", {"action": "approve", "id": did, "confirm": True})
    with pytest.raises(ApiError):
        http.handle_post(world, "/api/demands", {"action": "approve", "id": "dm_none", "confirm": True})


def test_批准开发的事件带人工批准凭据(world):
    did = _demand(world)
    r = http.handle_post(world, "/api/demands", {"action": "approve", "id": did, "confirm": True})
    meta = {t["id"]: t["meta"] for t in world.store.recent_triggers()}[r["routed"]["id"]]
    assert not meta.get("unapproved") and meta["approved"]["via"] == "panel" and meta["approved"]["option"] == "approve"


def test_观察_搁置要原因_恢复(world):
    did = _demand(world)
    http.handle_post(world, "/api/demands", {"action": "watch", "id": did})
    assert _status(did)["status"] == "watching"
    with pytest.raises(ApiError):
        http.handle_post(world, "/api/demands", {"action": "shelve", "id": did})
    http.handle_post(world, "/api/demands", {"action": "shelve", "id": did, "reason": "成本太高，先不做"})
    st = _status(did)
    assert st["status"] == "shelved" and st["decided_ref"].endswith("：成本太高，先不做")
    from superops.biz import bizviews
    dm = next(x for x in bizviews.business()["demand"]["demands"] if x["id"] == did)
    assert dm["shelved_reason"] == "成本太高，先不做"
    http.handle_post(world, "/api/demands", {"action": "reopen", "id": did})
    assert _status(did)["status"] == "open"
    with pytest.raises(ApiError):
        http.handle_post(world, "/api/demands", {"action": "delete", "id": did})


def test_提新需求_同命令行的_demand_manual(world, monkeypatch):
    sent = []
    monkeypatch.setattr(world.pipeline, "emit", lambda event, **kw: sent.append(event) or {"routed": "trigger"})
    with pytest.raises(ApiError):
        http.handle_post(world, "/api/demands", {"action": "add", "text": "  "})
    http.handle_post(world, "/api/demands", {"action": "add", "text": "想要横屏口播模板"})
    assert sent[0]["type"] == "demand.manual" and sent[0]["data"]["text"] == "想要横屏口播模板"
