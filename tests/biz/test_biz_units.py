"""业务模块的单元测试（移植自 ops 的 tests/test_units.py）：钉钉指令解析、合并闸门、卡片渲染、
指标采集、Agent 产出入库、发布执行器、创作者后台取数。"""
import json

from superops.biz import db, ingest
from superops.biz.timeutil import iso
from superops.biz.ci import gate
from superops.biz.collectors import metrics
from superops.biz.notifier.listener import handle_event, parse_command
from superops.biz.notifier.render import render_card

from bizhelpers import FakeHub, at


def test_parse_commands():
    assert parse_command("C12 1")["data"] == {"card": "C12", "choice": "1", "reason": None}
    assert parse_command("批 c7 reject 这周没人手")["data"]["reason"] == "这周没人手"
    assert parse_command("@兔管家 C3 approve_all")["data"]["choice"] == "approve_all"
    assert parse_command("记下来：视频号先不做自动发布")["type"] == "decision.note"
    assert parse_command("需求: 批量口播要支持横屏")["type"] == "demand.manual"
    assert parse_command("已发 pk_c1_douyin https://v.douyin.com/abc/")["data"]["pack_id"] == "pk_c1_douyin"
    assert parse_command("暂停 douyin")["type"] == "_pause"
    assert parse_command("今天天气不错") is None


def test_listener_only_accepts_approvers():
    raw = {"event_type": "user_im_message_receive_group", "event_id": "e1",
           "data": '{"senderStaffId": "u1", "text": {"content": "C5 1"}}'}
    h = FakeHub()
    assert handle_event(raw, h, approvers=["someone_else"]) is None and not h.events
    handle_event(raw, h, approvers=["u1"])
    assert h.events[0]["type"] == "decision.made" and h.events[0]["data"]["by"] == "u1"
    assert h.events[0]["dedup_key"] == "dingtalk:e1"


def test_listener_card_callback():
    raw = {"event_type": "user_card_action_triggered", "payload": {"body": {
        "operatorUserId": "u1", "actionData": {"context": {"card_id": "card_20261110_0003", "choice": "approve"}}}}}
    h = FakeHub()
    handle_event(raw, h, approvers=["u1"])
    assert h.events[0]["data"]["card"] == "card_20261110_0003" and h.events[0]["data"]["choice"] == "approve"


def test_gate_checks():
    changes = [
        {"new_path": "db/migrations/0012.sql", "diff": "+ALTER TABLE users DROP COLUMN phone;\n"},
        {"new_path": "src/billing/credits.go", "diff": "+x := 1\n"},
        {"new_path": "src/config.ts", "diff": "+const key = 'sk-abcdefghijklmnopqrstuvwxyz123456'\n"},
    ]
    g = gate.check(changes)
    assert any("迁移" in x for x in g.needs_human) and any("计费" in x for x in g.needs_human)
    assert any("API Key" in x for x in g.blockers) and not g.auto_merge_ok
    assert gate.check([{"new_path": "src/a.ts", "diff": "+const a = 1\n"}]).auto_merge_ok
    big = [{"new_path": "src/a.ts", "diff": "+x\n" * 801}]
    assert not gate.check(big).auto_merge_ok


def test_render_card():
    title, body = render_card({"kind": "publish_batch", "title": "今晚 6 条", "proposal": "全部发布",
                               "options": [{"key": "a", "label": "全部发布"}, {"key": "b", "label": "全部不发", "require_reason": True}],
                               "on_expire": "b"}, 17, "2026-11-10T22:00:00+08:00")
    assert "C17" in title and "【待发布】" in title and "11-10 22:00 前未处理则「全部不发」" in body


def test_metrics_plan_and_anomaly(home):
    ops = db.open_ops()
    at("2026-11-10 10:00")
    for i in range(6):
        pid = metrics.register_post(ops, {"platform": "douyin", "platform_post_id": str(i), "title": f"t{i}",
                                          "published_at": "2026-11-08T10:00:00+08:00"})
        metrics.save_snapshot(ops, {"id": pid, "platform": "douyin", "title": f"t{i}"}, "24h", {"views": 1000}, "test")
    pid = metrics.register_post(ops, {"platform": "douyin", "platform_post_id": "hot", "title": "爆了"})
    evs = metrics.save_snapshot(ops, {"id": pid, "platform": "douyin", "title": "爆了"}, "24h", {"views": 5000}, "test")
    assert any(e["type"] == "social.metrics.anomaly" and "高于" in e["summary"] for e in evs)
    jobs = ops.execute("SELECT checkpoint, due_at FROM metric_jobs WHERE post_id=? ORDER BY due_at", (pid,)).fetchall()
    assert [j[0] for j in jobs] == ["1h", "24h", "72h", "7d"]


def test_ingest_comment_and_demand(home):
    ops = db.open_ops()
    ops.execute("INSERT INTO comments(id,post_id,text) VALUES ('douyin_1:9','douyin_1','能不能横屏')")
    lease = {"task": {"id": "t1"}}
    ingest.apply("ops-comment-triage", {"comments": [{"id": "9", "category": "demand", "sentiment": "neu", "reply_draft": "在做了"}],
                                        "signals": [{"quote": "能不能横屏", "kind": "功能需求", "module": "批量口播", "intensity": 2}]}, lease)
    row = ops.execute("SELECT category, reply_status FROM comments").fetchone()
    assert tuple(row) == ("demand", "drafted")
    ingest.apply("ops-demand-mining", {"assignments": [{"quote": "能不能横屏", "kind": "功能需求", "module": "批量口播",
                                                        "intensity": 2, "new_demand": {"title": "批量口播支持横屏", "size": "S"}}]}, lease)
    d = ops.execute("SELECT title, signal_count FROM demands").fetchone()
    assert d[0] == "批量口播支持横屏" and d[1] >= 1


def test_选题_改方向后换掉的和点名不做的标成搁置(home):
    ops = db.open_ops()
    lease = {"task": {"id": "t1-topic-planning"}}
    cal = lambda *ids: [{"content_id": i, "title": i, "pillar": "A", "platforms": ["小红书"]} for i in ids]
    # 第一轮：排了 3 条，等人批
    ingest.apply("ops-topic-planning", {"calendar": cal("c_20261116_01", "c_20261117_01", "c_20261118_01"), "report_state": "waiting"}, lease)
    # 改方向后只出一条，另一条点名不做，工单结束
    ingest.apply("ops-topic-planning", {"calendar": cal("c_20261111_01"), "shelved": ["c_20261118_01"], "report_state": "ready"}, lease)
    st = dict(ops.execute("SELECT id, status FROM contents").fetchall())
    assert st == {"c_20261116_01": "shelved", "c_20261117_01": "shelved", "c_20261118_01": "shelved", "c_20261111_01": "proposed"}
    # 别的工单的待批选题、已写稿的不动
    ops.execute("INSERT INTO contents(id,status,task) VALUES ('c_20261120_01','proposed','t9'), ('c_20261121_01','scripted','t1-topic-planning')")
    ingest.apply("ops-topic-planning", {"calendar": [], "shelved": ["c_20261121_01"], "report_state": "ready"}, lease)
    st = dict(ops.execute("SELECT id, status FROM contents").fetchall())
    assert st["c_20261120_01"] == "proposed" and st["c_20261121_01"] == "scripted"


