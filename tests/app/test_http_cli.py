"""管理 API、脚本契约（/v1/*）、钉钉来源。"""
import json
import urllib.error
import urllib.request

import pytest

from superops.core.errors import UserError
import yaml
from pathlib import Path

from superops.app import http, views


@pytest.fixture
def api(world):
    server = http.serve(world, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(method, path, body=None, headers=None):
        req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=20) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())
    yield call
    server.shutdown()


def test_script_contract_events_notify_counter_digest(world, api):
    assert api("GET", "/v1/health")[1]["ok"]
    status, result = api("POST", "/v1/events", {"source": "collector", "type": "social.metrics.anomaly", "subject": "post:p9", "summary": "异常",
                                                "dedup_key": "a1"})
    assert status == 200 and result["routed"] == "trigger"
    assert api("POST", "/v1/events", {"type": "social.metrics.anomaly", "subject": "post:p9", "dedup_key": "a1"})[1]["routed"] == "duplicate"
    world.pump()
    assert api("POST", "/v1/counter", {"name": "tikhub_requests", "value": 3})[1]["value"] == 3
    assert api("GET", "/v1/counter?name=tikhub_requests")[1]["value"] == 3
    assert api("POST", "/v1/notify", {"kind": "message", "title": "请手动发布", "body": "pk1", "ref": "m1", "dedup": True})[1]["ok"]
    assert api("POST", "/v1/notify", {"kind": "message", "title": "请手动发布", "body": "pk1", "ref": "m1", "dedup": True})[1].get("duplicate")
    digest = api("GET", "/v1/digest")[1]
    assert "异常复盘" in digest["tasks_by_rule"]


def test_panel_views_and_person_actions(world, api):
    world.schedule.fire("cron.publish_batch_card")
    world.pump()
    overview = api("GET", "/api/overview")[1]
    assert [r["code"] for r in overview["needs_person"]] == ["T1"] and overview["open"]["person"] == 1
    assert overview["needs_person"][0]["options"][1]["require_reason"]
    page = api("GET", "/api/tickets/T1")[1]
    assert page["decision"]["direct"] and page["messages"][0]["kind"] == "attention"
    status, error = api("POST", "/api/tickets/T1/answer", {"choice": "2"})
    assert status == 400 and error["code"] == "reasonRequired"
    assert api("POST", "/api/tickets/T1/answer", {"choice": "1"})[1]["ok"]
    world.pump()
    assert api("GET", "/api/overview")[1]["needs_person"] == []
    assert len(api("GET", "/api/tickets?filter=ended")[1]) == 3            # 卡片工单 + 两个发布工单
    assert api("GET", "/api/tickets/T99")[0] == 404
    assert any(s["id"] == "pipeline" and s["triggers_24h"] == 2 for s in api("GET", "/api/sources")[1])
    thread = api("GET", "/api/thread?key=schedule:cron.publish_batch_card")[1]
    assert thread["triggers"][0]["went"].startswith("接单")
    assert api("GET", "/api/search?q=T1")[1]["tickets"][0]["code"] == "T1"
    assert len(api("GET", "/api/settings")[1]["rules"]) == 8


def test_cross_site_writes_are_refused(world, api):
    status, _ = api("POST", "/api/mode", {"mode": "off"}, headers={"Origin": "http://evil.example"})
    assert status == 403 and world.settings.current()["eval"]["mode"] == "auto"
    assert api("POST", "/api/mode", {"mode": "observe"})[1]["mode"] == "observe"


def test_reply_to_ticket_from_panel_is_a_followup(world, api):
    world.cli.ask("MOCK_WAIT 盯一下数据")
    world.pump()
    assert api("POST", "/api/tickets/T1/reply", {"text": "可以收了"})[1]["result"] == "stored"
    world.pump()
    assert views.ticket_row(world, world.find_ticket("T1"))["label"] == "已完成"
    assert api("POST", "/api/tickets/T1/stop", {})[1]["label"] == "已完成"       # 已结束的工单再停没有效果



