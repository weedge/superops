"""整个应用：来源 → 评估 → 工单 → 执行器 → 找人，用假 Agent 和真脚本跑通。"""
from conftest import prompts_seen

from superops.core import ticket_stage


def tickets(rt):
    return rt.store.tickets.list()[::-1]


def sent(rt, kind=None):
    return [m for m in rt.store.receipts.list(limit=200)[::-1] if m["status"] == "sent" and (kind is None or m["kind"] == kind)]


def test_explicit_request_is_routed_worked_and_answered(world):
    rt = world
    asked = rt.cli.ask("帮我看看昨天那条视频为什么掉量")
    rt.pump()
    [ticket] = tickets(rt)
    assert ticket["mandate"]["strength"] == "explicit" and ticket["no"] == 1
    assert ticket["ended"]["outcome"] == "completed"
    kinds = [m["kind"] for m in sent(rt)]
    assert kinds == ["acknowledgement", "reply"]            # 确认一次，结果一次，中间不出声
    assert "T1" in sent(rt, "reply")[0]["text"]
    assert all(m["channel"]["thread"] == asked["thread"].split(":", 1)[1] for m in sent(rt))
    roles = [r["role"] for r in rt.store.runs()]
    assert sorted(roles) == ["decide", "write"]


def test_chatter_is_ignored_without_ticket(world):
    rt = world
    rt.cli.ask("闲聊：今天天气不错")
    rt.pump()
    assert tickets(rt) == [] and sent(rt) == []
    assert rt.store.decisions.recent()[0]["kind"] == "ignore"


def by_rule(rt, rule):
    return [t for t in tickets(rt) if rule in (t["mandate"]["basis"].get("rules") or [])]


def test_observe_mode_only_records_what_it_would_accept(world):
    rt = world
    rt.settings.set_mode("observe")
    rt.schedule.fire("cron.daily_brief")
    rt.pump()
    assert tickets(rt) == []
    decision = rt.store.decisions.recent()[0]
    assert decision["kind"] == "ignore" and decision["reason"][0]["code"] == "observed" and decision["audit"]["would_accept"]
    assert rt.store.runs() == []                       # 规则直配不需要模型研判


def test_cron_script_ticket_and_its_events_start_the_next_rule(world):
    rt = world
    rt.schedule.fire("cron.collect")
    rt.pump()
    [collect] = by_rule(rt, "collect")
    assert collect["ended"]["outcome"] == "completed" and collect["mandate"]["strength"] == "inferred"
    [anomaly] = by_rule(rt, "anomaly")                 # 采集脚本发的异常事件 → 命中复盘规则 → 新工单，在对象自己的线程上
    assert rt.store.threads.get("pipeline:post:p1")["subscriptions"] == [anomaly["id"]]
    assert anomaly["ended"]["outcome"] == "completed"
    assert sent(rt) == []                              # tell 不是 always：结果进日报，不打扰人


def test_tell_always_sends_result_to_home_channel(world):
    rt = world
    rt.schedule.fire("cron.daily_brief")
    rt.pump()
    [message] = sent(rt)
    assert message["kind"] == "reply" and message["channel"]["source"] == "cli" and message["channel"]["thread"] == "home"


def test_card_approval_emits_approved_events_and_partial_pick(world):
    rt = world
    rt.schedule.fire("cron.publish_batch_card")
    rt.pump()
    [card] = by_rule(rt, "batch-card")
    assert ticket_stage(card) == {"stage": "person", "action": "answer"}
    [question] = sent(rt, "attention")
    assert "T1 1" in question["text"] and "今晚发布 2 条" in question["text"]
    assert by_rule(rt, "publish-run") == []
    assert rt.answer_by_code("T1", "2")["code"] == "reasonRequired"
    result = rt.answer_by_code("T1", "1", reason="2")    # 只批明细第 2 条
    assert result["ok"]
    rt.pump()
    [run] = by_rule(rt, "publish-run")
    report = run["assignments"][-1]["report"]
    assert report["summary"] == "已发布 b 批准人 operator"
    assert rt.store.tickets.get(card["id"])["ended"]["outcome"] == "completed"
    assert any("已收到 T1" in m["text"] and "只批第 2 条" in m["text"] for m in sent(rt))