def test_sync_terms(home):
    ops = db.open_ops()
    assert ingest.sync_terms(ops) > 5
    assert ingest.sync_terms(ops) == 0


def test_dingtalk_message_id_and_failure(monkeypatch):
    import subprocess as sp
    from superops.biz.notifier import DingTalkNotifier
    n = DingTalkNotifier({"chat_id": "cid1"})
    out = {"stdout": '{"success": true, "result": {"data": {"openMsgId": "msgX"}}}'}
    monkeypatch.setattr(sp, "run", lambda *a, **k: sp.CompletedProcess(a, 0, out["stdout"], ""))
    assert n.send("notice", "t", "b") == {"ok": True, "message_id": "msgX"}
    out["stdout"] = '{"success": false, "errorMsg": "no permission"}'
    assert n.send("notice", "t", "b")["ok"] is False


def test_listener_flatten_group_message():
    # dws event consume --flatten 的真实字段形状（ID 已替换）
    raw = {"type": "user_im_message_receive_group", "event_id": "ev1", "message_id": "msg1",
           "conversation_id": "cid1", "sender": "审批人", "sender_open_dingtalk_id": "ODT_APPROVER",
           "content": "C5 2 先不做", "create_time": "2026-11-10 11:39:30"}
    h = FakeHub()
    handle_event(raw, h, approvers=["ODT_APPROVER"])
    assert h.events[0]["data"] == {"card": "C5", "choice": "2", "reason": "先不做", "by": "ODT_APPROVER"}
    # 昵称冒充审批人 ID 不算数
    fake = dict(raw, event_id="ev2", sender="ODT_APPROVER", sender_open_dingtalk_id="ODT_OTHER")
    h2 = FakeHub()
    assert handle_event(fake, h2, approvers=["ODT_APPROVER"]) is None and not h2.events


def test_gitlab_project_normalize():
    from superops.biz.ci.gitlab import GitLab
    want = "group%2Fsub-group%2Fproduct%2Fpopto"
    for p in ("/group/sub-group/product/popto", "group/sub-group/product/popto/",
              "https://gitlab.example.com/group/sub-group/product/popto.git"):
        assert GitLab.pid(p) == want
    assert GitLab.pid(123) == "123"


def test_gitlab_add_labels_compatible_with_11x():
    from superops.biz.ci.gitlab import GitLab
    gl = GitLab("https://gl.example", token="t")
    calls = []

    def fake(method, path, body=None):
        calls.append((method, path, body))
        return {"labels": ["frontend", "review:round-1"]} if method == "GET" else {}
    gl._req = fake
    gl.add_labels("front/popto", 7, ["review:round-2", "review:passed", "frontend"])
    put = calls[-1]
    assert put[0] == "PUT" and "add_labels" not in put[2]
    assert put[2]["labels"] == "frontend,review:round-1,review:round-2,review:passed"


def test_dingtalk_retries_transient_dns_errors(monkeypatch):
    import subprocess as sp
    import time as _t
    from superops.biz.notifier import DingTalkNotifier
    calls = []
    outs = [(1, "", "dial tcp: lookup mcp-gw.dingtalk.com: i/o timeout"), (0, '{"success": true}', "")]

    def fake(*a, **k):
        calls.append(1)
        rc, out, err = outs[len(calls) - 1]
        return sp.CompletedProcess(a, rc, out, err)
    monkeypatch.setattr(sp, "run", fake)
    monkeypatch.setattr(_t, "sleep", lambda s: None)
    assert DingTalkNotifier({"chat_id": "c"}).send("card", "t", "b")["ok"] and len(calls) == 2
    calls.clear()
    outs[:] = [(1, "", "permission denied")]
    assert not DingTalkNotifier({"chat_id": "c"}).send("card", "t", "b")["ok"] and len(calls) == 1  # 非瞬时错误不重试


def test_listener_watch_stderr_states(tmp_path, monkeypatch):
    import io
    monkeypatch.setenv("SUPEROPS_DATA", str(tmp_path))  # 不要写到真实的 ~/popto-superops-data
    from superops.biz.notifier import listener as L
    class P:
        stderr = io.StringIO(
            "[event] ready event_key=x bus_pid=4242 subscribe_id=s\n"
            '[event] transport {"observed":true,"state":"degraded","reconnect_count":6}\n')
    st = {"connected": True, "changed": 0}
    L._watch_stderr(P(), st, "test")
    assert st["bus_pid"] == 4242 and st["connected"] is False
    P.stderr = io.StringIO('[event] transport {"observed":true,"state":"connected","reconnect_count":0}\n')
    L._watch_stderr(P(), st, "test")
    assert st["connected"] is True


def test_demand_lifecycle_prd_mr_merged(home):
    ops = db.open_ops()
    ops.execute("INSERT INTO demands(id,title,status) VALUES ('dm_1','改 favicon','open')")
    ops.commit()
    lease = {"task": {"id": "t1"}, "events": [{"subject": "demand:dm_1", "data": {"demand_id": "dm_1"}}]}
    url = "https://gl/front/x/popto/merge_requests/12"
    assert ingest.apply("ops-prd-writing", {"size": "S", "mr_url": url}, lease)
    assert tuple(ops.execute("SELECT status, mr_links FROM demands").fetchone()) == ("in_dev", url)
    assert ingest.demand_merged("front/x/popto", 12) == ["dm_1"]
    assert ops.execute("SELECT status FROM demands").fetchone()[0] == "done"
    assert ingest.demand_merged("front/x/popto", 12) == []              # 幂等


def test_listener_poll_fallback_dedups_by_message_id(tmp_path, monkeypatch):
    import json as _j
    import subprocess as sp
    import threading
    from superops.biz.notifier import listener as L
    monkeypatch.setenv("SUPEROPS_DATA", str(tmp_path))
    msgs = {"result": {"messages": [
        {"messageId": "msgA", "createTime": "2026-11-10 13:57:57", "senderId": "ODT_A", "text": "**【需求】卡片** 回复「C2 1」"},
        {"messageId": "msgB", "createTime": "2026-11-10 13:59:00", "senderId": "ODT_A", "text": "C2 1"},
        {"messageId": "msgC", "createTime": "2026-11-10 13:59:30", "senderId": "ODT_X", "text": "C2 3 乱批"}]}}
    monkeypatch.setattr(sp, "run", lambda *a, **k: sp.CompletedProcess(a, 0, _j.dumps(msgs), ""))
    h = FakeHub()
    stop = threading.Event()
    t = threading.Thread(target=L.poll_group, args=("cid", h, ["ODT_A"], False, stop), kwargs={"interval": 0.05})
    t.start()
    import time as _t
    _t.sleep(0.3)
    stop.set()
    t.join(2)
    assert [e["dedup_key"] for e in h.events] == ["dingtalk:msg:msgB"]       # 只有审批人的批红，且多轮轮询只处理一次
    assert h.events[0]["data"]["card"] == "C2"
    # 事件流里同一条消息：去重键相同
    raw = {"type": "user_im_message_receive_group", "event_id": "e9", "message_id": "msgB",
           "sender_open_dingtalk_id": "ODT_A", "content": "C2 1"}
    h2 = FakeHub()
    handle_event(raw, h2, approvers=["ODT_A"])
    assert h2.events[0]["dedup_key"] == "dingtalk:msg:msgB"