def test_triggers_can_be_filtered_to_what_people_said(world, api):
    """面板“一条指令的旅程”只要人发的触发：human=1 只回人发的，human=0 只回定时和流水线的。"""
    world.cli.ask("MOCK_WAIT 盯一下数据")
    world.schedule.fire("cron.publish_batch_card")
    world.pump()
    people = api("GET", "/api/triggers?human=1&limit=50")[1]
    machines = api("GET", "/api/triggers?human=0&limit=50")[1]
    assert people and all(t["human"] for t in people) and people[0]["text"] == "MOCK_WAIT 盯一下数据"
    assert machines and not any(t["human"] for t in machines)
    assert len(api("GET", "/api/triggers?limit=50")[1]) == len(people) + len(machines)

def dingtalk(world, text, sender="u1", mid="m1"):
    return world.dingtalk.handle({"event_type": "user_im_message_receive_group", "conversation_id": "chatA", "message_id": mid,
                                  "sender_open_dingtalk_id": sender, "text": text})


@pytest.fixture
def ding(world, monkeypatch):
    sent = []
    world.settings.current()
    settings = world.settings._file          # 改“文件里的策略”，这样运行时开关变化后重新合成时还在
    world.settings._cache = None
    settings["sources"]["dingtalk"] = {"enabled": True, "config": {"chats": ["chatA"], "observe": "mention"}}
    settings["policy"]["sources"]["dingtalk"] = {"allowed": [{"id": "u1", "name": "张三"}], "bots": [], "admission": {}, "answerers": {"batch-card": ["u2"]}}
    settings["home"] = {"source": "dingtalk", "thread": "home", "address": {"chat": "chatA"}}
    monkeypatch.setattr(world.dingtalk, "send", lambda message: sent.append(message) or "sent")
    world.sent = sent
    return world


def test_dingtalk_answer_followup_commands_and_requests(ding):
    rt = ding
    rt.schedule.fire("cron.publish_batch_card")
    rt.pump()
    assert rt.sent[0]["kind"] == "attention" and rt.sent[0]["channel"]["address"]["chat"] == "chatA"
    assert dingtalk(rt, "T1 1", sender="stranger", mid="m0") == {"denied": "decide"}
    assert dingtalk(rt, "T1 1", mid="m1")["ok"]
    assert dingtalk(rt, "T1 1", mid="m1") == {"duplicate": True}
    rt.pump()
    assert rt.find_ticket("T1")["answered"][0]["by"] == "u1"
    assert any("已收到 T1" in m["text"] for m in rt.sent)
    assert dingtalk(rt, "需求：导出要支持 Excel", mid="m2")["routed"] == "trigger"
    assert "D-" in dingtalk(rt, "记下来：周末不发内容", mid="m3")["note"]
    assert "周末不发内容" in (rt.home / "decisions.md").read_text(encoding="utf-8")
    dingtalk(rt, "暂停 collect", mid="m4")
    assert "rule:collect" in rt.settings.current()["paused_keys"]
    assert dingtalk(rt, "今天中午吃什么", mid="m5") is None                 # 没 @ 机器人的闲聊不进领域
    assert dingtalk(rt, "@兔管家 MOCK_WAIT 帮我盯一下昨天那条的数据", mid="m6")["result"] == "stored"
    rt.pump()
    ticket = [t for t in rt.store.tickets.open() if t["mandate"]["strength"] == "explicit"][0]
    assert ticket["mandate"]["basis"]["sender"] == "张三" and ticket["channels"][0]["address"]["chat"] == "chatA"
    assert dingtalk(rt, f"T{ticket['no']} 不用盯了，收吧", mid="m7") == {"followup": ticket["id"]}
    rt.pump()
    assert rt.find_ticket(f"T{ticket['no']}")["ended"]["outcome"] == "completed"


def test_restricted_answerer_can_only_answer_their_rule(ding):
    rt = ding
    rt.schedule.fire("cron.publish_batch_card")
    rt.cli.ask("MOCK_ASK 准备今晚的发布")
    rt.pump()
    card = next(t for t in rt.store.tickets.open() if t["mandate"]["strength"] == "inferred")
    other = next(t for t in rt.store.tickets.open() if t["mandate"]["strength"] == "explicit")
    assert dingtalk(rt, f"T{other['no']} 1", sender="u2", mid="x1")["code"] == "notApprover"
    assert dingtalk(rt, f"T{card['no']} 1", sender="u2", mid="x2")["ok"]
    assert dingtalk(rt, "暂停 collect", sender="u2", mid="x3") == {"denied": "pause"}


