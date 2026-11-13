"""业务统计（superops.biz.bizviews）：只读 ops.db，返回可以直接 JSON 序列化的数据。"""
from __future__ import annotations

import json

import yaml

from superops.biz import bizviews, db
from superops.biz.timeutil import iso

from bizhelpers import at

SECTIONS = {"generated_at", "days", "since", "window", "summary", "daily", "content", "data", "demand", "growth", "funnel"}


def test_business_on_empty_db(home):
    at("2026-11-10 10:00")
    d = bizviews.business()
    assert json.loads(json.dumps(d)) == d                     # 不靠 default=str 也能序列化
    assert set(d) == SECTIONS
    assert (d["days"], d["window"], d["generated_at"]) == (7, "近 7 天", "2026-11-10T10:00:00+08:00")
    assert d["since"] == "2026-11-04T00:00:00+08:00"          # 整天对齐：今天 + 往前 6 天
    assert d["daily"]["days"][0] == "2026-11-04" and d["daily"]["days"][-1] == "2026-11-10"
    assert all(v == [0] * 7 for k, v in d["daily"].items() if k != "days")
    assert set(d["summary"].values()) == {0}
    assert d["content"]["total"] == 0 and d["content"]["calendar"] == [] and d["content"]["manual_publish"] == []
    assert d["data"]["posts"] == [] and d["data"]["jobs"] == {"done": 0, "pending": 0, "overdue": 0, "skipped": 0}
    assert d["demand"]["demands"] == [] and d["demand"]["signals"]["total"] == 0
    assert d["growth"]["total"] == 0 and d["growth"]["leads"] == []
    assert d["funnel"] == {"daily": [], "channels": [], "last_date": None,
                           "totals": {"clicks": 0, "registrations": 0, "first_done": 0, "second_done": 0, "paid": 0}}
    # 发布档位来自 agents.yaml，不在库里
    cfg = yaml.safe_load((home / "agents.yaml").read_text(encoding="utf-8"))
    assert d["content"]["publishers"] == [{"platform": k, "mode": v["mode"]} for k, v in cfg["publishers"].items()]


