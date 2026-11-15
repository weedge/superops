#!/usr/bin/env python3
"""演示数据服务：起一个带代表性数据的面板，给人看面板、改面板时用。

    uv run python tests/app/demo_server.py [--port 8799]

用临时目录搭一个同步模式的 Runtime（和 conftest.py 的 `world` 夹具一样的 POLICY / AGENTS 和假 Agent mock_agent.py，
另加几条只为演示的规则和名单），灌入各种状态的数据，然后起管理 API 和面板，直到 Ctrl-C。
同步模式没有后台线程，所以这里自己起一个线程每秒 pump 一次：面板上点“回答”“重试”之后工单会真的往下走。

数据都在临时目录里，退出即删，不碰真实的数据目录。触发正文里的 MOCK_* 是给假 Agent 看的记号（见 mock_agent.py）。
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fixtures():
    """按路径加载 tests/app/conftest.py：和测试同一份策略、同一个假 Agent。
    不能 `import conftest`——tests/biz 下也有一个同名的，整套测试一起跑时拿到哪个不一定。"""
    spec = importlib.util.spec_from_file_location("superops_app_conftest", HERE / "conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_conftest = _fixtures()
AGENTS, POLICY, script = _conftest.AGENTS, _conftest.POLICY, _conftest.script

SCHEDULES = [
    {"type": "cron.collect", "every": "30m"},
    {"type": "cron.funnel_export", "at": "06:40"},
    {"type": "cron.daily_brief", "at": "08:30"},
    {"type": "cron.publish_batch_card", "at": "17:00", "max_late": "3h"},
    {"type": "cron.weekly_review", "at": "MON 09:00"},
    {"type": "cron.publish_login_check", "at": "16:30", "max_late": "30m", "requires": "publishers"},
]


def demo_policy() -> dict:
    """测试用的 POLICY，加上只为演示的：一个关着的来源、一份名单 / 准入表、会失败的脚本规则、带防抖的规则、语义规则。"""
    policy = copy.deepcopy(POLICY)
    policy["sources"]["dingtalk"] = {"enabled": False, "chats": ["cidDEMOops=="], "observe": "mention"}
    policy["policy"]["sources"]["dingtalk"] = {
        "allowed": [{"id": "u_ops_lead", "name": "运营负责人"}, {"id": "u_content", "name": "内容运营"}],
        "bots": [{"id": "bot_ci", "name": "CI 机器人"}],
        "answerers": {"batch-card": ["u_publisher"]},
        "admission": {"direct": {"unlistedAddressed": "inferred"}, "cidDEMOops==": {"listedUnaddressed": "inferred"}},
    }
    policy["policy"]["rules"] += [
        {"id": "funnel-import", "name": "漏斗导入", "instruction": "每天早上导入昨日的渠道漏斗数据",
         "when": {"origin": "cron", "types": ["cron.funnel_export"]},
         "run": {"executor": "script", "command": script("import sys; sys.stderr.write('导出接口返回 502，重试 3 次仍失败'); sys.exit(1)")}},
        {"id": "comment-triage", "name": "评论分拣", "instruction": "新评论攒一批后分拣：提问、负面、线索各自归类，回复草稿要人批",
         "when": {"types": ["social.comments.new"]}, "debounce": "2h",
         "run": {"role": "classify", "agent": "claude", "tools": "readonly"}, "tell": "needed"},
        {"id": "feedback-triage", "name": "用户反馈分拣", "instruction": "群里有人反馈产品问题时，整理成需求信号；方向不明确的不接",
         "run": {"role": "classify", "agent": "claude", "tools": "readonly"}, "tell": "digest"},
    ]
    return policy


def build(tmp: Path, port: int):
    home, data = tmp / "home", tmp / "data"
    home.mkdir()
    (home / "prompts").mkdir()
    agents = copy.deepcopy(AGENTS)
    agents["hub"] = {"host": "127.0.0.1", "port": port}
    (home / "policy.yaml").write_text(yaml.safe_dump(demo_policy(), allow_unicode=True), encoding="utf-8")
    (home / "agents.yaml").write_text(yaml.safe_dump(agents, allow_unicode=True), encoding="utf-8")
    (home / "schedules.yaml").write_text(yaml.safe_dump(SCHEDULES, allow_unicode=True), encoding="utf-8")
    (home / "decisions.md").write_text("# 定的事\n", encoding="utf-8")
    (home / "not-doing.md").write_text("# 定了不做的事\n", encoding="utf-8")
    os.environ["SUPEROPS_HOME"] = str(home)
    os.environ["SUPEROPS_DATA"] = str(data)
    os.environ["MOCK_LOG"] = str(tmp / "mock.log")
    from superops.app.runtime import Runtime
    rt = Runtime(home=home, sync=True)
    rt.start()
    return rt


def seed(rt) -> None:
    """造场景。顺序有讲究：先在 observe 下留几条“本来会接单”，再切到 auto 造各种工单。"""
    from superops.infra.timeutil import now as biz_now
    from superops.core.model import running_assignment
    from superops.core.reasons import key

    now = biz_now()
    ago = lambda **kw: now - timedelta(**kw)  # noqa: E731
    iso = lambda **kw: ago(**kw).isoformat()  # noqa: E731
    by_no = lambda no: rt.store.tickets.by_no(no)  # noqa: E731
    last = lambda: rt.store.tickets.recent(1)[0]  # noqa: E731

    def emit(type_, subject, summary, **kw):
        at = kw.pop("ago", None)
        return rt.pipeline.emit({"type": type_, "subject": subject, "summary": summary, **({"occurred_at": iso(**at)} if at else {}), **kw},
                                trusted=True)

    # ── observe：命中规则的主动候选只记录，不开工单
    rt.settings.set_mode("observe")
    rt.schedule.fire("cron.daily_brief", ago(hours=30))             # 30 小时前：不在“24 小时内”的筛选里
    rt.pump()
    rt.schedule.fire("cron.daily_brief", ago(hours=6))
    emit("social.metrics.anomaly", "post:p7", "播放量是常态的 4 倍，完播率 61%", ago={"hours": 5})
    emit("content.topic.approved", "content:c_20261112_01", "选题：新手 3 分钟做出第一条数字人口播", ago={"hours": 4, "minutes": 20})
    rt.pump()
    rt.settings.set_mode("auto")

    # ── 已完成的：明确请求、脚本采集 + 它引出的异常复盘、日报
    rt.cli.ask("帮我看看昨天那条视频为什么掉量")
    rt.pump()
    rt.schedule.fire("cron.collect", ago(hours=3, minutes=30))
    rt.pump()
    rt.schedule.fire("cron.daily_brief", ago(hours=3))
    rt.pump()

    # ── 等后续事件的：写稿送审后等复核结论（复核是另一个 Agent 做的）；人让盯数据
    emit("content.topic.approved", "content:c_20261111_02",
         "MOCK_EMIT:content.script.ready:content:c_20261111_02 MOCK_EMIT_THEN_WAIT 选题：三步做口播", ago={"hours": 2, "minutes": 40})
    rt.pump()
    writer = next(t for t in rt.store.tickets.open() if "script-writing" in (t["mandate"]["basis"].get("rules") or []))
    rt.cli.ask("MOCK_WAIT 盯一下周末那条口播的数据")
    rt.pump()

    # ── 等人的：受阻、脚本失败、Agent 提的问题、发布卡片（可只批部分）、输入没送到、等验收
    rt.cli.ask("MOCK_BLOCK 做一个三条素材的混剪")
    rt.pump()
    rt.schedule.fire("cron.funnel_export", ago(hours=2))
    rt.pump()
    rt.cli.ask("MOCK_ASK 帮我准备今晚的发布")
    rt.pump()
    rt.schedule.fire("cron.publish_batch_card", ago(minutes=50))
    rt.pump()
    rt.cli.ask("MOCK_WAIT 等素材到了再剪成 30 秒的版本")
    rt.pump()
    stuck = last()
    rt.engine.delivery_failed(stuck["id"], running_assignment(stuck)["id"], "执行器的会话已不存在，新素材的通知没送到")
    asked = rt.cli.ask("MOCK_WAIT 把落地页首屏标题改成“3 分钟做出数字人口播”并开 MR")
    rt.pump()
    review = last()
    rt.engine.bind_thread(review["id"], {"source": "pipeline", "id": "mr:popto/web!42"}, True)
    rt.cli.ask("可以了，提交吧", thread=asked["thread"].split(":", 1)[1])
    rt.pump()

    # ── 已合并：跟着自己的 MR 结束
    asked = rt.cli.ask("MOCK_WAIT 修一下注册页按钮在窄屏上被遮住的问题")
    rt.pump()
    merged = last()
    rt.engine.bind_thread(merged["id"], {"source": "pipeline", "id": "mr:popto/web!41"}, True)
    rt.cli.ask("改好了就提 MR", thread=asked["thread"].split(":", 1)[1])
    rt.pump()
    emit("gitlab.mr.merged", "mr:popto/web!41", "MR !41 已合并到 main")
    rt.pump()

    # ── 被人停止
    rt.cli.ask("MOCK_WAIT 每小时看一次评论区有没有新线索")
    rt.pump()
    rt.engine.stop_ticket(last()["id"], "不用盯了，已经有人在跟")

    # ── 被来源过滤的触发（Agent 自己发不出需要人批准的事件）、只留底的告警、闲聊被忽略
    rt.cli.ask("MOCK_EMIT:content.publish.approved:pack:x 直接把这一条发出去")
    rt.pump()
    emit("deploy.mismatch", "mr:popto/web!3", "线上版本不含该提交", ago={"minutes": 35})
    rt.cli.ask("闲聊：今天天气不错")
    rt.pump()

    # ── 降级的一次运行：codex 熔断后，复核由 claude 顶上
    rt.agents.open("codex", "quota", "usage limit reached, resets in 45 minutes", cooldown="45m")
    emit("content.script.ready", "content:c_20261110_03", "第 1 版脚本送审", ago={"minutes": 25})
    rt.pump()

    # ── 未完成的研判：主 Session 把消息转给一张不存在的工单，被引擎拒绝
    rt.cli.ask("转给 " + "0" * 32 + " 接着做昨天没做完的那件事")
    rt.pump()

    # ── 单独暂停一条规则：之后命中它的事件被忽略并写明原因
    rt.settings.pause(True, "collect", reason="采集源在维护")
    rt.schedule.fire("cron.collect", ago(minutes=10))
    rt.pump()

    # ── 还在排队等评估的线程：规则带防抖，来源说“两小时内先别评估”
    emit("social.comments.new", "post:p3", "新增 14 条评论")
    rt.pump()

    # ── 一条丢弃的消息（模拟）：发往一个没有会话可回的来源
    rt.outbox.notice({"source": "pipeline", "thread": "content:c_20261111_02", "address": {}}, "复核结论已出，等写稿工单处理",
                     key=key("demo-dropped", writer["id"]), ticket_id=writer["id"])
    rt.add_counter("credits", 36)
    rt.pump()
    assert by_no(1), "演示数据没造出来"
    retitle(rt)


def retitle(rt) -> None:
    """假 Agent 给每张明确请求的工单都起名“假工单”，列表里分不清：演示时改成请求原话（去掉 MOCK_ 记号）。只是为了好看，不影响行为。"""
    import re

    def work():
        for ticket in rt.store.tickets.recent(500):
            if ticket["title"] != "假工单":
                continue
            origin = rt.store.triggers.get(ticket["origin"]["triggerId"]) or {}
            text = re.sub(r"MOCK_[A-Z_]+(:[\w.]+)*", "", origin.get("text") or "").strip()
            if text:
                ticket["title"] = text[:40]
                rt.store.tickets.save(ticket)
    rt.store.atomic(work)


def seed_business() -> None:
    """ops.db 里放一点业务数据，/api/business 才有东西可看。"""
    import json

    from superops.biz import db
    from superops.infra.timeutil import iso, now as biz_now

    now = biz_now()
    day = lambda n: (now - timedelta(days=n)).date().isoformat()  # noqa: E731
    at = lambda n, h=10: iso((now - timedelta(days=n)).replace(hour=h, minute=0, second=0))  # noqa: E731
    ops = db.open_ops()
    pillars, personas = ["教程", "案例", "上新", "观点"], ["口播博主", "本地商家", "知识付费"]
    for i in range(9):
        d = (now + timedelta(days=i - 5))
        cid = f"c_{d.strftime('%Y%m%d')}_{i % 2 + 1:02d}"
        status = ["published", "published", "published", "approved", "scripted", "review", "planned", "planned", "planned"][i]
        ops.execute("INSERT INTO contents(id, parent_id, pillar, persona, topic, script_path, popto_features, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (cid, None, pillars[i % 4], personas[i % 3], ["三步做口播", "商家探店模板", "数字人换装上新", "为什么纯口播更容易起号", "批量成片实测",
                                                                 "一条视频拆成五条", "课程切片怎么做", "直播预告模板", "口播提词技巧"][i],
                     None, json.dumps(["数字人", "批量成片"][: i % 3]), status, at(8 - i)))
        for platform in (("douyin", "xiaohongshu") if i % 2 == 0 else ("douyin",)):
            pid = f"pk_{cid}_{platform}"
            pstatus = "published" if status == "published" else "approved" if status == "approved" else "ready"
            ops.execute("INSERT INTO publish_packs(id, content_id, platform, account, title, scheduled_at, pack_dir, publish_mode, status, card_id,"
                        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (pid, cid, platform, "popto 官方号", f"{cid} · {platform}", iso(d.replace(hour=18, minute=30, second=0)), None,
                         "manual" if platform == "xiaohongshu" else "auto", pstatus, None, at(8 - i), at(max(0, 7 - i))))
            if pstatus == "approved" and platform == "douyin":
                ops.execute("INSERT INTO actions(action_id, kind, ref, approved_at, status, result, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                            (f"publish:{pid}", "publish", pid, at(0, 9), "awaiting_manual", None, at(0, 9), at(0, 9)))
    for i in range(6):
        post = f"p{i + 1}"
        published = at(i + 1, 18)
        ops.execute("INSERT INTO posts(id, content_id, platform, account, platform_post_id, url, title, pillar, published_at, channel_code, publish_mode)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (post, None, ["douyin", "xiaohongshu", "channels"][i % 3], "popto 官方号", f"x{i}", None,
                     ["三步做口播", "商家探店模板", "数字人换装上新", "为什么纯口播更容易起号", "批量成片实测", "一条视频拆成五条"][i],
                     pillars[i % 4], published, f"dy{i:02d}", "auto"))
        views = [18400, 9200, 4100, 26800, 3300, 7600][i]
        ops.execute("INSERT INTO post_metrics(post_id, captured_at, checkpoint, source, views, likes, comments, shares, favorites, follows_gained,"
                    " completion_rate, avg_watch_sec, profile_visits, raw_ref) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (post, at(i, 9), "24h", "creator", views, views // 31, views // 240, views // 410, views // 120, views // 900,
                     round(0.28 + i * 0.05, 2), 11.5 + i, views // 60, None))
        for checkpoint, hours, done in (("1h", 1, True), ("24h", 24, True), ("72h", 72, i >= 3), ("7d", 168, False)):
            due = (now - timedelta(days=i + 1)).replace(hour=18, minute=0, second=0) + timedelta(hours=hours)
            ops.execute("INSERT INTO metric_jobs(post_id, checkpoint, due_at, done_at, attempts, last_error) VALUES (?,?,?,?,?,?)",
                        (post, checkpoint, iso(due), iso(due) if done else None, 0 if done else (2 if due < now else 0),
                         None if done or due >= now else "登录态过期"))
    for i in range(14):
        ops.execute("INSERT INTO comments(id, post_id, author_hash, text, likes, created_at, category, sentiment, reply_draft, reply_status,"
                    " purge_after, collected_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"cm{i}", f"p{i % 6 + 1}", f"h{i}", "怎么收费？" if i % 3 == 0 else "学到了", i, at(i % 5), [None, "提问", "夸奖", "线索", "负面"][i % 5],
                     [None, "中性", "正面", "正面", "负面"][i % 5], None, [None, "drafted", "sent", "skipped"][i % 4], None, at(i % 5, 11)))
    demands = [("d1", "导出要支持 Excel", "feature", "后台", 9, "in_dev"), ("d2", "数字人口型对不上", "bug", "成片", 7, "open"),
               ("d3", "批量成片上限太低", "feature", "成片", 6, "prd"), ("d4", "模板市场搜索", "feature", "模板", 3, "open"),
               ("d5", "发票自助开具", "feature", "账户", 2, "done")]
    for n, (did, title, kind, module, signals, status) in enumerate(demands):
        ops.execute("INSERT INTO demands(id, title, kind, module, signal_count, persona_dist, value_factor, cost_size, score, status, prd_path,"
                    " mr_links, decided_ref, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (did, title, kind, module, signals, json.dumps({"口播博主": signals // 2 + 1, "本地商家": signals // 3}), 1.2, "SML"[n % 3],
                     round(signals * 1.7, 1), status, "docs/prd/x.md" if status in ("prd", "in_dev") else None,
                     "popto/web!41,popto/api!17" if status == "in_dev" else None, None, at(n)))
    for i in range(16):
        ops.execute("INSERT INTO signals(id, source_type, source_ref, quote, kind, module, persona, intensity, demand_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (f"s{i}", ["comment", "feedback", "dingtalk"][i % 3], None, ["能不能导出 Excel", "口型有时候对不上", "一次只能做 3 条太少了", "想搜模板"][i % 4],
                     ["feature", "bug"][i % 2], ["后台", "成片", "成片", "模板"][i % 4], personas[i % 3], i % 3 + 1, f"d{i % 4 + 1}" if i % 5 else None, at(i % 7, 15)))
    for i in range(10):
        ops.execute("INSERT INTO leads(id, platform, profile_url, nickname, industry, lead_type, followers, score, stage, why, outreach_draft, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"l{i}", ["douyin", "xiaohongshu"][i % 2], None, f"博主 {chr(65 + i)}", ["教育", "本地生活", "美业", "法律"][i % 4],
                     ["kol", "merchant"][i % 2], 3000 * (i + 1), 90 - i * 6, ["new", "drafted", "contacted", "replied", "not_pursue"][i % 5],
                     "近 30 天纯口播占比高，评论里多次问怎么批量做", "你好，看到你最近的口播系列…", at(i % 6, 16)))
    for term, industry, hits in (("数字人口播", "通用", 31), ("批量剪辑", "通用", 18), ("探店视频怎么做", "本地生活", 9), ("课程切片", "教育", 4)):
        ops.execute("INSERT INTO listening_terms(term, platform, industry, active, hits_7d, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (term, "douyin", industry, 1, hits, at(20), at(0)))
    for n in range(1, 31):
        for code, weight in (("dy01", 1.0), ("xhs02", 0.6), (None, 0.3)):
            clicks = int((220 - n * 3) * weight)
            ops.execute("INSERT INTO funnel_daily(date, channel_code, clicks, registrations, first_done, second_done, paid) VALUES (?,?,?,?,?,?,?)",
                        (day(n), code, clicks, clicks // 7, clicks // 12, clicks // 25, clicks // 90))
    ops.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="superops 面板的演示数据服务")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="superops-demo-"))

    def terminate(*_):          # 被 kill 时也走下面的清理，不留临时目录
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    rt = build(tmp, args.port)
    stop = threading.Event()
    try:
        seed(rt)
        seed_business()
        from superops.app import http
        server = http.serve(rt, args.host, args.port)

        def pump() -> None:  # 同步模式没有后台循环：自己每秒跑一轮，面板上的操作才会生效
            while not stop.wait(1.0):
                try:
                    rt.pump()
                except Exception as error:  # noqa: BLE001
                    print(f"pump 出错：{error}", file=sys.stderr)

        threading.Thread(target=pump, name="demo-pump", daemon=True).start()
        print(f"演示面板：http://{args.host}:{args.port}/   （数据在 {tmp}，Ctrl-C 退出并删除）", flush=True)
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        try:
            rt.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
