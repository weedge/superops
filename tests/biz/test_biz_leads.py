"""获客（移植自 ops 的 tests/test_leads.py）：监听 → 线索入库 → 今日线索卡片 → 批红 → 待跟进；
限定审批人、分群监听里属于业务模块的部分。"""
from __future__ import annotations

import json

from superops.biz import db, ingest
from superops.biz.executors import leads as leadmod


LEASE = {"task": {"id": "t_listen"}, "rule": {}, "events": []}


def _listen(n=3, platform="B站"):
    return {"leads": [{"platform": platform, "profile_url": f"https://space.bilibili.com/{i}", "nickname": f"号{i}",
                       "industry": "保险", "lead_type": "team_lead" if i == 2 else "creator", "followers": 20000 * (i + 1),
                       "score": 80 + i, "why": "有需求", "suggestion": "回一条具体建议，不带链接"} for i in range(n)]}


def _stages(ops):
    return dict(ops.execute("SELECT nickname, stage FROM leads").fetchall())


def _last_json(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_listening_with_new_leads_triggers_card_event(home):
    out = _listen()
    assert ingest.apply("ops-social-listening", out, LEASE)
    assert [e["type"] for e in out["follow_up_events"]] == ["lead.batch.ready"]
    ops = db.open_ops()
    assert {r[0] for r in ops.execute("SELECT platform FROM leads")} == {"bilibili"}  # 平台名归一，不会因写法不同重复入库
    # 同一批线索第二次被发现：不重复入库
    ingest.apply("ops-social-listening", _listen(platform="bilibili"), LEASE)
    assert ops.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 3


def test_no_leads_no_card_event(home):
    out = {"leads": [], "term_stats": []}
    ingest.apply("ops-social-listening", out, LEASE)
    assert not out.get("follow_up_events")


def test_card_lists_leads_team_lead_first_and_marks_them_carded(home, data_dir, monkeypatch, capsys):
    ingest.apply("ops-social-listening", _listen(), LEASE)
    monkeypatch.setenv("OPS_RUN_DIR", str(data_dir))
    leadmod.cmd_card()
    out = _last_json(capsys)
    card = out["decision_card"]
    assert out["needs_decision"] and card["kind"] == "lead_batch" and card["on_expire"] == "skip"
    assert card["items"][0]["title"].startswith("号2（团队长") and card["items"][0]["platform"] == "B站"
    comment, approve, skip = card["options"]
    assert len(comment["emit"]) == len(approve["emit"]) == len(card["items"]) == 3  # 一一对应，支持“T12 1 2,3”只批几条
    assert {e["type"] for e in comment["emit"]} == {"lead.comment.approved"} and "B站还不能自动发评论" not in card["items"][0]["note"]
    assert len(skip["emit"][0]["data"]["lead_ids"]) == 3
    assert "回一条具体建议" in (data_dir / "今日线索.md").read_text(encoding="utf-8")
    assert set(_stages(db.open_ops()).values()) == {"已发卡"}
    # 已经发过卡片的不再重复发
    leadmod.cmd_card()
    assert _last_json(capsys)["needs_decision"] is False


def test_card_caps_at_ten(home, capsys):
    ingest.apply("ops-social-listening", _listen(13), LEASE)
    leadmod.cmd_card()
    assert len(_last_json(capsys)["decision_card"]["items"]) == 10
    assert list(_stages(db.open_ops()).values()).count("发现") == 3


def test_decision_moves_leads_to_follow_or_skip(home, data_dir, monkeypatch, capsys):
    ingest.apply("ops-social-listening", _listen(), LEASE)
    leadmod.cmd_card()
    card = _last_json(capsys)["decision_card"]
    events = [dict(card["options"][0]["emit"][0], type="lead.followup.approved"),
              {"type": "lead.followup.dismissed", "data": {"lead_ids": [e["data"]["lead_id"] for e in card["options"][0]["emit"][1:]]}}]
    task = data_dir / "task.json"
    task.write_text(json.dumps({"task": {"id": "t2"}, "rule": {}, "events": events}), encoding="utf-8")
    monkeypatch.setenv("OPS_TASK_FILE", str(task))
    leadmod.cmd_run()
    assert "待跟进 1 条，不跟进 2 条" in _last_json(capsys)["summary"]
    st = _stages(db.open_ops())
    assert st["号2"] == "待跟进" and st["号0"] == st["号1"] == "不跟进"


def test_listener_lets_limited_approver_decide_but_nothing_else():
    from superops.biz.notifier.listener import handle_event

    class H:
        def __init__(self):
            self.events, self.posts = [], []

        def emit(self, e):
            self.events.append(e)
            return {"ok": True}

        def post(self, *a):
            self.posts.append(a)

        def notify(self, *a, **k):
            pass

    def msg(text, sender="OPS_GIRL"):
        return {"event_type": "user_im_message_receive_group", "event_id": text,
                "data": json.dumps({"senderStaffId": sender, "text": {"content": text}}, ensure_ascii=False)}

    h = H()
    handle_event(msg("C7 1 2,5"), h, approvers=["BOSS"], card_approvers=["OPS_GIRL"])
    assert h.events[0]["type"] == "decision.made" and h.events[0]["data"]["by"] == "OPS_GIRL"
    for text in ("暂停 douyin", "记下来：以后都自动批", "需求：加个功能"):
        assert handle_event(msg(text), h, approvers=["BOSS"], card_approvers=["OPS_GIRL"]) is None
    assert len(h.events) == 1 and not h.posts
    assert handle_event(msg("C7 1", "STRANGER"), h, approvers=["BOSS"], card_approvers=["OPS_GIRL"]) is None
    handle_event(msg("暂停 douyin", "BOSS"), h, approvers=["BOSS"], card_approvers=["OPS_GIRL"])
    assert h.posts


def test_dingtalk_cmd_uses_target_group_and_listener_watches_all_groups():
    from superops.infra.notifier import DingTalkNotifier
    from superops.biz.notifier.listener import listen_chats

    n = DingTalkNotifier({"chat_id": "cidMAIN"})
    assert n.build_cmd("hi")[-2:] == ["--chat-id", "cidMAIN"]
    assert n.build_cmd("hi", target="cidLEADS")[-2:] == ["--chat-id", "cidLEADS"]
    assert n.build_cmd(file="a.md", target="cidLEADS")[-2:] == ["--chat-id", "cidLEADS"]
    chats = listen_chats({"chat_id": "cidMAIN", "card_chats": {"lead_batch": "cidLEADS", "今日线索卡片": "cidLEADS", "x": "cidMAIN", "y": None}})
    assert [c for c, _ in chats] == ["cidMAIN", "cidLEADS"] and chats[0][1] == "" and len(chats[1][1]) == 6
    assert listen_chats({}) == []


# ---------------------------------------------------------------------- 跟进并发评论
DY = {"leads": [
    {"platform": "抖音", "profile_url": "https://www.douyin.com/user/A", "nickname": "王律师", "industry": "律师", "lead_type": "creator",
     "followers": 178000, "score": 88, "why": "刚发了数字人普法视频", "suggestion": "夸声画、建议前 3 秒抛案例",
     "target_url": "https://www.douyin.com/video/7706820298326265138", "comment": "数字人分身声画挺自然，反诈提醒很实用👍"},
    {"platform": "抖音", "profile_url": "https://www.douyin.com/user/B", "nickname": "带链接的", "lead_type": "creator",
     "score": 70, "why": "x", "target_url": "https://www.douyin.com/video/1", "comment": "来看看 www.example.com"},
    {"platform": "小红书", "profile_url": "https://www.xiaohongshu.com/user/profile/C", "nickname": "小红书号", "lead_type": "creator",
     "score": 60, "why": "x", "target_url": "https://www.xiaohongshu.com/explore/1", "comment": "写得真细"}]}


def _card(capsys, data_dir, monkeypatch):
    ingest.apply("ops-social-listening", json.loads(json.dumps(DY)), LEASE)
    monkeypatch.setenv("OPS_RUN_DIR", str(data_dir))
    leadmod.cmd_card()
    return _last_json(capsys)["decision_card"]


def test_卡片_明细就是要发的评论原文_发不了的写明原因(home, data_dir, monkeypatch, capsys):
    card = _card(capsys, data_dir, monkeypatch)
    assert [o["label"] for o in card["options"]] == ["跟进并发评论（可只选几条）", "只记待跟进（我自己去发）", "这批都不跟进"]
    notes = {i["title"].split("（")[0]: i["note"] for i in card["items"]}
    assert notes["王律师"].startswith("评论：「数字人分身声画挺自然，反诈提醒很实用👍」 作品：https://www.douyin.com/video/7706820298326265138")
    assert "链接或联系方式" in notes["带链接的"] and "小红书还不能自动发评论" in notes["小红书号"]
    assert "1 条能自动发" in card["proposal"] and card["on_expire"] == "skip"
    assert "评论原文：数字人分身" in (data_dir / "今日线索.md").read_text(encoding="utf-8")


def test_批了跟进并发评论_能发的发_发不了的记待跟进_不重发(home, data_dir, monkeypatch, capsys):
    from superops.biz.executors import comment as cm
    card = _card(capsys, data_dir, monkeypatch)
    posted = []
    monkeypatch.setattr(cm, "post", lambda platform, url, text, *, lead_id, headless=True:
                        posted.append((platform, url, text)) or cm.CommentResult(True, note="已发出（评论列表里看到了这条）"))
    monkeypatch.setattr(leadmod.time, "sleep", lambda s: None)
    task = data_dir / "task.json"
    task.write_text(json.dumps({"task": {"id": "t3"}, "rule": {}, "events": card["options"][0]["emit"]}), encoding="utf-8")
    monkeypatch.setenv("OPS_TASK_FILE", str(task))
    leadmod.cmd_comment()
    out = _last_json(capsys)
    assert posted == [("douyin", "https://www.douyin.com/video/7706820298326265138", "数字人分身声画挺自然，反诈提醒很实用👍")]
    assert out["status"] == "done" and "发出 1 条，2 条要人去发" in out["summary"] and "✅ 抖音 王律师" in out["message_markdown"]
    ops = db.open_ops()
    rows = {r[0]: (r[1], r[2]) for r in ops.execute("SELECT nickname, stage, comment_status FROM leads")}
    assert rows == {"王律师": ("已评论", "sent"), "带链接的": ("待跟进", "manual"), "小红书号": ("待跟进", "manual")}
    leadmod.cmd_comment()                                  # 同一张卡片的事件再来一次：已发的不重发
    assert len(posted) == 1
    # 监听又发现了同一个人：已经发过评论的，原文和作品不被改掉
    again = json.loads(json.dumps(DY))
    again["leads"][0]["comment"] = "换一句"
    ingest.apply("ops-social-listening", again, LEASE)
    assert ops.execute("SELECT comment_text FROM leads WHERE nickname='王律师'").fetchone()[0].startswith("数字人分身")


def test_点了发送没确认到的不重试_超过每日上限转人工(home, data_dir, monkeypatch, capsys):
    from superops.biz.executors import comment as cm
    card = _card(capsys, data_dir, monkeypatch)
    monkeypatch.setattr(cm, "post", lambda *a, **k: cm.CommentResult(False, note="点了发送，但 20 秒内没在评论列表里看到这条"))
    task = data_dir / "task.json"
    task.write_text(json.dumps({"task": {"id": "t4"}, "rule": {}, "events": card["options"][0]["emit"][:1]}), encoding="utf-8")
    monkeypatch.setenv("OPS_TASK_FILE", str(task))
    leadmod.cmd_comment()
    ops = db.open_ops()
    assert ops.execute("SELECT stage, comment_status FROM leads WHERE nickname='王律师'").fetchone()[:] == ("待跟进", "unconfirmed")
    monkeypatch.setattr(cm, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该再发")))
    leadmod.cmd_comment()
    ops.execute("UPDATE leads SET comment_status=NULL, stage='已发卡'")
    ops.commit()
    monkeypatch.setattr(leadmod, "comments_today", lambda ops: leadmod.DAILY_COMMENTS)
    leadmod.cmd_comment()
    assert "今天已经自动发了" in _last_json(capsys)["manual"][0]


def test_评论原文的硬检查():
    from superops.biz.executors import comment as cm
    assert cm.check_text("") and cm.check_text("x" * 101) and cm.check_text("加我微信聊") and cm.check_text("看 popto.cn")
    assert cm.check_text("数字人分身声画挺自然👍") is None
    assert not cm.post("bilibili", "u", "好", lead_id="l").supported
    assert "没有具体作品的链接" in cm.post("douyin", "https://www.douyin.com/user/A", "好", lead_id="l").note


# ---------------------------------------------------------------------- 监听意图词
def test_意图词_增删停用_删掉的不会被keywords同步回来(home):
    ops = db.open_ops()
    n = ingest.sync_terms(ops)
    assert n > 0 and ops.execute("SELECT COUNT(*) FROM listening_terms WHERE term='PopTo爆款兔'").fetchone()[0] == 1
    assert ingest.edit_term(ops, "add", "保险团队长怎么带新人", "pain")["active"] == 1
    assert ingest.edit_term(ops, "disable", "PopTo爆款兔")["active"] == 0
    assert ingest.edit_term(ops, "remove", "popto.cn")["removed_at"]
    ingest.sync_terms(ops)
    assert ops.execute("SELECT active, removed_at IS NOT NULL FROM listening_terms WHERE term='popto.cn'").fetchone()[:] == (0, 1)
    assert ingest.edit_term(ops, "add", "popto.cn")["removed_at"] is None                     # 再加一次就恢复
    for bad in (("add", " ", None), ("add", "x", "其他"), ("enable", "没有的词", None), ("oops", "PopTo爆款兔", None)):
        try:
            ingest.edit_term(ops, *bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    from superops.biz import bizviews
    assert "popto.cn" in {t["term"] for t in bizviews._growth_view(ops)["terms"]}


# ---------------------------------------------------------------------- 接口监听（TikHub）
def _fake_tikhub(monkeypatch, now_ts):
    from superops.biz.collectors import listening as ls
    old = now_ts - 3 * 86400

    class FakeTH:
        configured, key, requests = True, "k", 0

        def __init__(self, *a, **k):
            pass

        def get(self, path, **p):
            self.requests += 1
            if "search_notes" in path:
                return {"data": {"data": {"items": [
                    {"model_type": "note", "note": {"id": "x1", "title": "口播稿怎么写", "desc": "求方法", "timestamp": now_ts - 3600,
                                                    "liked_count": 5, "comments_count": 2, "collected_count": 1,
                                                    "user": {"nickname": "保险小王", "userid": "u1"}}},
                    {"model_type": "ad", "note": {}}]}}}
            if "kuaishou" in path:
                return {"data": {"mixFeeds": [
                    {"feed": {"photo_id": 1, "caption": "旧作品", "timestamp": old * 1000, "user_name": "a", "share_info": "userId=U&photoId=P1"}},
                    {"feed": {"photo_id": 2, "caption": "数字人口播新作", "timestamp": (now_ts - 60) * 1000, "user_name": "快手号",
                              "like_count": 3, "share_info": "userId=U2&photoId=P2"}}]}}
            raise AssertionError(path)

    def fake_post(th, path, body):
        th.requests += 1
        if "douyin" in path:
            return {"data": {"business_data": [{"data": {"aweme_info": {
                "aweme_id": "777", "desc": f"{body['keyword']} 的视频", "create_time": now_ts - 7200,
                "author": {"nickname": "律师A", "sec_uid": "SEC", "follower_count": 120000},
                "statistics": {"digg_count": 10, "comment_count": 1, "collect_count": 2}}}}]}}
        return {"data": {"items": [{"exportId": "export/V1", "title": "视频号作品", "pubTime": now_ts - 600,
                                    "source": {"title": "某视频号"}}]}}
    monkeypatch.setattr(ls, "_post", fake_post)
    monkeypatch.setattr(ls, "TikHub", FakeTH)
    return ls, FakeTH


def test_接口监听_四个平台统一字段_只留24小时_跨词去重(home, monkeypatch):
    from bizhelpers import at
    t = at("2026-11-12 14:00")
    ls, FakeTH = _fake_tikhub(monkeypatch, int(t.timestamp()))
    r = ls.collect([{"term": "数字人口播", "industry": "solution"}, {"term": "口播稿怎么写", "industry": "pain"}], FakeTH())
    hits = r["hits"]
    assert set(hits) == {"douyin:777", "xiaohongshu:x1", "wechat_channels:export/V1", "kuaishou:P2"}   # 快手的旧作品被滤掉
    dy = hits["douyin:777"]
    assert dy["fans"] == 120000 and dy["url"] == "https://www.douyin.com/video/777" and dy["profile_url"].endswith("/user/SEC")
    assert dy["terms"] == ["数字人口播", "口播稿怎么写"] and r["per_term"] == {"数字人口播": 4, "口播稿怎么写": 4}
    assert hits["kuaishou:P2"]["url"] == "https://www.kuaishou.com/short-video/P2" and r["requests"] == 8
    ops = db.open_ops()
    assert len(ls.store(ops, hits, "2026-11-12T14:01:00+08:00")) == 4
    assert ls.store(ops, hits, "2026-11-13T14:01:00+08:00") == []                 # 第二天又搜到：不再交给 Agent 重判
    ops.execute("INSERT INTO listening_terms(term, industry, active) VALUES ('数字人口播','solution',1), ('更新不动了','pain',1)")
    ls.update_term_hits(ops)
    assert dict(ops.execute("SELECT term, hits_7d FROM listening_terms").fetchall()) == {"数字人口播": 4, "更新不动了": 0}


def test_接口监听_脚本_入库后发事件交给Agent_预算用完就停(home, monkeypatch, capsys):
    from bizhelpers import FakeHub, at
    t = at("2026-11-12 14:00")
    ls, FakeTH = _fake_tikhub(monkeypatch, int(t.timestamp()))
    monkeypatch.setattr(ls, "hub_client", lambda: FakeHub())
    ops = db.open_ops()
    ingest.edit_term(ops, "add", "数字人口播", "solution")
    ops.execute("UPDATE listening_terms SET active=0 WHERE term<>'数字人口播'")
    ops.commit()
    ls.cmd_collect()
    out = _last_json(capsys)
    ev = out["follow_up_events"][0]
    assert ev["type"] == "social.mention.new" and ev["source"] == "collector" and ev["payload_ref"].startswith("ops.db://mentions?since=")
    assert "新 4 条" in out["summary"]
    from superops.biz.context import resolve_payload_ref
    rows, untrusted = resolve_payload_ref(ev["payload_ref"], ops)
    assert len(rows) == 4 and untrusted                                       # 帖子是不可信输入
    r = ls.collect([{"term": "a"}, {"term": "b"}], FakeTH(), budget_left=3)
    assert r["requests"] == 3 and "预算用完" in r["errors"][0]


def test_小红书线索出卡片前补粉丝数(home, monkeypatch, capsys):
    from superops.biz.collectors import tikhub
    calls = []

    class TH:
        configured = True

        def __init__(self, *a, **k):
            pass

        def get(self, path, **p):
            calls.append(p)
            return {"data": {"data": {"fans": 23000}}}
    monkeypatch.setattr(tikhub, "TikHub", TH)
    ingest.apply("ops-social-listening", {"leads": [{"platform": "小红书", "profile_url": "https://www.xiaohongshu.com/user/profile/u1",
                                                       "nickname": "保险小王", "why": "x", "followers": None}]}, LEASE)
    leadmod.cmd_card()
    assert calls == [{"user_id": "u1"}] and "2.3 万粉" in _last_json(capsys)["decision_card"]["items"][0]["title"]


def test_抖音按人群词找人_只要画像区间里的_取最近一条非置顶作品(home, monkeypatch):
    from bizhelpers import at
    from superops.biz.collectors import listening as ls
    t = at("2026-11-12 14:00")
    now_ts = int(t.timestamp())

    def user(sec, fans):
        return {"dynamic_patch": {"raw_data": json.dumps({"user_info": {"sec_uid": sec, "nickname": sec, "follower_count": fans}})}}

    def fake_post(th, path, body):
        th.requests += 1
        assert "fetch_user_search" in path
        return {"data": {"user_list": [user("BIG", 61054), user("SMALL", 2717), user("HUGE", 2000000), user("OLD", 30000), user("SEEN", 40000)]}}

    class TH:
        requests = 0

        def get(self, path, **p):
            self.requests += 1
            if p["sec_user_id"] == "OLD":
                return {"data": {"aweme_list": [{"aweme_id": "9", "create_time": now_ts - 30 * 86400}]}}
            return {"data": {"aweme_list": [{"aweme_id": "1", "is_top": 1, "create_time": now_ts - 200 * 86400},
                                            {"aweme_id": "2", "desc": "保险人怎么获客", "create_time": now_ts - 3600},
                                            {"aweme_id": "3", "create_time": now_ts - 86400}]}}
    monkeypatch.setattr(ls, "_post", fake_post)
    th = TH()
    r = ls.collect_people([{"term": "保险代理人", "industry": "persona"}, {"term": "口播稿怎么写", "industry": "pain"}], th,
                          ls.settings({}), known={"SEEN"})
    assert list(r["hits"]) == ["douyin:2"] and r["candidates"] == 2              # 小号、超大号、见过的都不要；OLD 一个月没发
    h = r["hits"]["douyin:2"]
    assert h["fans"] == 61054 and h["url"] == "https://www.douyin.com/video/2" and h["categories"] == ["persona"]
    assert r["per_term"] == {"保险代理人": 1}
    # 作品搜索跳过人群词
    seen = []
    monkeypatch.setitem(ls.SEARCH, "douyin", lambda th, kw: seen.append(kw) or [])
    ls.collect([{"term": "保险代理人", "industry": "persona"}, {"term": "口播稿怎么写", "industry": "pain"}], th, platforms=("douyin",))
    assert seen == ["口播稿怎么写"]


def test_小红书作者先补粉丝数_库里有的不再查_按互动排_有上限(home, monkeypatch):
    from bizhelpers import at
    from superops.biz.collectors import listening as ls
    at("2026-11-12 14:00")
    ops = db.open_ops()
    ops.execute("INSERT INTO mentions(id,platform,author_id,fans,collected_at) VALUES ('xiaohongshu:old','xiaohongshu','A',15000,'2026-11-10T14:00:00+08:00')")
    hits = {f"xiaohongshu:{i}": {"platform": "xiaohongshu", "author_id": a, "fans": None, "likes": lk, "collects": 0, "comments": 0}
            for i, (a, lk) in enumerate([("A", 1), ("B", 5), ("C", 50), ("C", 0)])}
    asked = []

    class TH:
        requests = 0

        def get(self, path, **p):
            asked.append(p["user_id"])
            return {"data": {"data": {"fans": 800}}}
    assert ls.enrich_fans(ops, hits, TH(), limit=1) == 1
    assert asked == ["C"]                                                     # A 用库里的，C 互动最高先查，B 超出上限
    assert [hits[f"xiaohongshu:{i}"]["fans"] for i in range(4)] == [15000, None, 800, 800]


def test_线索卡片_粉丝不足的个人号不上卡_团队长例外_评分统一百分制(home, data_dir, monkeypatch, capsys):
    ingest.apply("ops-social-listening", {"leads": [
        {"platform": "抖音", "profile_url": "https://www.douyin.com/user/a", "nickname": "大号", "why": "x", "followers": 60000, "score": 0.85},
        {"platform": "抖音", "profile_url": "https://www.douyin.com/user/b", "nickname": "小号", "why": "x", "followers": 300, "score": 60},
        {"platform": "抖音", "profile_url": "https://www.douyin.com/user/c", "nickname": "团队长", "lead_type": "team_lead", "why": "x",
         "followers": 800, "score": 70}], "signals": [{"quote": "口播稿总卡壳", "kind": "pain", "module": "写稿"}]}, LEASE)
    ops = db.open_ops()
    assert ops.execute("SELECT score FROM leads WHERE nickname='大号'").fetchone()[0] == 85
    assert ops.execute("SELECT COUNT(*) FROM signals WHERE quote='口播稿总卡壳'").fetchone()[0] == 1
    monkeypatch.setenv("OPS_RUN_DIR", str(data_dir))
    leadmod.cmd_card()
    out = _last_json(capsys)
    names = [i["title"].split("（")[0] for i in out["decision_card"]["items"]]
    assert names == ["团队长", "大号"] and "1 条粉丝数低于门槛" in out["summary"]
    assert _stages(ops)["小号"] == "粉丝不足"


def test_接口偶发出错重试一次(home, monkeypatch):
    from superops.biz.collectors import listening as ls
    calls = []

    def flaky(th, kw):
        calls.append(kw)
        if len(calls) == 1:
            raise RuntimeError("HTTP Error 400: Bad Request")
        return []
    monkeypatch.setitem(ls.SEARCH, "douyin", flaky)
    monkeypatch.setattr(ls.time, "sleep", lambda s: None)
    r = ls.collect([{"term": "a", "industry": "pain"}], type("T", (), {"requests": 0})(), platforms=("douyin",))
    assert calls == ["a", "a"] and r["errors"] == []


# ---------------------------------------------------------------------- 竞品评论 → 需求评审
def test_竞品评论采集_挑评论最多的作品_短评论和重复的不要_入库后发事件(home, monkeypatch, capsys):
    from bizhelpers import FakeHub, at
    from superops.biz.collectors import competitors as cp
    at("2026-11-12 15:00")
    asked = []

    class TH:
        configured, requests = True, 0

        def __init__(self, *a, **k):
            pass

        def douyin_comments(self, aid, count=50):
            self.requests += 1
            asked.append(("dy", aid))
            return [{"id": "c1", "text": "能不能用自己的声音克隆？", "likes": 3, "created_at": 1794400000},
                    {"id": "c2", "text": "求链接", "likes": 0, "created_at": 1794400000},
                    {"id": "c1", "text": "能不能用自己的声音克隆？", "likes": 3, "created_at": 1794400000}], 0, False

        def get(self, path, **p):
            self.requests += 1
            if "search_notes" in path:
                return {"data": {"data": {"items": [{"model_type": "note", "note": {"id": f"n{i}", "title": f"蝉镜笔记{i}", "comments_count": i,
                                                                                    "user": {"nickname": "x"}}} for i in range(5)]}}}
            asked.append(("xhs", p["note_id"]))
            return {"data": {"data": {"comments": [{"id": f"x{p['note_id']}", "content": "口型对不上，价格也贵", "like_count": 1, "time": 1794400000}]}}}

    def fake_post(th, path, body):
        th.requests += 1
        return {"data": {"business_data": [{"data": {"aweme_info": {"aweme_id": "a1", "desc": "蝉镜教程",
                                                                     "statistics": {"comment_count": 9}}}},
                                           {"data": {"aweme_info": {"aweme_id": "a0", "desc": "蝉镜，没评论", "statistics": {"comment_count": 0}}}},
                                           {"data": {"aweme_info": {"aweme_id": "z9", "desc": "无关的猫咪视频", "statistics": {"comment_count": 99}}}}]}}
    monkeypatch.setattr(cp, "TikHub", TH)
    monkeypatch.setattr(cp, "_post", fake_post)
    monkeypatch.setattr(cp, "hub_client", lambda: FakeHub())
    ops = db.open_ops()
    ingest.sync_terms(ops)
    ops.execute("UPDATE listening_terms SET active=0 WHERE industry='competitor' AND term<>'蝉镜数字人'")
    ops.commit()
    cp.mark_research()                                                                  # 调研没到期：这次只采作品和评论
    cp.cmd_collect()
    out = _last_json(capsys)
    assert asked == [("dy", "a1"), ("xhs", "n4"), ("xhs", "n3"), ("xhs", "n2")]           # 没评论的不拉；小红书取评论最多的 3 条
    rows = {r[0]: r[1] for r in ops.execute("SELECT id, text FROM competitor_comments")}
    assert rows["douyin:c1"] == "能不能用自己的声音克隆？" and "douyin:c2" not in rows and len(rows) == 4
    assert ops.execute("SELECT COUNT(*) FROM competitor_works").fetchone()[0] == 5      # 没评论的作品也记下（调研要看）
    assert [e["type"] for e in out["follow_up_events"]] == ["competitor.comments.batch"]
    ev = out["follow_up_events"][0]
    assert ev["type"] == "competitor.comments.batch" and ev["payload_ref"].startswith("ops.db://competitor_comments?since=")
    from superops.biz.context import resolve_payload_ref
    data, untrusted = resolve_payload_ref(ev["payload_ref"], ops)
    assert len(data) == 4 and untrusted
    cp.cmd_collect()                                                                    # 同一批评论再采到：不再发事件
    assert _last_json(capsys)["follow_up_events"] == []


def test_竞品评审规则和Skill接得上():
    import yaml
    from pathlib import Path
    root = Path(__file__).parents[2]
    rules = {r["id"]: r for r in yaml.safe_load((root / "policy.yaml").read_text(encoding="utf-8"))["policy"]["rules"]}
    assert rules["competitor-review"]["when"]["types"] == ["competitor.comments.batch"]
    assert rules["competitor-review"]["run"]["tools"] == "readonly"
    assert rules["demand-manual"]["when"]["types"] == ["demand.manual"]                  # 评审卡片批准后发的事件有人接
    assert "demand.manual" in (root / "skills/ops-competitor-review/SKILL.md").read_text(encoding="utf-8")


def test_评论跨工单也按间隔排队_发不了的不算失败(home, data_dir, monkeypatch, capsys):
    from bizhelpers import at
    from superops.biz.executors import comment as cm
    at("2026-11-12 14:51")
    card = _card(capsys, data_dir, monkeypatch)
    assert len({e["subject"] for e in card["options"][0]["emit"]}) == 1          # 一张卡片的评论进同一张工单
    ops = db.open_ops()
    ops.execute("UPDATE leads SET comment_status='sent', commented_at='2026-11-12T14:50:50+08:00' WHERE nickname='带链接的'")
    ops.commit()
    slept = []
    monkeypatch.setattr(leadmod.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(leadmod.random, "uniform", lambda a, b: 30)
    monkeypatch.setattr(cm, "post", lambda *a, **k: cm.CommentResult(True, note="已发出"))
    task = data_dir / "task.json"
    task.write_text(json.dumps({"task": {"id": "t5"}, "rule": {}, "events": card["options"][0]["emit"]}), encoding="utf-8")
    monkeypatch.setenv("OPS_TASK_FILE", str(task))
    leadmod.cmd_comment()
    out = _last_json(capsys)
    assert slept == [20.0]                                  # 上一条（别的工单发的）在 10 秒前：再等 20 秒凑够 30 秒
    assert out["status"] == "done" and "发出 1 条，1 条要人去发" in out["summary"]   # 小红书那条发不了，但不算失败



def test_竞品账号_粉丝数解析_官方认证排前面():
    from superops.biz.collectors import competitors as cp
    assert cp._fans_text("Fans 40.4k") == 40400 and cp._fans_text("粉丝 1.2万") == 12000 and cp._fans_text("") is None

    class TH:
        def get(self, path, **p):
            return {"data": {"data": {"users": [{"id": "a", "name": "蝉镜AI", "sub_title": "Fans 218"},
                                                {"id": "b", "name": "蝉镜", "sub_title": "Fans 40.4k", "red_official_verified": True},
                                                {"id": "c", "name": "蝉镜粉丝", "sub_title": "Fans 90k"}]}}}
    acc = cp.xhs_accounts(TH(), "蝉镜")
    assert [a["account_id"] for a in acc] == ["b", "c", "a"] and acc[0]["verified"] == "官方认证" and acc[0]["fans"] == 40400


def test_竞品调研_立即发起_采账号和作品_不拉评论(home, monkeypatch, capsys):
    from bizhelpers import FakeHub, at
    from superops.biz.collectors import competitors as cp
    at("2026-11-12 16:00")
    monkeypatch.setattr(cp, "hub_client", lambda: FakeHub())

    class TH:
        configured, requests = True, 0

        def __init__(self, *a, **k):
            pass

        def get(self, path, **p):
            self.requests += 1
            if "search_users" in path:
                return {"data": {"data": {"users": [{"id": "x", "name": "闪剪", "sub_title": "Fans 1.2万", "red_official_verified": True}]}}}
            if "search_notes" in path:
                return {"data": {"data": {"items": [{"model_type": "note", "note": {"id": "n1", "title": "闪剪教程", "comments_count": 5,
                                                                                    "user": {"nickname": "闪剪官方"}}}]}}}
            raise AssertionError(f"调研不该拉评论：{path}")

        def douyin_comments(self, *a, **k):
            raise AssertionError("调研不该拉评论")

    def fake_post(th, path, body):
        th.requests += 1
        if "user_search" in path:
            return {"data": {"user_list": [{"user_info": {"sec_uid": "S", "nickname": "闪剪", "follower_count": 300000,
                                                          "enterprise_verify_reason": "某某科技有限公司"}}]}}
        return {"data": {"business_data": [{"data": {"aweme_info": {"aweme_id": "v1", "desc": "闪剪做数字人", "author": {"nickname": "A"},
                                                                     "statistics": {"comment_count": 3, "digg_count": 9}}}}]}}
    monkeypatch.setattr(cp, "TikHub", TH)
    monkeypatch.setattr(cp, "_post", fake_post)
    ops = db.open_ops()
    ingest.edit_term(ops, "add", "闪剪", "competitor")
    ops.execute("UPDATE listening_terms SET active=0 WHERE industry='competitor' AND term<>'闪剪'")
    ops.commit()
    cp.run("research")
    out = _last_json(capsys)
    ev = out["follow_up_events"]
    assert [e["type"] for e in ev] == ["competitor.data.ready"] and ev[0]["payload_ref"].startswith("ops.db://competitor_works?since=")
    assert {a["platform"] for a in ev[0]["data"]["accounts"]} == {"douyin", "xiaohongshu"}
    assert ops.execute("SELECT name, fans, verified FROM competitor_accounts WHERE platform='douyin'").fetchone()[:] == ("闪剪", 300000, "某某科技有限公司")
    assert not cp.research_due() and "已发起竞品调研" in out["summary"]


def test_竞品评审卡片_宿主补齐来源作品(home, data_dir):
    from superops.infra import paths
    work = paths.ops_data() / "work" / "t123-competitor-review"
    work.mkdir(parents=True)
    q1 = {"text": "定制的数字人能免费预览吗", "platform": "xiaohongshu", "competitor": "蝉镜数字人", "author": "某博主",
          "title": "如何用数字人5分钟做口播", "url": "https://www.xiaohongshu.com/explore/n1"}
    q2 = {"text": "试了5次都失败", "platform": "douyin", "competitor": "一键追爆款", "author": "B", "title": "一键追爆款教程",
          "url": "https://www.douyin.com/video/9"}
    card = {"title": "竞品评论里挖到 2 条候选需求", "items": [{"title": "试用预览", "note": "原话「能免费预览吗」"},
                                                             {"title": "失败说明", "note": f"原话「失败」— {q2['url']}"}], "options": []}
    out = {"candidates": [{"title": "试用预览", "evidence": {"count": 20, "quotes": [q1]}},
                          {"title": "失败说明", "evidence": {"count": 5, "quotes": [q2, q1]}}], "decision_card": card}
    ingest.competitor_review(None, out, {"task": {"id": "t123-competitor-review"}})
    assert card["items"][0]["note"].endswith("来源：小红书 @某博主《如何用数字人5分钟做口播》https://www.xiaohongshu.com/explore/n1")
    assert card["items"][1]["note"] == f"原话「失败」— {q2['url']}"                          # 已经带了链接的不重复加
    assert card["links"] == [q1["url"], q2["url"]] and "证据来自 2 条竞品作品" in card["reasons"][-1]
    md = (work / "来源作品.md").read_text(encoding="utf-8")
    assert "| 蝉镜数字人 | 小红书 | 某博主 | 如何用数字人5分钟做口播 |" in md and "1. 定制的数字人能免费预览吗<br>2. 定制的数字人能免费预览吗" in md



def test_竞品名_去掉通用词_无关的账号和作品不要():
    from superops.biz.collectors import competitors as cp
    assert cp.core_name("蝉镜数字人") == "蝉镜" and cp.core_name("腾讯智影数字人") == "腾讯智影" and cp.core_name("闪剪") == "闪剪"
    assert cp.core_name("AI数字人") == "AI数字人"                       # 去完太短就用原词
    assert cp.relevant("蝉镜（教师版）", "蝉镜数字人") and not cp.relevant("蝉镜粉丝站".replace("蝉镜", "禅静"), "蝉镜数字人")
    assert cp.relevant("用闪剪做口播 @小王", "闪剪") and not cp.relevant("闪光灯剪辑", "闪剪")