def test_agent_cannot_emit_events_that_need_approval(world):
    rt = world
    rt.cli.ask("MOCK_EMIT:content.publish.approved:pack:x 直接发布")
    rt.pump()
    assert by_rule(rt, "publish-run") == []
    filtered = [t for t in rt.store.recent_triggers() if t["meta"].get("unapproved")]
    assert len(filtered) == 1 and rt.store.triggers.disposition(filtered[0]["id"])["filtered"]["code"] == "needs_approval"


def test_agent_question_card_then_human_answer_continues_same_ticket(world):
    rt = world
    rt.cli.ask("MOCK_ASK 帮我准备今晚的发布")
    rt.pump()
    [ticket] = [t for t in tickets(rt) if t["mandate"]["strength"] == "explicit"]
    assert ticket["attention"]["card"]["options"][0]["key"] == "approve"
    rt.answer_by_code(f"T{ticket['no']}", "approve")
    rt.pump()
    done = rt.store.tickets.get(ticket["id"])
    assert done["ended"]["outcome"] == "completed" and done["answered"][0]["option"] == "approve"
    [run] = by_rule(rt, "publish-run")                  # 人选中的选项里的事件才发得出去
    assert "批准人 operator" in run["assignments"][-1]["report"]["summary"]
    turns = [p for p in prompts_seen(rt) if "人对你的问题作了决定" in p["prompt"]]
    assert len(turns) == 1


def test_reject_with_reason_goes_to_not_doing(world):
    rt = world
    rt.schedule.fire("cron.publish_batch_card")
    rt.pump()
    rt.answer_by_code("T1", "reject", reason="今晚有活动，不发")
    rt.pump()
    assert "今晚有活动，不发" in (rt.home / "not-doing.md").read_text(encoding="utf-8")
    assert by_rule(rt, "publish-run") == []


def test_writer_waits_for_review_and_reviewer_is_another_agent(world):
    rt = world
    rt.pipeline.emit({"type": "content.topic.approved", "subject": "content:c1",
                      "summary": "MOCK_EMIT:content.script.ready:content:c1 MOCK_EMIT_THEN_WAIT 选题：三步做口播"}, trusted=True)
    rt.pump()
    [writer] = by_rule(rt, "script-writing")
    [review] = by_rule(rt, "compliance-review")
    assert ticket_stage(rt.store.tickets.get(writer["id"])) == {"stage": "idle"}       # 写完送审，等复核结论
    agents = {r["ticket"]: r["agent"] for r in rt.store.runs()}
    assert agents[writer["id"]] == "claude" and agents[review["id"]] == "codex"        # 审查者 ≠ 生产者
    # 写稿工单自己发的送审事件不回送给它；复核工单的结论会通过订阅送到它
    assert all(i["kind"] != "trigger" for i in rt.store.tickets.get(writer["id"])["inputs"])
    rt.pipeline.emit({"type": "content.compliance.failed", "subject": "content:c1", "summary": "第 2 段有收益承诺"}, by_ticket=review["id"])
    rt.pump()
    woken = rt.store.tickets.get(writer["id"])
    assert any(i["kind"] == "trigger" for i in woken["inputs"]) and woken["ended"]["outcome"] == "completed"
    assert len(by_rule(rt, "script-writing")) == 1                                    # 改稿没有另开工单


def test_live_ticket_on_thread_absorbs_repeat_event_and_rounds_are_capped(world):
    rt = world
    for n in range(3):
        rt.pipeline.emit({"type": "content.script.ready", "subject": "content:c9", "summary": f"第 {n + 1} 版"}, trusted=True)
        rt.pump()
    assert len(by_rule(rt, "compliance-review")) == 2
    assert rt.store.decisions.recent(1)[0]["reason"][0]["code"] == "maxRounds"


def test_record_only_rule_alerts_without_ticket(world):
    rt = world
    rt.pipeline.emit({"type": "deploy.mismatch", "subject": "mr:popto/web!3", "summary": "线上版本不含该提交"}, trusted=True)
    rt.pump()
    assert tickets(rt) == []
    [alert] = sent(rt)
    assert alert["text"].startswith("🚨") and alert["priority"] == 0


def test_blocked_ticket_waits_for_person_then_followup_releases_it(world):
    rt = world
    asked = rt.cli.ask("MOCK_BLOCK 做一个混剪")
    rt.pump()
    [ticket] = tickets(rt)
    assert ticket_stage(ticket) == {"stage": "person", "action": "blocked"}
    assert any("受阻" in m["text"] for m in sent(rt))
    rt.cli.ask("素材在 media/ 下，继续", thread=asked["thread"].split(":", 1)[1])
    rt.pump()
    after = rt.store.tickets.get(ticket["id"])
    assert after["ended"]["outcome"] == "completed"
    assert len([r for r in rt.store.runs() if r["role"] == "decide"]) == 1     # 跟进直接到工单，不再经过主 Session