def test_demand_initial_score(home):
    ops = db.open_ops()
    lease = {"task": {"id": "t1"}}
    ingest.apply("ops-demand-mining", {"assignments": [
        {"quote": "保险客户要批量出片", "kind": "功能需求", "module": "批量口播", "intensity": 3, "persona": "金融保险从业者",
         "new_demand": {"title": "批量口播支持保险模板", "size": "M", "value": "付费"}},
        {"quote": "也想要", "kind": "功能需求", "module": "批量口播", "intensity": 2, "persona": "普通创作者",
         "demand_id": "dm_" + ingest._sid("批量口播支持保险模板")}]}, lease)
    row = ops.execute("SELECT score, value_factor, persona_dist, signal_count FROM demands").fetchone()
    # (3×1.5 + 2×1.0) × 1.5 ÷ 2 = 4.88
    assert row[0] == 4.88 and row[1] == 1.5 and row[3] == 2
    assert json.loads(row[2]) == {"金融保险从业者": 1, "普通创作者": 1}


def test_event_media_hardlinked_not_symlinked(home, tmp_path):
    from superops.biz.context import ContextBuilder
    src = tmp_path / "content" / "v.mp4"
    src.parent.mkdir()
    src.write_bytes(b"video")
    run = tmp_path / "run"
    run.mkdir()
    ctx = ContextBuilder(None, run)
    try:
        ctx.p_event_media({}, [{"data": {"media_path": str(src)}}])
    finally:
        ctx.close()
    dst = run / "media" / "v.mp4"
    assert dst.exists() and not dst.is_symlink() and dst.read_bytes() == b"video"


def test_platform_names_normalized(home):
    from superops.biz import platforms
    assert platforms.normalize("抖音") == "douyin" and platforms.normalize("B站") == "bilibili"
    assert platforms.normalize("douyin") == "douyin" and platforms.label("抖音") == "抖音"
    ops = db.open_ops()
    pid = metrics.register_post(ops, {"platform": "抖音", "platform_post_id": "123", "title": "t"})
    assert pid == "douyin_123"
    assert ops.execute("SELECT platform FROM posts WHERE id=?", (pid,)).fetchone()[0] == "douyin"


def test_manual_publish_message_has_copy_and_files(tmp_path):
    from superops.biz.executors import publish
    d = tmp_path / "pack"
    d.mkdir()
    (d / "video.mp4").write_bytes(b"v")
    (d / "cover.jpg").write_bytes(b"c")
    meta = {"is_test": True, "test_note": "联调", "media": {"video": "video.mp4", "cover": "cover.jpg"},
            "per_platform": {"抖音": {"title": "下班路过面包店", "body": "三款新品", "topics": ["#面包店"],
                                      "pinned_comment": "主页链接", "ai_label": True, "scheduled_at": "2026-11-10T19:00:00+08:00"}}}
    p = {"id": "pk_x_douyin", "platform": "douyin", "title": "下班路过面包店", "content_id": "x", "pack_dir": str(d)}
    text = publish._manual_instructions(p, meta)
    for want in ("测试内容，不要真的发布", "三款新品", "#面包店", "置顶评论", "AI 生成内容", "19:00", "「cover.jpg」"):
        assert want in text, want
    assert "pack" not in text.split("素材")[0] or str(d) not in text      # 不再给本机目录
    assert [f.name for f in publish._pack_files(p, meta)] == ["cover.jpg", "video.mp4"]


def test_render_card_local_links(tmp_path, monkeypatch):
    from superops.biz.notifier.render import render_card
    monkeypatch.setenv("SUPEROPS_DATA", str(tmp_path))
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "weekly.md").write_text("周报")
    card = {"title": "t", "proposal": "p", "options": [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}],
            "links": ["runs/weekly.md", str(tmp_path / "runs"), "https://x.example/mr/1"]}
    body = render_card(card, 9, None)[1]
    assert "见下一条文件「weekly.md」" in body and "[详情](https://x.example/mr/1)" in body
    assert str(tmp_path / "runs") + "`" not in body and "详情：`" not in body


def test_publish_run_modes(home, monkeypatch, tmp_path):
    from superops.biz.executors import publish, publishers as pubs
    ops = db.open_ops()
    d = tmp_path / "pack"
    d.mkdir()
    (d / "video.mp4").write_bytes(b"v")
    (d / "meta.json").write_text(json.dumps({"media": {"video": "video.mp4"},
                                             "per_platform": {"抖音": {"title": "标题", "body": "正文"}}}, ensure_ascii=False))
    for pid in ("pk_a_douyin", "pk_b_douyin"):
        ops.execute("INSERT INTO publish_packs(id,content_id,platform,title,pack_dir,publish_mode,status) VALUES (?,?,?,?,?,?,?)",
                    (pid, pid[3:4], "douyin", "标题", str(d), "semi_auto", "carded"))
    ops.commit()
    sent, emitted = [], {}

    class Hub:
        def notify(self, kind, title, body, **k):
            sent.append((kind, title))
            return {"ok": True}
    monkeypatch.setattr(publish, "hub_client", lambda: Hub())
    monkeypatch.setattr(publish, "emit_result", lambda summary, **k: emitted.update(summary=summary, **k))

    def run_with(mode, result_or_exc, pack_id):
        sent.clear()
        monkeypatch.setattr(publish, "load_task", lambda: {"task": {"id": "t"}, "events": [
            {"data": {"pack_id": pack_id}, "received_at": iso()}]})
        monkeypatch.setattr("superops.biz.config.load_agents_config", lambda *a, **k: {"publishers": {"douyin": {"mode": mode}}})

        def fake_run(p, meta, *, dry_run, headless=True, draft=False):
            assert dry_run == (mode == "dry_run") and draft == (mode == "draft")
            if isinstance(result_or_exc, Exception):
                raise result_or_exc
            return result_or_exc
        monkeypatch.setattr(pubs, "run", fake_run)
        publish.cmd_run()

    # 试运行：截图 + 仍推人工发布
    shot = tmp_path / "s.png"
    shot.write_bytes(b"p")
    run_with("dry_run", pubs.PublishResult(ok=True, dry_run=True, screenshots=[str(shot)], notes=["已填好"]), "pk_a_douyin")
    kinds = [k for k, _ in sent]
    assert kinds[:2] == ["message", "file"] and any("请手动发布" in t for _, t in sent)
    # 真发布成功：登记作品并发后续事件
    run_with("live", pubs.PublishResult(ok=True, dry_run=False, url="https://www.douyin.com/video/42",
                                        platform_post_id="42"), "pk_b_douyin")
    assert ops.execute("SELECT status FROM publish_packs WHERE id='pk_b_douyin'").fetchone()[0] == "published"
    assert emitted["follow_up_events"][0]["type"] == "social.post.published"
    assert not any("请手动发布" in t for _, t in sent)
    # 存草稿：不登记作品、不推人工发布素材，群里说清楚去草稿箱发；人发完照样回“已发”补登记
    ops.execute("INSERT INTO publish_packs(id,content_id,platform,title,pack_dir,publish_mode,status) VALUES (?,?,?,?,?,?,?)",
                ("pk_d_douyin", "d", "douyin", "标题", str(d), "semi_auto", "carded"))
    ops.commit()
    run_with("draft", pubs.PublishResult(ok=True, dry_run=False, draft=True, screenshots=[str(shot)],
                                         notes=["立即发布", "已存草稿，没有发布（平台提示「保存成功」）"]), "pk_d_douyin")
    assert ops.execute("SELECT status FROM publish_packs WHERE id='pk_d_douyin'").fetchone()[0] == "drafted"
    assert ops.execute("SELECT status FROM actions WHERE action_id='publish:pk_d_douyin'").fetchone()[0] == "awaiting_manual"
    assert not ops.execute("SELECT 1 FROM posts WHERE content_id='d'").fetchone()
    assert any("已存草稿" in t for _, t in sent) and not any("请手动发布" in t for _, t in sent)
    assert emitted["follow_up_events"] == [] and "存草稿 1 条" in emitted["summary"]
    publish.record_manual("pk_d_douyin")
    assert ops.execute("SELECT status FROM publish_packs WHERE id='pk_d_douyin'").fetchone()[0] == "published"