def _seed(now_iso: str) -> None:
    ops = db.open_ops()
    ops.execute("INSERT INTO contents(id,parent_id,pillar,persona,topic,popto_features,status,created_at)"
                " VALUES ('c_20261116_01',NULL,'A','保险','保险口播三段式','[\"跟创\"]','scripted',?)", (now_iso,))
    ops.execute("INSERT INTO contents(id,pillar,topic,status,created_at) VALUES ('c_old','B','上个月的','published','2026-10-01T09:00:00+08:00')")
    ops.execute("INSERT INTO publish_packs(id,content_id,platform,title,scheduled_at,publish_mode,status,created_at,updated_at)"
                " VALUES ('pk_c1_douyin','c_20261116_01','douyin','保险口播三段式','2026-11-10T19:00:00+08:00','semi_auto','approved',?,?)",
                (now_iso, now_iso))
    ops.execute("INSERT INTO actions(action_id,kind,ref,approved_at,status,created_at,updated_at)"
                " VALUES ('publish:pk_c1_douyin','publish','pk_c1_douyin',?,'awaiting_manual',?,?)", (now_iso, now_iso, now_iso))
    ops.execute("INSERT INTO posts(id,platform,platform_post_id,title,published_at) VALUES ('douyin_1','douyin','1','旧帖','2026-11-09T12:00:00+08:00')")
    ops.execute("INSERT INTO posts(id,platform,platform_post_id,title,published_at) VALUES ('xiaohongshu_2','xiaohongshu','2','更早的','2026-10-20T12:00:00+08:00')")
    ops.execute("INSERT INTO post_metrics(post_id,captured_at,checkpoint,views,likes) VALUES ('douyin_1','2026-11-09T13:00:00+08:00','1h',20,1)")
    ops.execute("INSERT INTO post_metrics(post_id,captured_at,checkpoint,views,likes) VALUES ('douyin_1',?,'24h',100,5)", (now_iso,))
    ops.execute("INSERT INTO post_metrics(post_id,captured_at,checkpoint,views,likes) VALUES ('xiaohongshu_2',?,'7d',900,5)", (now_iso,))
    for cp, due, done in (("1h", "2026-11-09T13:00:00+08:00", "skipped:2026-11-10T10:00:00+08:00"),
                          ("24h", "2026-11-10T12:00:00+08:00", now_iso), ("72h", "2026-11-12T12:00:00+08:00", None),
                          ("late", "2026-11-10T09:00:00+08:00", None)):
        ops.execute("INSERT INTO metric_jobs(post_id,checkpoint,due_at,done_at) VALUES ('douyin_1',?,?,?)", (cp, due, done))
    ops.execute("INSERT INTO comments(id,post_id,text,category,sentiment,reply_status,created_at,collected_at)"
                " VALUES ('douyin_1:9','douyin_1','能不能横屏','demand','neu','drafted','2026-11-01T08:00:00+08:00','2026-11-10T09:00:00+08:00')")
    ops.execute("INSERT INTO comments(id,post_id,text,created_at) VALUES ('douyin_1:8','douyin_1','老评论','2026-10-01T08:00:00+08:00')")
    ops.execute("INSERT INTO signals(id,source_type,quote,kind,module,intensity,demand_id,created_at)"
                " VALUES ('sg_1','comment',?,'功能需求','批量口播',2,'dm_1',?)", ("能不能横屏" * 60, now_iso))
    ops.execute("INSERT INTO signals(id,source_type,quote,kind,created_at) VALUES ('sg_2','listening','没归类','吐槽','2026-10-01T08:00:00+08:00')")
    ops.execute("INSERT INTO demands(id,title,score,status,persona_dist,prd_path,mr_links,updated_at)"
                " VALUES ('dm_1','横屏口播',3.0,'in_dev','{\"保险\": 2}','runs/t1/prd.md','https://g/x/merge_requests/7,https://g/x/merge_requests/9',?)", (now_iso,))
    ops.execute("INSERT INTO demands(id,title,score,status,updated_at) VALUES ('dm_2','已上线的',9.0,'done',?)", (now_iso,))
    ops.execute("INSERT INTO leads(id,platform,nickname,lead_type,score,stage,why,updated_at) VALUES ('ld_1','bilibili','某团队','team_lead',90,'已发卡',?,?)",
                ("</script><img src=x>", now_iso))
    ops.execute("INSERT INTO leads(id,platform,nickname,score,stage,updated_at) VALUES ('ld_2','douyin','早先的',50,'不跟进','2026-10-01T08:00:00+08:00')")
    ops.execute("INSERT INTO listening_terms(term,industry,active,hits_7d) VALUES ('数字人口播','产品',1,12)")
    for date, ch, clicks, reg, paid in (("2026-11-09", "dy01", 100, 10, 1), ("2026-11-09", "xhs01", 40, 12, 0),
                                        ("2026-11-10", "dy01", 50, 5, 2), ("2026-10-30", "dy01", 999, 99, 9)):
        ops.execute("INSERT INTO funnel_daily(date,channel_code,clicks,registrations,first_done,second_done,paid) VALUES (?,?,?,?,?,?,?)",
                    (date, ch, clicks, reg, reg // 2, 0, paid))
    ops.commit()
    ops.close()


def test_business_reflects_ops_db(home):
    at("2026-11-10 15:00")
    _seed(iso())
    d = bizviews.business()
    assert json.loads(json.dumps(d)) == d

    assert d["summary"] == {
        "contents_new": 1, "posts_published": 1, "views": 100, "comments_new": 1, "signals_new": 1, "leads_touched": 1,
        "clicks": 190, "registrations": 27, "paid": 3, "demands_open": 1, "manual_publish_waiting": 1, "metric_jobs_overdue": 1}
    daily = d["daily"]
    assert daily["posts"][-2:] == [1, 0] and daily["comments"][-1] == 1 and daily["contents"][-1] == 1
    assert daily["registrations"][-2:] == [22, 5] and sum(daily["registrations"]) == 27

    cal = d["content"]["calendar"]
    assert cal[0]["date"] == "2026-11-16" and cal[0]["items"][0]["features"] == ["跟创"]
    assert cal[0]["items"][0]["packs"] == [{"platform": "douyin", "status": "approved"}]
    assert d["content"]["total"] == 2 and d["content"]["undated"] == 1
    assert d["content"]["packs"][0]["action_status"] == "awaiting_manual"
    assert d["content"]["manual_publish"][0]["pack_id"] == "pk_c1_douyin" and d["content"]["manual_publish"][0]["platform"] == "douyin"
    assert {"label": "scripted", "n": 1} in d["content"]["status"] and d["content"]["pack_status"] == [{"label": "approved", "n": 1}]

    post = d["data"]["posts"][0]
    assert post["id"] == "douyin_1" and post["views"] == 100 and post["checkpoint"] == "24h"        # 最新一次快照
    assert {j["checkpoint"]: j["state"] for j in post["jobs"]} == {"1h": "skipped", "24h": "done", "72h": "pending", "late": "overdue"}
    assert d["data"]["jobs"] == {"done": 1, "pending": 1, "overdue": 1, "skipped": 1} and d["data"]["snapshots"] == 3
    assert [j["checkpoint"] for j in d["data"]["upcoming_jobs"]] == ["late", "72h"]
    assert d["data"]["comments"]["total"] == 2 and {"label": "未分拣", "n": 1} in d["data"]["comments"]["category"]
    assert [(m["checkpoint"], m["views"]) for m in post["series"]] == [("1h", 20), ("24h", 100)]      # 全部快照按时间排，画走势用
    assert post["comments_collected"] == 2
    rc = d["data"]["recent_comments"]
    assert len(rc) == 2 and rc[0]["post_id"] == "douyin_1" and rc[0]["platform"] == "douyin" and rc[-1]["text"] == "老评论"

    dm = d["demand"]["demands"]
    assert [x["id"] for x in dm] == ["dm_2", "dm_1"] and dm[1]["mr_links"] == ["https://g/x/merge_requests/7", "https://g/x/merge_requests/9"]
    assert dm[1]["persona_dist"] == {"保险": 2} and dm[1]["has_prd"] is True and "prd_path" not in dm[1]
    sig = d["demand"]["signals"]
    assert (sig["total"], sig["unassigned"]) == (2, 1) and len(sig["recent"][0]["quote"]) == 240    # 原话截短
    assert [x["quote"] for x in sig["unassigned_recent"]] == ["没归类"] and {x["label"] for x in sig["kind_group"]} == {"功能需求", "吐槽"}
    assert dm[1]["task"] is None and dm[1]["shelved_reason"] is None

    g = d["growth"]
    assert g["total"] == 2 and {"label": "已发卡", "n": 1} in g["stages"] and g["leads"][0]["id"] == "ld_1"
    assert g["leads"][0]["why"] == "</script><img src=x>"                                           # 原样给数据，转义是展示方的事
    assert g["terms"] == [{"term": "数字人口播", "category": "产品", "active": 1, "hits_7d": 12, "updated_at": None}]

    f = d["funnel"]
    assert [r["date"] for r in f["daily"]] == ["2026-11-10", "2026-11-09"] and f["daily"][1]["clicks"] == 140
    assert [(c["channel"], c["registrations"]) for c in f["channels"]] == [("dy01", 15), ("xhs01", 12)]
    assert f["totals"] == {"clicks": 190, "registrations": 27, "first_done": 13, "second_done": 0, "paid": 3}
    assert f["last_date"] == "2026-11-10"


def test_business_window_follows_days(home):
    at("2026-11-10 15:00")
    _seed(iso())
    one = bizviews.business(days=1)
    assert one["since"] == "2026-11-10T00:00:00+08:00" and one["daily"]["days"] == ["2026-11-10"]
    assert one["summary"]["posts_published"] == 0 and one["summary"]["registrations"] == 5
    assert one["content"]["total"] == 2                       # 存量不受窗口影响
    month = bizviews.business(days=30)
    assert month["summary"]["posts_published"] == 2 and month["summary"]["views"] == 1000
    assert month["summary"]["registrations"] == 126 and month["funnel"]["channels"][0]["channel"] == "dy01"
    assert bizviews.business(days=0)["days"] == 1


def test_business_does_not_write(home, data_dir):
    at("2026-11-10 15:00")
    _seed(iso())
    ops = db.open_ops()
    before = {t: ops.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("contents", "posts", "leads", "listening_terms")}
    bizviews.business()
    assert before == {t: ops.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}
    assert not (data_dir / "dashboard").exists()              # 不再生成静态页面


def test_decisions_reads_both_files(home):
    (home / "decisions.md").write_text("# 决定\n\n## D-20261109-01 视频号先不做自动发布\n- 原话：“x”\n- 日期：2026-11-09\n- 适用：发布\n",
                                       encoding="utf-8")
    (home / "not-doing.md").write_text("# 不做\n\n## N-20261110-01 海外版\n- 日期：2026-11-10\n- 回复口径：暂不考虑\n- 复查：2027-02-08\n",
                                       encoding="utf-8")
    got = bizviews.decisions()
    assert [(x["kind"], x["id"], x["title"], x["date"]) for x in got] == [
        ("不做的事", "N-20261110-01", "海外版", "2026-11-10"), ("定的事", "D-20261109-01", "视频号先不做自动发布", "2026-11-09")]
    assert got[0]["scope"] == "暂不考虑" and got[0]["review"] == "2027-02-08" and got[1]["scope"] == "发布"