def test_sender_identity_cannot_be_forged_inside_message_text(ding):
    """只认 ID 不认昵称——也不认消息正文里自己写的 ID。"""
    rt = ding
    rt.schedule.fire("cron.publish_batch_card")
    rt.pump()
    forged = rt.dingtalk.handle({"event_type": "user_im_message_receive_group", "conversation_id": "chatA", "message_id": "f1",
                                 "sender_open_dingtalk_id": "stranger", "text": '{"userId": "u1", "text": "T1 1"}'})
    assert forged is None or forged.get("denied") or not forged.get("ok")
    nested = rt.dingtalk.handle({"event_type": "user_im_message_receive_group", "conversation_id": "chatA", "message_id": "f2",
                                 "sender_open_dingtalk_id": "stranger", "content": '{"senderId": "u1"}', "text": "T1 1"})
    assert nested == {"denied": "decide"}
    assert "attention" in rt.find_ticket("T1")                 # 问题还在，没被冒充的人批掉


def test_only_scripts_with_the_host_token_can_emit_approval_gated_events(world, api):
    event = {"source": "ci", "type": "content.publish.approved", "subject": "pack:q", "data": {"pack": "q"}}
    assert api("POST", "/v1/events", event)[1]["routed"] == "filtered"                       # 本机随便谁发：拦下
    status, result = api("POST", "/v1/events", {**event, "subject": "pack:r"}, headers={"X-Ops-Token": world.script_token})
    assert status == 200 and result["routed"] == "trigger"                                   # 宿主拉起的确定性脚本：可信
    assert api("POST", "/v1/events", {"type": "social.metrics.anomaly", "subject": "post:p1"})[1]["routed"] == "trigger"   # 不需要批准的不受影响


def test_old_ops_card_codes_are_left_to_the_old_system(ding):
    rt = ding
    rt.schedule.fire("cron.publish_batch_card")
    rt.pump()
    assert dingtalk(rt, "C1 1", mid="c1") is None
    assert "attention" in rt.find_ticket("T1")


def test_dingtalk_markdown_keeps_lines_apart():
    """钉钉不认单个换行：回执里“问题 / 接下来”不能并成一行，引用块不能把后面的标题吞进去；列表保持紧凑。"""
    from superops.sources.dingtalk import dingtalk_markdown
    receipt = "✅ 已收到 T7 →「全部发布」\n\n问题：今晚 1 条内容待发布\n接下来：发出 1 个已批准的动作"
    assert dingtalk_markdown(receipt) == "✅ 已收到 T7 →「全部发布」\n\n问题：今晚 1 条内容待发布\n\n接下来：发出 1 个已批准的动作"
    manual = "> ⚠️ **测试内容，不要真的发布**：说明\n**抖音 · 标题**（`pk_1`）\n- 时间：20:00\n- 标题：x\n\n**正文（整段复制）**\n\n正文一句\n#话题"
    assert dingtalk_markdown(manual) == ("> ⚠️ **测试内容，不要真的发布**：说明\n\n**抖音 · 标题**（`pk_1`）\n- 时间：20:00\n- 标题：x\n\n"
                                         "**正文（整段复制）**\n\n正文一句\n\n#话题")
    card = "**回复**（在群里回一行即可）：\n- `T7 1` → 全部发布\n- `T7 2 + 原因` → 全部不发\n到 22:00 没有回复，按「全部不发」处理。"
    assert dingtalk_markdown(card).endswith("- `T7 2 + 原因` → 全部不发\n\n到 22:00 没有回复，按「全部不发」处理。")
    assert dingtalk_markdown("```\na\nb\n```") == "```\na\nb\n```"