def test_publish_live_failure_falls_back_to_manual(home, monkeypatch, tmp_path):
    from superops.biz.executors import publish, publishers as pubs
    ops = db.open_ops()
    ops.execute("INSERT INTO publish_packs(id,content_id,platform,title,pack_dir,publish_mode,status) VALUES "
                "('pk_c_douyin','c','douyin','t',?, 'semi_auto','carded')", (str(tmp_path),))
    ops.commit()
    sent = []
    monkeypatch.setattr(publish, "hub_client", lambda: type("H", (), {"notify": lambda self, k, t, b, **kw: sent.append((k, t))})())
    monkeypatch.setattr(publish, "emit_result", lambda *a, **k: None)
    monkeypatch.setattr(publish, "load_task", lambda: {"task": {"id": "t"}, "events": [
        {"data": {"pack_id": "pk_c_douyin"}, "received_at": iso()}]})
    monkeypatch.setattr("superops.biz.config.load_agents_config", lambda *a, **k: {"publishers": {"douyin": {"mode": "live"}}})

    def boom(*a, **k):
        raise pubs.NotLoggedIn("抖音登录已失效")
    monkeypatch.setattr(pubs, "run", boom)
    publish.cmd_run()
    assert sent[0][0] == "alert" and "登录失效" in sent[0][1] and any("请手动发布" in t for _, t in sent)


def test_test_content_never_live():
    from superops.biz.executors.publish import _publisher_mode
    cfg = {"publishers": {"douyin": {"mode": "live"}}}
    assert _publisher_mode(cfg, "douyin", {"is_test": True}) == "dry_run"
    assert _publisher_mode(cfg, "douyin", {}) == "live"
    assert _publisher_mode({}, "douyin", {}) == "off"
    draft = {"publishers": {"douyin": {"mode": "draft"}}}
    assert _publisher_mode(draft, "douyin", {}) == "draft" and _publisher_mode(draft, "douyin", {"is_test": True}) == "dry_run"


def test_friendly_publish_errors():
    from superops.biz.executors.publishers import PublishError, friendly_error
    raw = Exception('Locator.click: Timeout 30000ms exceeded.\nCall log:\n  - waiting for get_by_role("button", name="发表视频")')
    assert friendly_error(raw) == "等「发表视频」超时（多半是网络慢或页面改版）"
    assert friendly_error(PublishError("视频上传失败")) == "视频上传失败"


def test_late_registration_skips_past_checkpoints(home):
    ops = db.open_ops()
    at("2026-11-10 10:00")
    pid = metrics.register_post(ops, {"platform": "douyin", "platform_post_id": "77", "title": "老作品",
                                      "published_at": "2026-08-31T23:49:00+08:00"})
    jobs = {r[0]: (r[1] or "") for r in ops.execute("SELECT checkpoint, done_at FROM metric_jobs WHERE post_id=?", (pid,))}
    assert set(jobs) == {"1h", "24h", "72h", "7d", "late"}
    assert all(jobs[c].startswith("skipped:") for c in ("1h", "24h", "72h", "7d")) and jobs["late"] == ""
    # 刚发的作品照常排 4 个节点，没有 late
    pid2 = metrics.register_post(ops, {"platform": "douyin", "platform_post_id": "78", "published_at": "2026-11-10T09:30:00+08:00"})
    cps = [r[0] for r in ops.execute("SELECT checkpoint FROM metric_jobs WHERE post_id=? AND done_at IS NULL", (pid2,))]
    assert sorted(cps) == ["1h", "24h", "72h", "7d"]


def test_comment_batch_payload_uses_collected_at(home):
    from superops.biz.collectors.comments import save_comments
    from superops.biz.context import resolve_payload_ref
    ops = db.open_ops()
    at("2026-11-10 10:00")
    n = save_comments(ops, "douyin_1", [{"id": "c1", "text": "三天前的老评论", "author": "某人", "created_at": 1762500000}],
                      collected_at="2026-11-10T10:00:00+08:00")
    ops.commit()
    rows, untrusted = resolve_payload_ref("ops.db://comments?post_id=douyin_1&since=2026-11-10T10:00:00+08:00", ops)
    assert n == 1 and untrusted and [r["text"] for r in rows] == ["三天前的老评论"]
    assert rows[0]["author_hash"] != "某人"


def test_late_snapshots_never_flag_anomaly(home):
    ops = db.open_ops()
    at("2026-11-10 10:00")
    for i in range(6):
        pid = metrics.register_post(ops, {"platform": "douyin", "platform_post_id": f"l{i}", "published_at": "2026-08-01T10:00:00+08:00"})
        metrics.save_snapshot(ops, {"id": pid, "platform": "douyin", "title": "x"}, "late", {"views": 10}, "creator")
    pid = metrics.register_post(ops, {"platform": "douyin", "platform_post_id": "big", "published_at": "2026-08-01T10:00:00+08:00"})
    evs = metrics.save_snapshot(ops, {"id": pid, "platform": "douyin", "title": "x"}, "late", {"views": 5000}, "creator")
    assert not any(e["type"] == "social.metrics.anomaly" for e in evs)


