"""数据采集按账号分开：同一平台多个号时，每个号各开一次自己的浏览器读作品列表；没记账号的作品（多账号之前发的）算主号。"""
from __future__ import annotations

import pytest


def test_数据采集按账号分开开浏览器_没记账号的算主号(home, monkeypatch):
    from superops.biz import db
    from superops.biz.collectors import creator, metrics
    ops = db.open_ops()
    a = metrics.register_post(ops, {"platform": "douyin", "platform_post_id": "7706848829571648819", "title": "主号的",
                                    "published_at": "2026-11-11T09:00:00+08:00"})
    b = metrics.register_post(ops, {"platform": "douyin", "account": "douyin@2", "platform_post_id": "7706857639103286538", "title": "二号的",
                                    "published_at": "2026-11-11T09:00:00+08:00"})
    calls = []

    def fake_list(platform, want, *, since=None, account=None, **k):
        calls.append((platform, account, set(want)))
        if account == "douyin@2":
            raise RuntimeError("二号登录失效")
        return [{"id": x, "titles": [], "url": "u", "created": None, "metrics": {"views": 9, "likes": 0, "comments": 0}} for x in want]
    monkeypatch.setattr(creator, "list_works", fake_list)
    due = [dict(r) for r in ops.execute("SELECT p.id AS pid, p.platform, p.account, p.platform_post_id, p.title, p.published_at FROM posts p")]
    got, errs, _ = metrics.creator_pass(ops, due)
    assert sorted(calls, key=lambda c: str(c[1])) == [("douyin", None, {"7706848829571648819"}), ("douyin", "douyin@2", {"7706857639103286538"})]
    assert a in got and b not in got and list(errs) == ["douyin@2"]
    j = next(x for x in due if x["pid"] == b)
    with pytest.raises(RuntimeError, match="二号登录失效"):
        metrics._snapshot_one(ops, j, "24h", got, errs, {}, type("T", (), {"configured": False})(), 0, 0)


def test_多个号时_待发布卡片写明账号_人工登记记下是哪个号发的(home, tmp_path, monkeypatch):
    from superops.biz import db
    from superops.biz.executors import publish, publishers as pubs
    monkeypatch.setattr("superops.infra.config.load_agents_config", lambda *a, **k: {"publishers": {"douyin": {"mode": "draft"}}})
    ops = db.open_ops()
    for pid, acct in (("pk_a_douyin", None), ("pk_b_douyin", "douyin@2"), ("pk_c_douyin", "不存在的号")):
        ops.execute("INSERT INTO publish_packs(id,content_id,platform,account,title,pack_dir,publish_mode,status) VALUES (?,?,?,?,?,?,?,?)",
                    (pid, pid[3], "douyin", acct, "标题", str(tmp_path), "semi_auto", "ready"))
    ops.commit()
    pubs.write_account("douyin", status="ok", name="主号")
    pubs.write_account("douyin@2", status="ok", name="二号")
    _, card = publish.build_batch_card(ops)
    notes = {i["pack_id"]: i["note"] for i in card["items"]}
    assert "账号：抖音「主号」" in notes["pk_a_douyin"] and "账号：抖音「二号」" in notes["pk_b_douyin"]
    assert "⚠️" in notes["pk_c_douyin"] and "不在已登录的账号里" in notes["pk_c_douyin"]
    ops.execute("UPDATE publish_packs SET status='approved'")
    ops.commit()
    assert publish.record_manual("pk_b_douyin")["account"] == "douyin@2"
    assert publish.record_manual("pk_a_douyin")["account"] == "douyin"
    assert publish.record_manual("pk_c_douyin")["account"] is None          # 认不出来就不乱记


def test_只有一个号时_卡片不多写账号(home, tmp_path, monkeypatch):
    from superops.biz import db
    from superops.biz.executors import publish
    monkeypatch.setattr("superops.infra.config.load_agents_config", lambda *a, **k: {"publishers": {}})
    ops = db.open_ops()
    ops.execute("INSERT INTO publish_packs(id,content_id,platform,title,pack_dir,publish_mode,status) VALUES (?,?,?,?,?,?,?)",
                ("pk_a_douyin", "a", "douyin", "标题", str(tmp_path), "semi_auto", "ready"))
    ops.commit()
    _, card = publish.build_batch_card(ops)
    assert "账号" not in card["items"][0]["note"]