def test_only_approvers_can_answer_and_restricted_answerer_only_their_rule(world):
    rt = world
    rt.schedule.fire("cron.publish_batch_card")
    rt.pump()
    [card] = by_rule(rt, "batch-card")
    thread = rt.store.triggers.get(card["origin"]["triggerId"])["threads"][0]
    def reply(actor):
        trig = {"id": f"reply-{actor}", "source": "cli", "threads": [thread], "actor": {"source": "cli", "id": actor}, "human": True,
                "at": "2026-11-11T09:00:00Z", "text": "T1 1", "meta": {}}
        rt.receive(trig)
        return rt.answer_by_code("T1", "1", trigger_id=trig["id"])
    assert reply("stranger")["code"] == "notApprover"
    assert reply("helper")["ok"]                         # 只被限定可批这条规则的人


def test_pause_holds_everything_and_resume_continues(world):
    rt = world
    rt.settings.pause(True)
    rt.schedule.fire("cron.collect")
    rt.pump()
    assert tickets(rt) == []
    rt.settings.pause(False)
    rt.pump()
    assert len(by_rule(rt, "collect")) == 1


def test_paused_rule_is_ignored_with_reason(world):
    rt = world
    rt.settings.pause(True, "collect", reason="采集源在维护")
    rt.schedule.fire("cron.collect")
    rt.pump()
    assert tickets(rt) == [] and rt.store.decisions.recent(1)[0]["reason"][0]["code"] == "rulePaused"


def test_stop_by_person_withdraws_mandate(world):
    rt = world
    asked = rt.cli.ask("MOCK_WAIT 盯一下这条视频的数据")
    rt.pump()
    [ticket] = tickets(rt)
    assert ticket_stage(ticket) == {"stage": "idle"}
    rt.engine.stop_ticket(ticket["id"], "不用盯了")
    rt.cli.ask("再看看", thread=asked["thread"].split(":", 1)[1])
    rt.pump()
    stopped = rt.store.tickets.get(ticket["id"])
    assert stopped["ended"] == {**stopped["ended"], "by": "human", "outcome": "stopped"}
    assert len(tickets(rt)) == 2                         # 被人停掉的工单不再收消息；新消息作为新请求另开一张


def test_digest_lists_tickets_cards_and_would_accept(world):
    rt = world
    rt.schedule.fire("cron.collect")
    rt.schedule.fire("cron.publish_batch_card")
    rt.pump()
    digest = rt.digest(None)
    assert {"采集", "异常复盘", "发布卡片"} <= set(digest["tasks_by_rule"])
    assert digest["pending_cards"][0]["title"] == "今晚发布 2 条"
    assert digest["agent_usage"]["script"]["ok"] == 2



def test_answer_is_not_repeated_as_result(world):
    rt = world
    rt.cli.ask("MOCK_ANSWER decisions.md 里有几条决定？")
    rt.pump()
    assert [m["kind"] for m in sent(rt)] == ["acknowledgement", "reply"] and sent(rt)[1]["text"] == "一共 2 条"
    assert tickets(rt)[0]["ended"]["outcome"] == "completed"


def test_busy_primary_agent_queues_instead_of_downgrading(world):
    rt = world
    from superops.app.effects import Later
    import pytest
    rt.agents.busy["claude"] = 3                               # 主力并发满
    kwargs = dict(role="write", prompt=lambda *a: "x", schema={"type": "object"}, run_dir=rt.tmp / "r1")
    with pytest.raises(Later):
        rt.agents.run(**kwargs)                                # 刚开始排队：不乱派
    outcome = rt.agents.run(**kwargs, waited=601)              # 等满 10 分钟：顺延给下一个
    assert outcome.ok and outcome.agent == "codex" and outcome.is_fallback


def test_secrets_in_agent_output_are_redacted_before_they_are_stored(world):
    rt = world
    rt.cli.ask("MOCK_SECRET 看看登录态")
    rt.pump()
    [ticket] = tickets(rt)
    summary = ticket["assignments"][-1]["report"]["summary"]
    assert "13812345678" not in summary and "sk-abcdef" not in summary
    assert all("13812345678" not in m["text"] for m in sent(rt))