def test_creator_work_parsers():
    from superops.biz.collectors import creator
    ch = creator.channels_work({
        "objectId": "export/UzFfBgAAxOqpYGQFKkuKk8zT4DCawndAq-Garu-K698IVXkEFA", "createTime": 1794164679,
        "readCount": 189, "likeCount": 2, "favCount": 5, "commentCount": 0, "forwardCount": 1, "followCount": 3,
        "fullPlayRate": 0.06, "avgPlayTimeSec": 7.5,
        "desc": {"shortTitle": [{"shortTitle": "爆款兔批量生成数字人口播"}], "description": "#爆款兔 欢迎使用"}})
    assert ch["id"] == "UzFfBgAAxOqpYGQFKkuKk8zT4DCawndAq-Garu-K698IVXkEFA"   # 去掉 export/ 前缀，免得作品编号里带斜杠
    assert ch["metrics"] == {"views": 189, "likes": 2, "comments": 0, "shares": 1, "follows_gained": 3,
                             "completion_rate": 0.06, "avg_watch_sec": 7.5}             # 推荐👍（favCount）不当收藏
    assert ch["created"].strftime("%Y-%m-%d %H:%M") == "2026-11-09 03:04" and ch["titles"][0] == "爆款兔批量生成数字人口播"
    xn = creator.xhs_work({"id": "6adf5297000000001703a027", "display_title": "今天分享：爆款内容流水线♻️",
                           "time": "2026-10-26 21:16", "view_count": 10, "likes": 1, "collected_count": 0,
                           "comments_count": 1, "shared_count": 1})
    assert xn["url"] == "https://www.xiaohongshu.com/explore/6adf5297000000001703a027" and xn["created"].hour == 21
    assert xn["metrics"] == {"views": 10, "likes": 1, "comments": 1, "shares": 1, "favorites": 0}
    dy = creator.douyin_work({"aweme_id": "7680223353274362362", "create_time": 1788191347, "item_title": "生日快乐",
                              "desc": "生日快乐 #周星驰", "statistics": {"play_count": 165, "digg_count": 3,
                                                                       "comment_count": 1, "share_count": 0, "collect_count": 0}})
    assert dy["metrics"]["views"] == 165 and dy["url"] == "https://www.douyin.com/video/7680223353274362362"


def test_creator_title_matching():
    from datetime import timedelta

    from superops.biz.collectors import creator
    from superops.biz.executors.publishers.channels import short_title
    from superops.biz.timeutil import parse_iso
    pub = parse_iso("2026-11-10T19:00:00+08:00")
    title = "保险人 3 分钟做完一条口播，客户追着问！"

    def w(i, titles, hours):
        return {"id": i, "titles": titles, "created": pub + timedelta(hours=hours), "metrics": {}, "url": None}
    works = [w("new", [short_title(title), "正文第一行"], 0.2),        # 视频号短标题：去符号、截 16 字
             w("old", [short_title(title), ""], -24 * 10),              # 十天前同名的旧作品：超出时间窗
             w("other", ["完全不相关的标题"], 0)]
    assert creator.match_title(title, pub, works)["id"] == "new"
    assert creator.match_title(title, pub, works, taken={"new"}) is None      # 已被别的记录占用
    assert creator.match_title(title, pub, [w("x", [title.replace(" ", "")[:20]], 1)])["id"] == "x"   # 小红书截 20 字
    assert creator.pick_new_work(title, pub - timedelta(minutes=10), works)["id"] == "new"
    assert creator.pick_new_work(title, pub + timedelta(hours=1), works) is None
    assert not creator.same_title("测试", ["测试 测试"])                        # 太短不模糊匹配
    assert creator.same_title("生日快乐", ["生日快乐 #周星驰祝你生日快乐"]) is False
    assert creator.same_title("生日快乐", ["生日快乐"])


def test_post_ids_from_links_and_bare_published_reply():
    from superops.biz.collectors.creator import id_from_url, looks_like_id
    assert id_from_url("douyin", "https://www.douyin.com/video/7680223353274362362?previous_page=x") == "7680223353274362362"
    assert id_from_url("douyin", "https://v.douyin.com/abc/") is None                        # 短链：之后按标题对
    assert id_from_url("xiaohongshu", "https://www.xiaohongshu.com/explore/6adf5297000000001703a027?xsec_token=x") \
        == "6adf5297000000001703a027"
    assert id_from_url("xiaohongshu", "http://xhslink.com/a/AbCd") is None
    assert id_from_url("wechat_channels", None) is None
    assert looks_like_id("wechat_channels", "export/UzFfBgAAxOqpYGQFKkuKk8zT4DCawndAq-Garu-K698IVXkEFA")
    assert not looks_like_id("douyin", "https://v.douyin.com/abc/") and not looks_like_id("xiaohongshu", "")
    assert parse_command("已发 pk_c1_wechat_channels")["data"] == {"pack_id": "pk_c1_wechat_channels"}
    assert parse_command("已发 pk_c1_douyin https://v.douyin.com/abc/")["data"]["url"] == "https://v.douyin.com/abc/"


def test_record_manual_without_link(home, tmp_path):
    from superops.biz.executors.publish import record_manual
    ops = db.open_ops()
    for pid, pf in (("pk_v_wechat_channels", "wechat_channels"), ("pk_x_xiaohongshu", "xiaohongshu")):
        ops.execute("INSERT INTO publish_packs(id,content_id,platform,title,pack_dir,publish_mode,status) VALUES (?,?,?,?,?,?,?)",
                    (pid, "c", pf, "标题", str(tmp_path), "semi_auto", "approved"))
    ops.commit()
    r = record_manual("pk_v_wechat_channels")
    assert r["post_id"] == "wechat_channels_pk_v_wechat_channels" and r["platform_post_id"] is None and r["url"] is None
    r = record_manual("pk_x_xiaohongshu", "https://www.xiaohongshu.com/explore/6adf5297000000001703a027?xsec_token=x")
    assert r["platform_post_id"] == "6adf5297000000001703a027" and r["post_id"] == "xiaohongshu_6adf5297000000001703a027"


def test_collect_reads_creator_backends_and_links_by_title(home, monkeypatch):
    from superops.biz.collectors import creator
    from superops.biz.timeutil import parse_iso
    ops = db.open_ops()
    at("2026-11-10 20:30")
    ch = metrics.register_post(ops, {"platform": "wechat_channels", "post_id": "wechat_channels_pk_v", "platform_post_id": "",
                                     "title": "保险人 3 分钟做完一条口播", "published_at": "2026-11-10T19:20:00+08:00"})
    xh = metrics.register_post(ops, {"platform": "xiaohongshu", "platform_post_id": "6adf5297000000001703a027",
                                     "title": "爆款内容流水线", "published_at": "2026-11-10T19:25:00+08:00"})
    dy = metrics.register_post(ops, {"platform": "douyin", "platform_post_id": "7680223353274362362", "title": "生日快乐",
                                     "published_at": "2026-11-10T19:10:00+08:00"})
    ops.commit()
    calls = []

    def fake_list(platform, want, *, since=None, **k):
        calls.append((platform, set(want), since))
        if platform == "douyin":
            raise RuntimeError("抖音创作者后台返回 status_code=8（多半是登录失效）")
        if platform == "wechat_channels":
            return [{"id": "UzFfNEW" + "x" * 30, "titles": ["保险人 3 分钟做完一条口播", ""], "url": None,
                     "created": parse_iso("2026-11-10T19:21:00+08:00"), "metrics": {"views": 300, "likes": 9}}]
        return [{"id": "6adf5297000000001703a027", "titles": ["爆款内容流水线"], "url": "u",
                 "created": parse_iso("2026-11-10T19:25:00+08:00"), "metrics": {"views": 40, "favorites": 2}}]
    monkeypatch.setattr(creator, "list_works", fake_list)

    class NoTikHub:
        configured, requests = False, 0

        def __init__(self, *a, **k):
            pass
    monkeypatch.setattr(metrics, "TikHub", NoTikHub)          # 别碰本机钥匙串里的真 Key
    monkeypatch.setattr(metrics, "hub_client", lambda: FakeHub())
    out = {}
    monkeypatch.setattr(metrics, "emit_result", lambda summary, **k: out.update(summary=summary, **k))
    metrics.cmd_collect()
    by_pf = {c[0]: c for c in calls}
    assert by_pf["wechat_channels"][1] == set() and by_pf["wechat_channels"][2] is not None   # 没有 ID：按标题找，翻到发布前 3 天
    assert by_pf["xiaohongshu"][1] == {"6adf5297000000001703a027"} and by_pf["xiaohongshu"][2] is None
    assert ops.execute("SELECT platform_post_id FROM posts WHERE id=?", (ch,)).fetchone()[0] == "UzFfNEW" + "x" * 30
    snaps = {r[0]: (r[1], r[2]) for r in ops.execute("SELECT post_id, views, source FROM post_metrics WHERE checkpoint='1h'")}
    assert snaps[ch] == (300, "creator") and snaps[xh] == (40, "creator") and dy not in snaps
    assert "1 条按标题在后台对上了作品" in out["summary"] and "抖音创作者后台没取到" in out["summary"]
    assert out["failed"] == [f"{dy}@1h"]