def test_手动采集_按作品开一张手动采集工单(world, monkeypatch):
    fired = []
    monkeypatch.setattr(world.schedule, "fire", lambda type_, due=None, data=None: fired.append((type_, data)) or {"id": "x"})
    assert http.handle_post(world, "/api/metrics/collect", {"post_ids": ["douyin_1", " "], "origin": "panel"})["id"] == "x"
    http.handle_post(world, "/api/metrics/collect", {})
    assert fired == [("cron.collect_metrics_now", {"post_ids": ["douyin_1"], "by": "panel"}),
                     ("cron.collect_metrics_now", {"post_ids": [], "by": "panel"})]
    rules = {r["id"]: r for r in yaml.safe_load((Path(__file__).parents[2] / "policy.yaml").read_text(encoding="utf-8"))["policy"]["rules"]}
    assert rules["collect-metrics-now"]["when"]["types"] == ["cron.collect_metrics_now"]
    assert rules["collect-metrics-now"]["run"]["command"][-1] == "now"


def test_手动排选题_带范围和方向(world, monkeypatch):
    fired = []
    monkeypatch.setattr(world.schedule, "fire", lambda type_, due=None, data=None: fired.append((type_, data)) or {"id": "x"})
    http.handle_post(world, "/api/topics/plan", {"range": "next_7_days", "note": " 多排小红书 ", "origin": "panel"})
    http.handle_post(world, "/api/topics/plan", {})
    assert fired == [("cron.topic_planning", {"manual": True, "by": "panel", "range": "next_7_days", "note": "多排小红书"}),
                     ("cron.topic_planning", {"manual": True, "by": "panel", "range": "next_week"})]
    with pytest.raises(UserError):
        http.handle_post(world, "/api/topics/plan", {"range": "明年"})


def test_监听意图词接口(world):
    r = http.handle_post(world, "/api/terms", {"action": "add", "term": "保险团队长怎么带新人", "category": "pain"})
    assert r["active"] == 1 and r["category"] == "pain"
    assert http.handle_post(world, "/api/terms", {"action": "disable", "term": "保险团队长怎么带新人"})["active"] == 0
    with pytest.raises(ValueError):
        http.handle_post(world, "/api/terms", {"action": "add", "term": ""})


def test_启动时端口被占_等上一个进程放掉再绑_别的错误直接抛(monkeypatch):
    import errno
    from superops import cli
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    calls = []

    def make():
        calls.append(1)
        if len(calls) < 3:
            raise OSError(errno.EADDRINUSE, "Address already in use")
        return "server"
    assert cli.bind_with_retry(make) == "server" and len(calls) == 3
    with pytest.raises(OSError):
        cli.bind_with_retry(lambda: (_ for _ in ()).throw(OSError(errno.EADDRINUSE, "busy")), tries=2)
    with pytest.raises(OSError):
        cli.bind_with_retry(lambda: (_ for _ in ()).throw(OSError(errno.EACCES, "denied")))


def test_卡片批准_同类型同主题的几条事件都发出去_只批几条时按原序号(world, monkeypatch):
    world.schedule.fire("cron.publish_batch_card")
    world.pump()
    ticket = world.store.tickets.by_no(1)
    sent = []
    monkeypatch.setattr(world.pipeline, "emit", lambda e, **k: sent.append(e) or {"routed": "trigger"})
    emit = [{"type": "demand.manual", "subject": "demand:competitor-review-20261112", "data": {"text": f"需求{i}"}} for i in range(1, 4)]
    world._attention_answered({"ticketId": ticket["id"], "attentionId": "att1", "option": {"key": "approve", "label": "进需求池", "emit": emit}})
    assert [e["data"]["text"] for e in sent] == ["需求1", "需求2", "需求3"] and len({e["dedup_key"] for e in sent}) == 3
    sent.clear()
    world.store.set("picked:att2", [3, 1])
    world._attention_answered({"ticketId": ticket["id"], "attentionId": "att2", "option": {"key": "approve", "label": "进需求池", "emit": emit}})
    assert [e["data"]["text"] for e in sent] == ["需求3", "需求1"] and sent[0]["dedup_key"].startswith("decision:att2:3:")