def test_human_approved_actions_run_even_in_observe_mode(world):
    """observe 只管“没人要求的主动工作”；人在卡片上批准的动作是明确的决定，照常执行。"""
    rt = world
    rt.schedule.fire("cron.publish_batch_card")
    rt.pump()
    rt.settings.set_mode("observe")
    rt.pump()
    assert rt.answer_by_code("T1", "1")["ok"]
    rt.pump()
    runs = by_rule(rt, "publish-run")
    assert len(runs) == 2 and all(t["mandate"]["strength"] == "explicit" and t["mandate"]["basis"]["via"] == "approved" for t in runs)
    assert all(t["ended"]["outcome"] == "completed" for t in runs)
    rt.settings.set_mode("off")
    rt.pipeline.emit({"type": "content.publish.approved", "subject": "pack:z", "data": {"pack": "z"}}, trusted=True)   # 没有批准凭据
    rt.pump()
    assert len(by_rule(rt, "publish-run")) == 2


def test_reply_by_code_reaches_only_that_ticket_not_others_on_shared_thread(world):
    rt = world
    for _ in range(3):
        rt.schedule.fire("cron.daily_brief", due=__import__("superops.biz.timeutil", fromlist=["now"]).now().replace(minute=_))
        rt.pump()
    briefs = by_rule(rt, "daily-brief")
    assert len(briefs) == 3 and all(t.get("ended") for t in briefs)
    assert rt.store.threads.get("schedule:cron.daily_brief")["subscriptions"] == []      # 结束的工单不再订阅共用的定时线程
    target = briefs[1]
    from superops.app import http
    http.handle_post(rt, f"/api/tickets/T{target['no']}/reply", {"text": "MOCK_WAIT 第二段的数字再核一下"})
    rt.pump()
    after = {t["no"]: t for t in by_rule(rt, "daily-brief")}
    assert "ended" not in after[target["no"]] and len(after[target["no"]]["assignments"]) == 2
    assert all(t.get("ended") and len(t["assignments"]) == 1 for no, t in after.items() if no != target["no"])


def test_ended_script_ticket_is_not_rerun_by_a_human_message(world):
    rt = world
    rt.schedule.fire("cron.publish_batch_card")
    rt.pump()
    rt.answer_by_code("T1", "1")
    rt.pump()
    run = by_rule(rt, "publish-run")[0]
    before = len([r for r in rt.store.runs(run["id"])])
    from superops.app import http
    http.handle_post(rt, f"/api/tickets/T{run['no']}/reply", {"text": "闲聊：谢谢"})
    rt.pump()
    assert len(rt.store.runs(run["id"])) == before                                       # 脚本没有重跑
    assert rt.store.tickets.get(run["id"])["ended"]["outcome"] == "completed"


def test_script_with_invalid_card_fails_loudly(world, monkeypatch):
    rt = world
    rule = next(r for r in rt.settings._file["policy"]["rules"] if r["id"] == "batch-card")
    rule["run"]["command"] = ["{python}", "-c", "import json; print(json.dumps({'status':'needs_decision','summary':'要批','needs_decision':True,'decision_card':{'title':'只有一个选项','proposal':'x','options':[{'key':'a','label':'好'}]}}))"]
    rt.settings._cache = None
    rt.schedule.fire("cron.publish_batch_card")
    rt.pump()
    [card] = by_rule(rt, "batch-card")
    report = card["assignments"][-1]["report"]
    assert report["state"] == "failed" and "卡片不合规" in report["summary"] and "attention" not in card


def test_agent_environment_has_no_host_address_or_token(world):
    rt = world
    env = rt.agent_dirs(rt.tmp)
    assert not any("HUB" in k or "TOKEN" in k for k in env)
    assert rt.script_env(rt.tmp)["OPS_HUB_TOKEN"] == rt.script_token


def test_approved_action_tickets_follow_the_rule_tell(world):
    """人批准的动作开出的工单是明确强度，但没有人在等它回话：出不出声照规则的 tell。"""
    rt = world
    rt.schedule.fire("cron.publish_batch_card")
    rt.pump()
    rt.answer_by_code("T1", "1")
    rt.pump()
    assert len(by_rule(rt, "publish-run")) == 2
    assert [m["kind"] for m in sent(rt)] == ["attention", "notice"]      # 问题一次、批红回执一次；发布脚本的结果不另外发