def test_live_publish_without_post_id_still_registers(home, monkeypatch, tmp_path):
    from superops.biz.executors import publish, publishers as pubs
    ops = db.open_ops()
    ops.execute("INSERT INTO publish_packs(id,content_id,platform,title,pack_dir,publish_mode,status) VALUES "
                "('pk_v_wechat_channels','c','wechat_channels','保险人 3 分钟做完一条口播',?, 'semi_auto','carded')", (str(tmp_path),))
    ops.commit()
    sent, out = [], {}
    monkeypatch.setattr(publish, "hub_client", lambda: type("H", (), {"notify": lambda self, k, t, b, **kw: sent.append((k, t, b))})())
    monkeypatch.setattr(publish, "emit_result", lambda summary, **k: out.update(summary=summary, **k))
    monkeypatch.setattr(publish, "load_task", lambda: {"task": {"id": "t"}, "events": [
        {"data": {"pack_id": "pk_v_wechat_channels"}, "received_at": iso()}]})
    monkeypatch.setattr("superops.biz.config.load_agents_config", lambda *a, **k: {"publishers": {"wechat_channels": {"mode": "live"}}})
    monkeypatch.setattr(pubs, "run", lambda *a, **k: pubs.PublishResult(ok=True, dry_run=False, notes=["已发表"]))
    publish.cmd_run()
    post = ops.execute("SELECT * FROM posts WHERE id='wechat_channels_pk_v_wechat_channels'").fetchone()
    assert post is not None and post["platform_post_id"] is None and post["publish_mode"] == "auto"
    assert ops.execute("SELECT COUNT(*) FROM metric_jobs WHERE post_id=?", (post["id"],)).fetchone()[0] == 4
    assert out["follow_up_events"][0]["type"] == "social.post.published"
    assert any("按标题在后台自动对上" in b for k, t, b in sent if t.startswith("✅"))


def test_profile_lock_serializes_one_platform(home):
    import pytest

    from superops.biz.executors.publishers import ProfileBusy, profile_lock
    with profile_lock("wechat_channels", 0):
        with pytest.raises(ProfileBusy):
            with profile_lock("wechat_channels", 0):
                pass
        with profile_lock("xiaohongshu", 0):      # 不同平台互不影响
            pass
    with profile_lock("wechat_channels", 0):      # 释放后能再拿到
        pass


def test_register_post_without_id_gets_unique_id(home):
    ops = db.open_ops()
    at("2026-11-10 10:00")
    a = metrics.register_post(ops, {"platform": "视频号", "title": "A 标题", "published_at": "2026-11-10T09:00:00+08:00"})
    b = metrics.register_post(ops, {"platform": "视频号", "title": "B 标题", "published_at": "2026-11-10T09:00:00+08:00"})
    assert a != b and a.startswith("wechat_channels_t")
    assert ops.execute("SELECT platform_post_id FROM posts WHERE id=?", (a,)).fetchone()[0] is None
    # 后来拿到了作品 ID（人回了带链接的“已发”）：同一编号补上 ID，不另起一条
    again = metrics.register_post(ops, {"platform": "wechat_channels", "post_id": a, "platform_post_id": "U" * 40})
    assert again == a and ops.execute("SELECT platform_post_id FROM posts WHERE id=?", (a,)).fetchone()[0] == "U" * 40
    assert metrics.register_post(ops, {"platform": "wechat_channels", "post_id": "other", "platform_post_id": "U" * 40}) == a
    x = metrics.register_post(ops, {"platform": "xiaohongshu",
                                    "url": "https://www.xiaohongshu.com/explore/6adf5297000000001703a027?xsec_token=1"})
    assert x == "xiaohongshu_6adf5297000000001703a027"


def test_needs_verification_then_retry_with_same_approval(home, monkeypatch, tmp_path):
    """抖音点了发布要短信验证：转人工并说清楚；之后用原来的批准重试（不用重新批），发成就登记作品。"""
    from superops.biz.executors import ActionLedger, publish, publishers as pubs
    ops = db.open_ops()
    ops.execute("INSERT INTO publish_packs(id,content_id,platform,title,pack_dir,publish_mode,status) VALUES "
                "('pk_v_douyin','v','douyin','标题',?, 'semi_auto','carded')", (str(tmp_path),))
    ops.commit()
    sent, emitted = [], []

    class Hub:
        def notify(self, kind, title, body, **k):
            sent.append((kind, title, body))
            return {"ok": True}

        def emit(self, evt):
            emitted.append(evt)
            return {"routed": "trigger"}
    monkeypatch.setattr(publish, "hub_client", lambda: Hub())
    monkeypatch.setattr(publish, "emit_result", lambda *a, **k: None)
    monkeypatch.setattr(publish, "load_task", lambda: {"task": {"id": "t"}, "events": [
        {"data": {"pack_id": "pk_v_douyin"}, "received_at": iso()}]})
    monkeypatch.setattr("superops.biz.config.load_agents_config", lambda *a, **k: {"publishers": {"douyin": {"mode": "live"}}})
    calls = []

    def verify(p, meta, *, dry_run, headless=True, draft=False):
        calls.append(headless)
        raise pubs.NeedsVerification("点了发布后抖音要求短信验证")
    monkeypatch.setattr(pubs, "run", verify)
    publish.cmd_run()
    assert sent[0][0] == "alert" and "身份验证" in sent[0][1] and "短信验证" in sent[0][2] and "superops publish retry pk_v_douyin --headed" in sent[0][2]
    assert any("请手动发布" in t for _, t, _ in sent)
    status = lambda: ops.execute("SELECT status FROM actions WHERE action_id='publish:pk_v_douyin'").fetchone()[0]  # noqa: E731
    assert status() == "awaiting_manual"
    # 原批准还在 24 小时内：有界面重试，发成后登记作品、发出“已发布”事件
    monkeypatch.setattr(pubs, "run", lambda p, meta, *, dry_run, headless=True, draft=False: (calls.append(headless), pubs.PublishResult(
        ok=True, dry_run=False, url="https://www.douyin.com/video/77", platform_post_id="77"))[1])
    result = publish.retry("pk_v_douyin", headed=True)
    assert result["ok"] and calls == [True, False] and status() == "done"
    assert ops.execute("SELECT platform_post_id FROM posts WHERE platform='douyin'").fetchone()[0] == "77"
    assert ops.execute("SELECT COUNT(*) FROM metric_jobs WHERE post_id=?", (result["post"]["post_id"],)).fetchone()[0] >= 4
    assert emitted[0]["type"] == "social.post.published"
    # 发完了就不能再重试；批准过期的也不行
    assert publish.retry("pk_v_douyin")["error"].startswith("这个动作的状态是 done")
    ops.execute("INSERT INTO actions(action_id,kind,ref,approved_at,status) VALUES ('publish:old','publish','old','2020-01-01T00:00:00+08:00','awaiting_manual')")
    ops.commit()
    assert "超过 24 小时" in ActionLedger(ops).reopen("publish:old")[1]