def test_事件命中进行中工单的规则时_真正交给那张工单(world, monkeypatch):
    # 同一批事件晚到几毫秒：工单在扇出之后才开出来，评估时只写“已送达”却没交给它（2026-11-12 两条需求因此丢失）
    live = {"id": "tk1", "no": 138, "mandate": {"basis": {"rules": ["anomaly"]}}, "assignments": [{"state": "running"}]}
    trig = {"id": "tr9", "human": False, "at": "2026-11-12T08:38:14Z", "source": "pipeline",
            "meta": {"type": "social.metrics.anomaly", "origin": "collector", "subject": "post:p1"}}
    candidate = {"evalId": "ev1", "triggers": [trig], "tickets": [live], "strength": "inferred", "thread": {"key": "pipeline:post:p1"}}
    a = world.assessor
    out = a.direct(candidate)
    assert out["reason"]["code"] == "continuesTicket" and out["_attach"] == "tk1"
    attached, decided = [], []
    monkeypatch.setattr(world.engine, "candidate", lambda tid: candidate)
    monkeypatch.setattr(world.engine, "candidate_delivered", lambda eid: None)
    monkeypatch.setattr(world.engine, "decide", lambda tid, o: decided.append(o) or {})
    monkeypatch.setattr(world.engine, "attach", lambda t, tid: attached.append((t, tid)))
    a._assess({"triggerId": "tr9"}, "ev1")
    assert decided[0]["kind"] == "ignore" and "_attach" not in decided[0] and attached == [("tk1", "tr9")]


def test_面板设计页的文档接口_只按名字给仓库里的文档(world):
    from superops.app.http import ApiError
    (world.home / "docs").mkdir(exist_ok=True)
    (world.home / "docs" / "整体设计.md").write_text("# 整体设计\n\n| 层 | 模块 |\n| --- | --- |\n| 引擎 | `core/` |\n", encoding="utf-8")
    (world.home / "docs" / "010-extra.md").write_text("# 010 新文档\n", encoding="utf-8")
    (world.home / "decisions.md").write_text("# 定的事\n", encoding="utf-8")
    (world.home / "prompts" / "team").mkdir(parents=True, exist_ok=True)
    (world.home / "prompts" / "team" / "scope.md").write_text("按影响决定做到哪\n", encoding="utf-8")
    (world.home / "skills" / "ops-x").mkdir(parents=True, exist_ok=True)
    (world.home / "skills" / "ops-x" / "SKILL.md").write_text("---\nname: ops-x\ndescription: 某系统 · 每日日报。每天发一条。\n---\n正文\n", encoding="utf-8")
    d = http.handle_get(world, "/api/docs", {})
    assert d["name"] == "design" and d["markdown"].startswith("# 整体设计") and d["group"] == "设计文档"
    listed = {x["name"]: x for x in d["docs"]}
    assert d["docs"][0]["name"] == "design"                                  # 整体设计置顶
    assert listed["extra"]["title"] == "010 新文档" and listed["extra"]["path"] == "docs/010-extra.md"   # docs/ 下新加的自动出现
    assert listed["decisions"]["group"] == "仓库规则"
    assert listed["prompt-team-scope"]["title"] == "团队 · 工单范围" and listed["skill-ops-x"]["title"] == "每日日报" and listed["skill-ops-x"]["group"] == "Skill"
    assert http.handle_get(world, "/api/docs", {"name": "extra"})["markdown"] == "# 010 新文档\n"
    for bad in ("../policy", "local.yaml", "flow", "docs/010-extra.md", "010-extra", "skills/ops-x/SKILL.md", "prompt-../shared"):     # 不接受路径；flow 这个临时仓库里没有
        with pytest.raises(ApiError):
            http.handle_get(world, "/api/docs", {"name": bad})


def test_仓库里的文档都进了设计页():
    from superops.app.http import panel_docs
    root = Path(__file__).parents[2]
    docs = panel_docs(root)
    assert {"design", "flow", "domain", "authorization", "execution", "sessions", "readme", "agents", "decisions", "not-doing",
            "prompt-shared", "prompt-team-scope", "skill-ops-daily-brief", "skill-ops-compliance-review-checklist"} <= set(docs)
    every = {p.relative_to(root).as_posix() for d in ("docs", "prompts", "skills") for p in (root / d).rglob("*.md")}
    assert {rel for _, rel, _ in docs.values()} >= every                       # 这三个目录下的文档一篇不漏
    assert docs["skill-ops-daily-brief"][0] == "每日运营日报" and docs["prompt-orchestrator"][2] == "提示词"
    assert all((root / rel).is_file() for _, rel, _ in docs.values())