def _no_tikhub(monkeypatch):
    class NoTikHub:
        configured, requests = False, 0

        def __init__(self, *a, **k):
            pass
    monkeypatch.setattr(metrics, "TikHub", NoTikHub)
    monkeypatch.setattr(metrics, "hub_client", lambda: FakeHub())


def test_手动采集_全部采一遍_存手动快照_不动节点_跳过已删除和太老的(home, monkeypatch):
    from superops.biz.collectors import creator
    from superops.biz.timeutil import parse_iso
    ops = db.open_ops()
    at("2026-11-12 11:00")
    live = metrics.register_post(ops, {"platform": "xiaohongshu", "platform_post_id": "6af44270000000002a00e1b9", "title": "讲解片",
                                       "published_at": "2026-11-11T18:24:52+08:00"})
    gone = metrics.register_post(ops, {"platform": "xiaohongshu", "platform_post_id": "6af438a4000000001803966c", "title": "30 秒成片",
                                       "published_at": "2026-11-11T17:43:04+08:00"})
    old = metrics.register_post(ops, {"platform": "xiaohongshu", "platform_post_id": "6a0000000000000000000001", "title": "老作品",
                                      "published_at": "2026-09-01T10:00:00+08:00"})
    assert metrics.mark_removed(ops, gone, "作品已被本人删除") and not metrics.mark_removed(ops, "nope")
    left = {r[0]: r[1] for r in ops.execute("SELECT checkpoint, last_error FROM metric_jobs WHERE post_id=? AND done_at LIKE 'skipped:%'", (gone,))}
    assert set(left) >= {"24h", "72h", "7d"} and "不再采集" in left["7d"]
    asked = []

    def fake_list(platform, want, *, since=None, **k):
        asked.append(set(want))
        return [{"id": "6af44270000000002a00e1b9", "titles": ["讲解片"], "url": "u", "created": parse_iso("2026-11-11T18:24:52+08:00"),
                 "metrics": {"views": 120, "likes": 3, "comments": 1}}]
    monkeypatch.setattr(creator, "list_works", fake_list)
    _no_tikhub(monkeypatch)
    out = {}
    monkeypatch.setattr(metrics, "emit_result", lambda summary, **k: out.update(summary=summary, **k))
    monkeypatch.setattr(metrics, "load_task", lambda: {"task": {"id": "t"}, "events": [{"data": {"post_ids": []}}]})
    metrics.cmd_now([])
    assert asked == [{"6af44270000000002a00e1b9"}]                      # 删掉的、30 天前的都不采
    snaps = list(ops.execute("SELECT post_id, checkpoint, views, source FROM post_metrics"))
    assert [tuple(r) for r in snaps] == [(live, "manual", 120, "creator")]
    assert ops.execute("SELECT COUNT(*) FROM metric_jobs WHERE post_id=? AND done_at IS NOT NULL AND done_at NOT LIKE 'skipped:%'",
                       (live,)).fetchone()[0] == 0                    # 1h…7d 节点不被手动快照占掉
    assert out["status"] == "done" and "采到 1 条" in out["summary"] and "播放 120" in out["summary"]
    assert old and metrics.manual_targets(ops, [gone, "nope"])[1] == ["「30 秒成片」已在平台删除，不采", "nope：没有这条作品"]


def test_手动采集_指定一条_没采到写原因_定时采集不碰已删除的(home, monkeypatch):
    from superops.biz.collectors import creator
    ops = db.open_ops()
    at("2026-11-12 11:00")
    a = metrics.register_post(ops, {"platform": "wechat_channels", "platform_post_id": "UzFfBgAAxJCpMEVxBhWXk8zT4DCaEhOgn2fr7i7tIMfGdOZ2Iw",
                                    "title": "Superagent架构全拆解", "published_at": "2026-11-11T20:04:21+08:00"})
    b = metrics.register_post(ops, {"platform": "douyin", "platform_post_id": "7706848829571648819", "title": "删了",
                                    "published_at": "2026-11-11T09:00:00+08:00"})
    metrics.mark_removed(ops, b)
    ops.execute("UPDATE metric_jobs SET done_at=NULL WHERE post_id=? AND checkpoint='24h'", (b,))   # 就算还挂着没采的节点
    monkeypatch.setattr(creator, "list_works", lambda platform, want, **k: (_ for _ in ()).throw(RuntimeError("视频号登录已失效")))
    _no_tikhub(monkeypatch)
    out = {}
    monkeypatch.setattr(metrics, "emit_result", lambda summary, **k: out.update(summary=summary, **k))
    monkeypatch.setattr(metrics, "load_task", lambda: {"task": {"id": "t"}, "events": [{"data": {"post_ids": [a]}}]})
    metrics.cmd_now([])
    assert out["status"] == "failed" and "没采到" in out["summary"] and "登录" in out["failed"][0]
    at("2026-11-12 23:00")
    seen = []
    monkeypatch.setattr(creator, "list_works", lambda platform, want, **k: seen.append(platform) or [])
    metrics.cmd_collect()
    assert "douyin" not in seen and all(b not in x for x in out.get("failed", []) + out.get("manual", []))


# ---------------------------------------------------------------------- 测试需求的标记、MR ↔ 需求、合并后的线上核实
PROJ = "front/x/popto"
MR_URL = f"https://gl/{PROJ}/merge_requests/13"


def _ci(monkeypatch, verify=None, mr=None, merge_res=None, merge_base=None):
    from superops.biz.ci import __main__ as ci
    from superops.biz.ci.gitlab import GitLab
    cfg = {"gitlab_url": "https://gl", "projects": [PROJ], "auto_merge": False, "branch_prefix": "agent/", "verify": verify or {}}
    monkeypatch.setattr(ci, "_ci_cfg", lambda: dict(cfg))
    monkeypatch.setattr(GitLab, "configured", property(lambda self: True))
    monkeypatch.setattr(GitLab, "mr", lambda self, p, i: mr or {"web_url": MR_URL, "title": "fix: 主题初始化", "labels": [], "sha": "a5ecdbc"})
    monkeypatch.setattr(GitLab, "changes", lambda self, p, i: [{"new_path": "index.html", "diff": "+x\n"}])
    monkeypatch.setattr(GitLab, "note", lambda self, *a: {})
    monkeypatch.setattr(GitLab, "merge", lambda self, p, i, sha=None: merge_res if merge_res is not None else
                        {"merge_commit_sha": "c9a7ca68103e", "web_url": MR_URL, "title": "fix: 主题初始化"})
    monkeypatch.setattr(GitLab, "merge_base", lambda self, p, a, b: merge_base)
    out = {}
    monkeypatch.setattr(ci, "emit_result", lambda summary, **k: out.update(summary=summary, **k))
    return ci, out


def _test_demand(ops, quote="【测试】index.html 主题脚本加保护。这是测试“需求 → 开发”链路用的需求，MR 不合并。"):
    lease = {"task": {"id": "t70-demand-manual"}, "events": []}
    ingest.apply("ops-demand-mining", {"assignments": [{"quote": quote, "kind": "bug", "intensity": 2,
                                                        "new_demand": {"title": "主题初始化读 localStorage 加保护", "size": "S"}}]}, lease)
    return ops.execute("SELECT id, title, is_test FROM demands").fetchone()


def test_测试需求_入池时打标记_标题补回测试(home):
    ops = db.open_ops()
    did, title, is_test = _test_demand(ops)
    assert is_test == 1 and title == "【测试】主题初始化读 localStorage 加保护"
    ingest.apply("ops-demand-mining", {"assignments": [{"demand_id": did, "quote": "【测试】再说一遍", "kind": "bug"}]},
                 {"task": {"id": "t2"}, "events": []})
    assert ops.execute("SELECT title FROM demands WHERE id=?", (did,)).fetchone()[0].count("【测试】") == 1
    assert not ingest.looks_like_test("导出 A/B 测试需求的结果", "测试一下这个按钮")   # 正经需求里的“测试”不算


def test_开发工单后续几轮_按工单找回需求_写上MR链接(home):
    ops = db.open_ops()
    did = _test_demand(ops)[0]
    ingest.apply("ops-prd-writing", {"prd_path": "prd.md", "needs_decision": True},
                 {"task": {"id": "t72-prd-writing"}, "events": [{"subject": f"demand:{did}", "data": {"demand_id": did}}]})
    # PRD 批了之后那一轮：输入是人的回答，事件里没有需求 ID；宿主推送后把 MR 链接放进产出
    ingest.apply("ops-prd-writing", {"mr_url": MR_URL}, {"task": {"id": "t72-prd-writing"}, "events": [{"data": {}}]})
    assert tuple(ops.execute("SELECT status, mr_links FROM demands").fetchone()) == ("in_dev", MR_URL)
    d = ingest.demand_for_mr(ops, PROJ, 13)
    assert d["id"] == did and d["is_test"] and "MR 不合并" in d["quote"]


def test_合并卡片_测试需求标在标题建议风险里_带需求原话(home, monkeypatch):
    ops = db.open_ops()
    did = _test_demand(ops)[0]
    ops.execute("UPDATE demands SET mr_links=? WHERE id=?", (MR_URL, did))
    ops.commit()
    ci, out = _ci(monkeypatch)
    monkeypatch.setattr(ci, "load_task", lambda: {"task": {"id": "t75"}, "events": [{"data": {"project": PROJ, "iid": 13, "sha": "a5ecdbc"}}]})
    ci.cmd_merge_gate()
    card = out["decision_card"]
    assert card["title"].startswith("【测试】合并") and card["proposal"] == "先不合（这是测试需求）"
    assert card["risks"][0] == "这是测试需求（原话里写了不合并）"
    assert any(r.startswith("需求原话：") and "MR 不合并" in r for r in card["reasons"])
    assert "没配线上版本地址" in card["risks"][-1]
    ops.execute("UPDATE demands SET is_test=0, title='正常需求' WHERE id=?", (did,))
    ops.execute("UPDATE signals SET quote='希望主题脚本加保护'")
    ops.commit()
    ci, out = _ci(monkeypatch, verify={PROJ: {"url": "https://popto/version.json"}})
    monkeypatch.setattr(ci, "load_task", lambda: {"task": {"id": "t75"}, "events": [{"data": {"project": PROJ, "iid": 13}}]})
    ci.cmd_merge_gate()
    card = out["decision_card"]
    assert card["title"].startswith("合并") and card["proposal"] == "合并（审查已通过）" and card["risks"][-1] == "合并后会自动核实线上版本"


def test_合并后_没配地址的请人确认上线_只登记一次(home, monkeypatch):
    ci, out = _ci(monkeypatch)
    monkeypatch.setattr(ci, "load_task", lambda: {"task": {"id": "t76"}, "events": [{"data": {"project": PROJ, "iid": 13}}]})
    ci.cmd_merge()
    types = [e["type"] for e in out["follow_up_events"]]
    assert types == ["gitlab.mr.merged", "deploy.unverifiable"] and "没法自动核实上线" in out["summary"]
    assert "ci.verify" in out["follow_up_events"][1]["summary"]
    assert ci.register_deploy_check(ci._ci_cfg(), PROJ, 13, "c9a7", MR_URL) == []          # poll 再发现这次合并也不重复
    ops = db.open_ops()
    assert ops.execute("SELECT status FROM deploy_checks").fetchone()[0] == "unconfigured"


def test_合并后_定时核实线上版本_包含就通过_过期没上线就告警(home, monkeypatch):
    at("2026-11-12 11:20")
    ci, out = _ci(monkeypatch, verify={PROJ: {"url": "https://popto/version.json", "within": "6h"}})
    monkeypatch.setattr(ci, "load_task", lambda: {"task": {"id": "t76"}, "events": [{"data": {"project": PROJ, "iid": 13}}]})
    ci.cmd_merge()
    assert [e["type"] for e in out["follow_up_events"]] == ["gitlab.mr.merged"] and "每 30 分钟核实" in out["summary"]
    from superops.biz.ci.gitlab import GitLab
    gl = GitLab("https://gl", token="t")
    online = {"v": "9476137aaaa"}                                    # 还是旧版本
    monkeypatch.setattr(ci, "online_version", lambda url: online["v"])
    assert ci.check_deploys(gl) == ([], [])                          # 期限内：继续等
    online["v"] = "d00dfeed1234"                                     # 后面又合了别的，一起上线：线上提交包含它
    monkeypatch.setattr(GitLab, "merge_base", lambda self, p, a, b: "c9a7ca68103e")
    events, notes = ci.check_deploys(gl)
    assert [e["type"] for e in events] == ["deploy.verified"] and notes == [f"{PROJ}!13 已上线"]
    assert ci.check_deploys(gl) == ([], [])                          # 通过了就不再查
    db.open_ops().execute("UPDATE deploy_checks SET status='pending'")
    online["v"] = "9476137aaaa"
    monkeypatch.setattr(GitLab, "merge_base", lambda self, p, a, b: "9476137aaaa")
    at("2026-11-12 17:30")
    events, _ = ci.check_deploys(gl)
    assert [e["type"] for e in events] == ["deploy.mismatch"] and "期望包含 c9a7ca68103e" in events[0]["summary"]
